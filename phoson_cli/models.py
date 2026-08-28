"""User model registry + provider config.

Lives at ``~/.phoson/models.json`` (0600, created lazily). The file holds
two sections:

- ``models``:    user-defined model overrides. Keys are bare model ids
  (as used in ``provider/model`` strings, e.g. ``"qwen3.8-27b"``).
  Supported fields: ``context_window`` (int), ``label`` (str),
  ``description`` (str). User values always win over fetched data.
- ``providers``: non-sensitive provider settings, e.g.
  ``{"openrouter": {"default_model": "qwen3.8-27b"}}`` and
  ``"base_url"`` overrides for OpenAI-compatible endpoints
  (local servers, proxies). **API keys never live here** — keys stay in
  ``config.toml`` / env vars because this file may be synced or shared.

The ``/model`` picker always queries the provider live (see
:func:`phoson_cli.model_selector.list_available_models`); nothing about
the available model list is written here. A ``cache`` section written by
older Phoson versions may still be present on disk — :func:`resolve_context_window`
reads it (read-only, for its context-window hints) but the CLI no longer
writes new entries to it.
"""

import json
from typing import Any
from pathlib import Path
from dataclasses import dataclass

#: File name inside the Phoson home directory.
MODELS_FILE_NAME = "models.json"


@dataclass(frozen=True)
class ModelOption:
    """A selectable model in the /model picker."""

    id: str
    label: str
    provider: str
    description: str = ""
    context_length: int | None = None
    pricing: str = ""


def models_file_path(home: str | Path | None = None) -> Path:
    """Return the path of the models file (``~/.phoson/models.json``)."""
    base = (
        Path(home).expanduser() if home is not None else Path("~/.phoson").expanduser()
    )
    return base / MODELS_FILE_NAME


# ── model id → provider inference (I-89) ─────────────────────────────────────

#: Providers that serve *other* vendors' models under the vendor's own
#: ``vendor/model`` id (e.g. OpenRouter serves ``openai/gpt-4o``). For a
#: router, a vendor prefix in the model id does NOT identify the serving
#: provider.
ROUTER_PROVIDERS: frozenset[str] = frozenset({"openrouter", "github"})

#: Provider aliases normalized to their canonical name.
PROVIDER_ALIASES: dict[str, str] = {
    "google": "gemini",
    "aws": "bedrock",
    "grok": "xai",
}

#: Canonical provider names the CLI can serve (mirrors ``build_chat``).
KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {
        "openrouter",
        "openai",
        "anthropic",
        "ollama",
        "github",
        "nvidia",
        "xai",
        "grok",
        "groq",
        "deepseek",
        "together",
        "perplexity",
        "azure",
        "gemini",
        "google",
        "mistral",
        "fireworks",
        "cohere",
        "bedrock",
        "aws",
        "vllm",
        "lmstudio",
    }
)


def normalize_provider(provider: str) -> str:
    """Lowercase a provider name and fold aliases (``google`` → ``gemini``)."""
    p = provider.lower()
    return PROVIDER_ALIASES.get(p, p)


def model_provider_for(
    model_id: str,
    active_provider: str,
    option_provider: str | None = None,
) -> str | None:
    """Best-effort provider for a model id (I-89).

    Resolution order:

    1. ``option_provider`` — the picker option's ``provider`` field, the
       authoritative source when it names a real provider (anything but
       the ``"custom"`` fallback tag used for the current-model entry).
    2. The ``vendor/`` prefix of ``model_id`` — but only when the active
       provider is **not** a router (see :data:`ROUTER_PROVIDERS`), and
       only when the prefix is a known provider name. Unknown prefixes
       (``qwen/...``, local deployment names, …) never trigger a switch.
    3. ``None`` — nothing identifies a different provider; the active
       provider is kept.
    """
    if option_provider:
        normalized = normalize_provider(option_provider)
        if normalized not in ("custom", ""):
            return normalized

    active = normalize_provider(active_provider)
    if "/" in model_id:
        prefix = normalize_provider(model_id.split("/", 1)[0])
        if (
            active not in ROUTER_PROVIDERS
            and prefix in KNOWN_PROVIDERS
            and prefix != active
        ):
            return prefix
    return None


def load_models_file(path: str | Path | None = None) -> dict[str, Any]:
    """Load the models file, returning ``{}`` if missing or invalid.

    The file is user-editable; malformed content must never crash the
    CLI. A warning is printed once per bad load for invalid JSON.
    """
    p = Path(path) if path is not None else models_file_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(
            f"⚠  {p.name} is not valid JSON — ignoring it. "
            "Fix or delete the file to restore defaults."
        )
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_models_file(data: dict[str, Any], path: str | Path | None = None) -> None:
    """Write the models file with restrictive permissions (0600)."""
    p = Path(path) if path is not None else models_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    p.chmod(0o600)


# ── models section: user overrides ───────────────────────────────────────────


def user_model_overrides(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the validated ``models`` section (model id → fields)."""
    raw = data.get("models", {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[str(key)] = value
    return out


def user_context_window(data: dict[str, Any], model: str) -> int | None:
    """Context window override for ``model`` (bare id), if the user set one."""
    entry = user_model_overrides(data).get(model)
    if not entry:
        return None
    value = entry.get("context_window")
    if isinstance(value, int) and value > 0:
        return value
    return None


def apply_model_overrides(
    data: dict[str, Any],
    options: list[ModelOption],
    provider: str,
) -> list[ModelOption]:
    """Merge user overrides into fetched options.

    User ``context_window``/``label``/``description`` win over fetched
    values. Models the user defined that are not in the fetched list are
    appended (so local or custom models appear in the picker).
    """
    overrides = user_model_overrides(data)
    if not overrides:
        return list(options)

    result: list[ModelOption] = []
    seen: set[str] = set()
    for opt in options:
        seen.add(opt.id)
        entry = overrides.get(opt.id)
        if not entry:
            result.append(opt)
            continue
        window = entry.get("context_window")
        result.append(
            ModelOption(
                id=opt.id,
                provider=opt.provider,
                label=(
                    str(entry["label"])
                    if isinstance(entry.get("label"), str) and entry.get("label")
                    else opt.label
                ),
                description=(
                    str(entry["description"])
                    if isinstance(entry.get("description"), str)
                    else opt.description
                ),
                context_length=(
                    window
                    if isinstance(window, int) and window > 0
                    else opt.context_length
                ),
                pricing=opt.pricing,
            )
        )

    for key, entry in overrides.items():
        if key in seen:
            continue
        window = entry.get("context_window")
        result.append(
            ModelOption(
                id=key,
                provider=provider,
                label=(
                    str(entry["label"])
                    if isinstance(entry.get("label"), str) and entry.get("label")
                    else key
                ),
                description=(
                    str(entry["description"])
                    if isinstance(entry.get("description"), str)
                    else "user-defined model"
                ),
                context_length=(
                    window if isinstance(window, int) and window > 0 else None
                ),
            )
        )
    return result


def resolve_context_window(
    data: dict[str, Any], provider: str, model: str
) -> int | None:
    """Context window for ``provider/model`` from user overrides or cache.

    Resolution: user override → cached listing (matched by bare id or
    provider/model) → ``None`` (caller falls back to the engine's
    context-window resolver).
    """
    window = user_context_window(data, model)
    if window is not None:
        return window
    providers = _cache_providers(data)
    candidates: list[Any] = []
    listing = providers.get(provider)
    if isinstance(listing, list):
        candidates.extend(listing)
    # Bare-id lookup across all cached providers (covers user-defined
    # models and provider renames).
    for value in providers.values():
        if isinstance(value, list):
            candidates.extend(value)
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") not in (model, f"{provider}/{model}"):
            continue
        window = entry.get("context_length")
        if isinstance(window, int) and window > 0:
            return window
    return None


# ── providers section: non-sensitive settings ───────────────────────────────


def provider_settings(data: dict[str, Any], provider: str) -> dict[str, Any]:
    """Validated non-sensitive settings for one provider (may be empty).

    Only the allowlisted keys are returned; anything else (including
    anything that looks like a secret) is ignored.
    """
    raw = data.get("providers", {})
    if not isinstance(raw, dict):
        return {}
    entry = raw.get(provider)
    if not isinstance(entry, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("default_model", "base_url"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


# ── cache section (legacy, read-only) ─────────────────────────────────────
#
# Older Phoson versions wrote a ``cache`` section with fetched provider
# listings. The CLI no longer writes it (the /model picker always queries
# live — see model_selector.list_available_models), but existing files may
# still carry one, so resolve_context_window keeps reading it as a source
# of context-window hints.


def _cache_providers(data: dict[str, Any]) -> dict[str, Any]:
    cache = data.get("cache", {})
    if not isinstance(cache, dict):
        return {}
    providers = cache.get("providers", {})
    return providers if isinstance(providers, dict) else {}
