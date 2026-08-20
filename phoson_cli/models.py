"""User model registry + provider config + model-list cache.

Lives at ``~/.phoson/models.json`` (0600, created lazily). The file holds
three sections:

- ``models``:    user-defined model overrides. Keys are bare model ids
  (as used in ``provider/model`` strings, e.g. ``"qwen3.8-27b"``).
  Supported fields: ``context_window`` (int), ``label`` (str),
  ``description`` (str). User values always win over fetched data.
- ``providers``: non-sensitive provider settings, e.g.
  ``{"openrouter": {"default_model": "qwen3.8-27b"}}`` and
  ``"base_url"`` overrides for OpenAI-compatible endpoints
  (local servers, proxies). **API keys never live here** — keys stay in
  ``config.toml`` / env vars because this file may be synced or shared.
- ``cache``:    cached provider model listings with a fetch timestamp.
  Used for instant/offline ``/model`` pickers and as fallback when the
  network is down.
"""

import json
import time
from typing import Any
from pathlib import Path
from dataclasses import dataclass

#: Default cache lifetime: 24 hours.
CACHE_TTL_SECONDS: float = 86_400.0

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


# ── cache section ────────────────────────────────────────────────────────────


def _cache_providers(data: dict[str, Any]) -> dict[str, Any]:
    cache = data.get("cache", {})
    if not isinstance(cache, dict):
        return {}
    providers = cache.get("providers", {})
    return providers if isinstance(providers, dict) else {}


def cache_fetched_at(data: dict[str, Any]) -> float:
    """Epoch seconds of the last successful fetch (0.0 if never)."""
    cache = data.get("cache", {})
    if not isinstance(cache, dict):
        return 0.0
    value = cache.get("fetched_at", 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def cache_is_fresh(data: dict[str, Any], ttl: float = CACHE_TTL_SECONDS) -> bool:
    """True if the cache exists and is younger than ``ttl`` seconds."""
    fetched = cache_fetched_at(data)
    return fetched > 0 and (time.time() - fetched) < ttl


def update_cache(
    data: dict[str, Any], listings: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Return a copy of ``data`` with the cache section refreshed.

    Listings from previously cached providers are preserved so a single
    successful fetch never wipes the others.
    """
    new_data = dict(data)
    old_cache = data.get("cache", {})
    old_providers = (
        old_cache.get("providers", {})
        if isinstance(old_cache, dict)
        and isinstance(old_cache.get("providers", {}), dict)
        else {}
    )
    merged = dict(old_providers)
    merged.update(listings)
    new_data["cache"] = {
        "fetched_at": time.time(),
        "providers": merged,
    }
    return new_data


def option_to_dict(option: ModelOption) -> dict[str, Any]:
    """Serialize a ModelOption for the cache section."""
    return {
        "id": option.id,
        "label": option.label,
        "provider": option.provider,
        "description": option.description,
        "context_length": option.context_length,
        "pricing": option.pricing,
    }


def cached_options(data: dict[str, Any], provider: str) -> list[ModelOption]:
    """Build options from the cached listing of one provider."""
    listing = _cache_providers(data).get(provider)
    if not isinstance(listing, list):
        return []
    options: list[ModelOption] = []
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        window = entry.get("context_length")
        pricing = entry.get("pricing")
        description = entry.get("description")
        label = entry.get("label")
        options.append(
            ModelOption(
                id=model_id,
                provider=provider,
                label=(str(label) if isinstance(label, str) and label else model_id),
                description=(str(description) if isinstance(description, str) else ""),
                context_length=(
                    window if isinstance(window, int) and window > 0 else None
                ),
                pricing=str(pricing) if isinstance(pricing, str) else "",
            )
        )
    return options
