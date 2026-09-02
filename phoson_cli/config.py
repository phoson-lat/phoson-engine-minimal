"""
Configuration management for the Phoson CLI.

Handles loading settings from files and environment variables, and building
the LLM chat clients.
"""

import os
import re
import shutil
import tomllib
import warnings
from typing import Any, Final
from pathlib import Path
from dataclasses import field, dataclass

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.grok import GrokChat
from phoson_llm.chats.groq import GroqChat
from phoson_llm.chats.vllm import VLLMChat
from phoson_llm.chats.azure import AzureChat
from phoson_llm.chats.cohere import CohereChat
from phoson_llm.chats.gemini import GeminiChat
from phoson_llm.chats.nvidia import NVIDIAChat
from phoson_llm.chats.ollama import OllamaChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.bedrock import BedrockChat
from phoson_llm.chats.mistral import MistralChat
from phoson_llm.chats.deepseek import DeepSeekChat
from phoson_llm.chats.lmstudio import LMStudioChat
from phoson_llm.chats.together import TogetherChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.fireworks import FireworksChat
from phoson_llm.chats.openrouter import OpenRouterChat
from phoson_llm.chats.perplexity import PerplexityChat
from phoson_llm.chats.github_models import GitHubModelsChat


class PhosonConfigError(Exception):
    """Raised when the Phoson configuration file is malformed or invalid."""


#: Providers that work without any API key (local runtimes / cloud IAM).
#: Single source of truth for the "configured?" checks in the CLI entry
#: point and in :func:`has_configured_provider`.
NO_CREDENTIAL_PROVIDERS: frozenset[str] = frozenset(
    {"ollama", "bedrock", "aws", "vllm", "lmstudio"}
)


@dataclass
class PhosonConfig:
    """Application configuration."""

    model: str = "qwen/qwen3.6-plus"
    subagent_model: str | None = "google/gemini-3.1-flash-lite-preview"
    reasoning_effort: str | None = None
    show_reasoning: bool = True
    provider: str = "openrouter"
    openrouter_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str | None = None
    github_token: str | None = None
    nvidia_api_key: str | None = None
    xai_api_key: str | None = None
    groq_api_key: str | None = None
    deepseek_api_key: str | None = None
    together_api_key: str | None = None
    perplexity_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_deployment: str | None = None
    gemini_api_key: str | None = None
    mistral_api_key: str | None = None
    fireworks_api_key: str | None = None
    cohere_api_key: str | None = None
    vllm_base_url: str | None = None
    vllm_api_key: str | None = None
    lmstudio_base_url: str | None = None
    sessions_dir: Path = Path("~/.phoson/sessions/").expanduser()
    max_iterations: int = 50
    safe_mode: bool = False
    theme: str = "system"
    subagent_max_parallel: int = 4
    subagent_timeout_seconds: float = 300.0
    # ── Wall-clock budget for non-interactive runs (#141 / H-7) ──────────
    # One-shot / stdin-piped runs have no Esc to escape a hang, so they get
    # a hard wall-clock cap at the *run* level (not the per-tool timeout,
    # which I-127 deliberately left uncapped for interactive use). Default
    # 600s; ``0`` disables the budget (unlimited) for those who want it.
    # Interactive mode ignores this entirely — Esc remains the escape.
    run_budget_seconds: float = 600.0
    # ── Completion notification (#167) ──────────────────────────────────
    # Cue the terminal when a run finishes, so a backgrounded window gets
    # attention: "bell" (BEL), "desktop" (OSC 9/777 desktop notification +
    # BEL fallback), or "off". Default "off" — preserves the historical
    # silent behaviour; a ringing bell on every (often frequent) coding
    # turn would be intrusive, so the user opts in via `/notify bell`
    # (or `desktop`), `notify_on_completion` in config.toml, or the
    # PHOSON_NOTIFY_ON_COMPLETION env var. TTY-gated so piped/script output
    # is never polluted.
    notify_on_completion: str = "off"
    enable_mcp: bool = False
    mcp_config_file: Path = Path("~/.phoson/mcps.json").expanduser()
    # Official monitor plugin (I-126): background watchers that wake the
    # agent. Off by default because it runs long-lived tasks; state lives
    # in monitors_data_dir.
    enable_monitors: bool = False
    monitors_data_dir: Path = Path("~/.phoson/monitors/").expanduser()
    # Third-party engine/CLI plugin specifications. They use the same
    # string/dict forms accepted by AgentEngine and are loaded in addition to
    # the optional MCP plugin. Config-file entries are data only; direct Plugin
    # instances remain an API-only AgentEngine feature.
    plugins: list[str | dict[str, Any]] = field(default_factory=list)
    # Input-history file for the front ends. Not a persisted setting (not
    # loaded from / saved to config.toml) — overridable per run, mainly so
    # tests can point it at a temp file instead of the user's real history.
    history_file: Path = Path("~/.phoson/history.txt").expanduser()
    # ── Context management (IMPROVEMENTS.md E1) ─────────────────────────
    # Automatic compaction mode: "balanced" (default), "aggressive"
    # (compacts earlier and keeps a shorter tail) or "off" (never
    # auto-compact; manual /compact still works).
    compact_mode: str = "balanced"
    # Fraction of the context window that triggers automatic compaction.
    # "aggressive" mode tightens this to 0.65; an explicit value wins.
    compact_threshold: float = 0.80
    # How many recent messages survive a compaction untouched.
    compact_min_keep_messages: int = 4
    # Offload large tool outputs to disk (head/tail + path in context).
    offload_tool_outputs: bool = True
    offload_max_chars: int = 24_000
    offload_head_chars: int = 1_500
    offload_tail_chars: int = 500
    # Where offloaded tool outputs live.
    compacted_dir: Path = Path("~/.phoson/compacted/").expanduser()
    # ── Customizable key bindings (IMPROVEMENTS.md E6) ───────────────────
    # User remaps for the full-screen TUI, as ``{action: [sequences]}``
    # (e.g. ``"toggle_reasoning": ["c-x"]``), loaded from the ``[keys]``
    # section of config.toml by :func:`load_key_bindings`. ``None`` =
    # built-in defaults. The file section is user-managed (like
    # permissions.json): save_config never writes it, so a stale value
    # can never override a hand-edited [keys] table.
    key_bindings: dict[str, list[str]] | None = None


def _parse_bool(value: str | None, default: bool) -> bool:
    """Parse string to boolean."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


#: Valid values for ``compact_mode`` (IMPROVEMENTS.md E1). "aggressive"
#: tightens the auto-compact threshold (0.65) and the kept tail (2) so
#: long sessions stay cheap; "off" disables automatic compaction.
COMPACT_MODES: Final[tuple[str, ...]] = ("balanced", "aggressive", "off")

#: (threshold, min_keep_messages) applied when the user picks a mode and
#: has not set the knobs explicitly. An explicit file/env value always
#: wins over the mode preset.
COMPACT_MODE_PRESETS: Final[dict[str, tuple[float, int]]] = {
    "balanced": (0.80, 4),
    "aggressive": (0.65, 2),
    "off": (0.80, 4),
}


def _parse_int(value: str | None, default: int, *, env_var: str = "") -> int:
    """Parse string to integer, warning on malformed input."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        source = f" (from {env_var})" if env_var else ""
        warnings.warn(
            f"Ignoring invalid integer value {value!r}{source};"
            f" using default {default}.",
            UserWarning,
            stacklevel=2,
        )
        return default


def _load_file_defaults(config_path: Path) -> dict[str, Any]:
    """Load defaults from the config TOML file.

    Raises:
        PhosonConfigError: If the file exists but contains invalid TOML.
    """
    if not config_path.exists():
        return {}
    try:
        with config_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise PhosonConfigError(
            f"Malformed configuration file {config_path}: {exc}"
        ) from exc
    defaults = raw.get("defaults", {})
    return defaults if isinstance(defaults, dict) else {}


# ── Per-type resolution helpers (env → file → default) ───────────────────────


def _resolve_str(
    env_var: str,
    file_key: str,
    fd: dict[str, Any],
    default: str,
) -> str:
    return str(os.environ.get(env_var) or fd.get(file_key) or default)


def _resolve_optional_str(
    env_var: str,
    file_key: str,
    fd: dict[str, Any],
    default: str | None,
) -> str | None:
    value = os.environ.get(env_var) or fd.get(file_key) or default
    return str(value) if value else None


def _resolve_bool(
    env_var: str,
    file_key: str,
    fd: dict[str, Any],
    default: bool,
) -> bool:
    if env_var in os.environ:
        return _parse_bool(os.environ[env_var], default)
    if file_key in fd:
        return bool(fd[file_key])
    return default


def _resolve_int(
    env_var: str,
    file_key: str,
    fd: dict[str, Any],
    default: int,
) -> int:
    if env_var in os.environ:
        return _parse_int(os.environ[env_var], default, env_var=env_var)
    return int(fd.get(file_key, default))


def _resolve_plugins(fd: dict[str, Any]) -> list[str | dict[str, Any]]:
    """Validate plugin specs read from ``[defaults].plugins``.

    Plugin configuration intentionally has no environment-variable override:
    TOML is needed for structured per-plugin configuration, and accepting a
    free-form serialized list from an environment variable would be ambiguous
    and hard to audit.
    """
    value = fd.get("plugins", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise PhosonConfigError("[defaults].plugins must be an array of plugin specs")

    specs: list[str | dict[str, Any]] = []
    for index, spec in enumerate(value):
        if isinstance(spec, str):
            if not spec.strip():
                raise PhosonConfigError(
                    f"[defaults].plugins[{index}] must not be an empty string"
                )
            specs.append(spec)
            continue
        if not isinstance(spec, dict):
            raise PhosonConfigError(
                f"[defaults].plugins[{index}] must be a string or inline table"
            )
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PhosonConfigError(
                f"[defaults].plugins[{index}] inline table requires a string 'name'"
            )
        plugin_config = spec.get("config", {})
        if not isinstance(plugin_config, dict):
            raise PhosonConfigError(
                f"[defaults].plugins[{index}].config must be an inline table"
            )
        if set(spec) - {"name", "config"}:
            raise PhosonConfigError(
                f"[defaults].plugins[{index}] supports only 'name' and 'config'"
            )
        specs.append({"name": name, "config": plugin_config})
    return specs


def _resolve_float(
    env_var: str,
    file_key: str,
    fd: dict[str, Any],
    default: float,
) -> float:
    if env_var in os.environ:
        try:
            return float(os.environ[env_var])
        except ValueError:
            warnings.warn(
                f"Ignoring invalid float value {os.environ[env_var]!r} "
                f"(from {env_var}); using default {default}.",
                UserWarning,
                stacklevel=2,
            )
            return default
    value = fd.get(file_key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def has_persisted_theme(config_path: Path | None = None) -> bool:
    """True when the user explicitly set a theme (IMPROVEMENTS.md E4).

    Checks the ``PHOSON_THEME`` env var and the ``theme`` key in the
    config file's ``[defaults]`` section. The built-in default (``dark``)
    does not count — first-run users get no config.toml at all, which is
    exactly when the light/dark suggestion should fire.
    """
    if os.environ.get("PHOSON_THEME", "").strip():
        return True
    path = config_path or Path("~/.phoson/config.toml").expanduser()
    try:
        fd = _load_file_defaults(path)
    except PhosonConfigError:
        return False
    return bool(str(fd.get("theme", "")).strip())


# ── Key bindings (IMPROVEMENTS.md E6) ────────────────────────────────────────


class PhosonKeyBindingsError(PhosonConfigError):
    """Raised when the ``[keys]`` config section cannot be used.

    The message is user-facing (``main()`` prints it and exits cleanly) —
    unlike a generic TOML syntax error, an unparseable *sequence* or an
    unknown *action* is almost always a typo the user should fix, not a
    signal to start with the built-in defaults and never complain.
    """


#: Actions the full-screen TUI exposes to remapping. The single source of
#: truth for the built-in key map lives in
#: :data:`phoson_cli.fullscreen.keys.DEFAULT_KEY_BINDINGS` (the values are
#: prompt_toolkit key sequences, e.g. ``"c-t"``); this tuple is the *names*
#: users may address from ``[keys]`` in config.toml.
KNOWN_KEY_ACTIONS: Final[tuple[str, ...]] = (
    "submit",
    "newline",
    "page_up",
    "page_down",
    "line_up",
    "line_down",
    "scroll_home",
    "scroll_end",
    "clear",
    "toggle_reasoning",
    "cycle_reasoning_effort",
    "ctrl_d",
    "paste_image",
    "escape",
    "undo_jump",
    "toggle_permission_mode",
    "command_palette",
    "exit",
)


def _parse_key_sequence(value: Any, *, action: str) -> list[str]:
    """Parse one ``[keys]`` value into prompt_toolkit key sequences.

    Accepts either a single sequence (``toggle_reasoning = "c-x"``) or a
    list of them (``line_up = ["s-up", "c-up"]`` — same precedence order
    as the built-in defaults). A *sequence* may itself be a chord of
    several keys, space-separated (``"c-x c-e"``). ``""`` means
    "unbound" (the action is disabled).

    Raises:
        PhosonKeyBindingsError: On the wrong type, an empty list, or a
            sequence that prompt_toolkit cannot parse (e.g. ``"ctrl+shift"``).
    """
    if isinstance(value, str):
        raw_values: list[Any] = [value]
    elif isinstance(value, list):
        raw_values = list(value)
    else:
        raise PhosonKeyBindingsError(
            f"Invalid key binding for action {action!r}: expected a string"
            f' (e.g. "c-x") or a list of them, got {type(value).__name__}.'
        )
    if not raw_values:
        raise PhosonKeyBindingsError(
            f"Invalid key binding for action {action!r}:"
            ' an empty list unbinds nothing — use "" instead.'
        )

    from prompt_toolkit.key_binding.key_bindings import _parse_key

    sequences: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            raise PhosonKeyBindingsError(
                f"Invalid key binding for action {action!r}:"
                f" sequence entries must be strings, got {type(raw).__name__}."
            )
        sequence = raw.strip()
        if not sequence:
            continue  # "" = deliberately unbound
        for part in sequence.split():
            try:
                _parse_key(part)
            except (ValueError, TypeError) as exc:
                raise PhosonKeyBindingsError(
                    f"Invalid key sequence {sequence!r} for action {action!r}: {exc}"
                ) from exc
        sequences.append(" ".join(sequence.split()))
    return sequences


def load_key_bindings(config_path: Path | None = None) -> dict[str, list[str]]:
    """Load the ``[keys]`` section of config.toml (IMPROVEMENTS.md E6).

    Returns ``{action: [sequences...]}`` — the same shape
    :func:`~phoson_cli.fullscreen.keys.build_key_bindings` consumes.

    Returns an empty dict when the section is absent (built-in defaults
    apply). Raises :class:`PhosonKeyBindingsError` when the section is
    present but unusable: an unknown action, a wrong value type, or a
    sequence prompt_toolkit cannot parse. (A *malformed TOML file* raises
    :class:`PhosonConfigError` from :func:`load_config` before this is
    even called.)
    """
    path = config_path or Path("~/.phoson/config.toml").expanduser()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise PhosonConfigError(f"Malformed configuration file {path}: {exc}") from exc

    keys_section = raw.get("keys")
    if keys_section is None:
        return {}
    if not isinstance(keys_section, dict):
        raise PhosonKeyBindingsError(
            f"Malformed configuration file {path}:"
            " [keys] must be a table of action = key-sequence pairs."
        )

    resolved: dict[str, list[str]] = {}
    for action, value in keys_section.items():
        if action not in KNOWN_KEY_ACTIONS:
            raise PhosonKeyBindingsError(
                f"Unknown key action {action!r} in [keys]"
                f" ({path}). Valid actions: {', '.join(KNOWN_KEY_ACTIONS)}."
            )
        resolved[action] = _parse_key_sequence(value, action=action)
    return resolved


def load_config() -> PhosonConfig:
    """Load configuration from files and environment variables.

    Resolution order for each setting: environment variable →
    ``~/.phoson/config.toml`` ``[defaults]`` section → built-in default.
    """
    d = PhosonConfig()
    fd = _load_file_defaults(Path("~/.phoson/config.toml").expanduser())
    # [keys] section (IMPROVEMENTS.md E6): user remaps for the full-screen
    # TUI. An empty table = built-in defaults; a malformed one raises
    # PhosonKeyBindingsError with a user-facing message (main() prints it).
    key_bindings = load_key_bindings()

    cfg = PhosonConfig(
        model=_resolve_str("PHOSON_MODEL", "model", fd, d.model),
        subagent_model=_resolve_optional_str(
            "PHOSON_SUBAGENT_MODEL", "subagent_model", fd, d.subagent_model
        ),
        reasoning_effort=_resolve_optional_str(
            "PHOSON_REASONING_EFFORT", "reasoning_effort", fd, d.reasoning_effort
        ),
        show_reasoning=_resolve_bool(
            "PHOSON_SHOW_REASONING", "show_reasoning", fd, d.show_reasoning
        ),
        provider=_resolve_str("PHOSON_PROVIDER", "provider", fd, d.provider).lower(),
        openrouter_api_key=_resolve_optional_str(
            "OPENROUTER_API_KEY", "openrouter_api_key", fd, d.openrouter_api_key
        ),
        openai_api_key=_resolve_optional_str(
            "OPENAI_API_KEY", "openai_api_key", fd, d.openai_api_key
        ),
        anthropic_api_key=_resolve_optional_str(
            "ANTHROPIC_API_KEY", "anthropic_api_key", fd, d.anthropic_api_key
        ),
        ollama_base_url=_resolve_optional_str(
            "OLLAMA_BASE_URL", "ollama_base_url", fd, d.ollama_base_url
        ),
        github_token=_resolve_optional_str(
            "GITHUB_TOKEN", "github_token", fd, d.github_token
        ),
        nvidia_api_key=_resolve_optional_str(
            "NVIDIA_API_KEY", "nvidia_api_key", fd, d.nvidia_api_key
        ),
        xai_api_key=_resolve_optional_str(
            "XAI_API_KEY", "xai_api_key", fd, d.xai_api_key
        ),
        groq_api_key=_resolve_optional_str(
            "GROQ_API_KEY", "groq_api_key", fd, d.groq_api_key
        ),
        deepseek_api_key=_resolve_optional_str(
            "DEEPSEEK_API_KEY", "deepseek_api_key", fd, d.deepseek_api_key
        ),
        together_api_key=_resolve_optional_str(
            "TOGETHER_API_KEY", "together_api_key", fd, d.together_api_key
        ),
        perplexity_api_key=_resolve_optional_str(
            "PERPLEXITY_API_KEY", "perplexity_api_key", fd, d.perplexity_api_key
        ),
        azure_openai_endpoint=_resolve_optional_str(
            "AZURE_OPENAI_ENDPOINT",
            "azure_openai_endpoint",
            fd,
            d.azure_openai_endpoint,
        ),
        azure_openai_api_key=_resolve_optional_str(
            "AZURE_OPENAI_API_KEY",
            "azure_openai_api_key",
            fd,
            d.azure_openai_api_key,
        ),
        azure_openai_deployment=_resolve_optional_str(
            "AZURE_OPENAI_DEPLOYMENT",
            "azure_openai_deployment",
            fd,
            d.azure_openai_deployment,
        ),
        gemini_api_key=_resolve_optional_str(
            "GEMINI_API_KEY", "gemini_api_key", fd, d.gemini_api_key
        ),
        mistral_api_key=_resolve_optional_str(
            "MISTRAL_API_KEY", "mistral_api_key", fd, d.mistral_api_key
        ),
        fireworks_api_key=_resolve_optional_str(
            "FIREWORKS_API_KEY", "fireworks_api_key", fd, d.fireworks_api_key
        ),
        cohere_api_key=_resolve_optional_str(
            "COHERE_API_KEY", "cohere_api_key", fd, d.cohere_api_key
        ),
        vllm_base_url=_resolve_optional_str(
            "VLLM_BASE_URL", "vllm_base_url", fd, d.vllm_base_url
        ),
        vllm_api_key=_resolve_optional_str(
            "VLLM_API_KEY", "vllm_api_key", fd, d.vllm_api_key
        ),
        lmstudio_base_url=_resolve_optional_str(
            "LMSTUDIO_BASE_URL", "lmstudio_base_url", fd, d.lmstudio_base_url
        ),
        sessions_dir=Path(
            _resolve_str("PHOSON_SESSIONS_DIR", "sessions_dir", fd, str(d.sessions_dir))
        ).expanduser(),
        max_iterations=_resolve_int(
            "PHOSON_MAX_ITERATIONS", "max_iterations", fd, d.max_iterations
        ),
        safe_mode=_resolve_bool("PHOSON_SAFE_MODE", "safe_mode", fd, d.safe_mode),
        theme=_resolve_str("PHOSON_THEME", "theme", fd, d.theme),
        subagent_max_parallel=_resolve_int(
            "PHOSON_SUBAGENT_MAX_PARALLEL",
            "subagent_max_parallel",
            fd,
            d.subagent_max_parallel,
        ),
        subagent_timeout_seconds=_resolve_float(
            "PHOSON_SUBAGENT_TIMEOUT",
            "subagent_timeout_seconds",
            fd,
            d.subagent_timeout_seconds,
        ),
        run_budget_seconds=_resolve_float(
            "PHOSON_RUN_BUDGET_SECONDS",
            "run_budget_seconds",
            fd,
            d.run_budget_seconds,
        ),
        notify_on_completion=_resolve_str(
            "PHOSON_NOTIFY_ON_COMPLETION",
            "notify_on_completion",
            fd,
            d.notify_on_completion,
        ).lower(),
        enable_mcp=_resolve_bool("PHOSON_ENABLE_MCP", "enable_mcp", fd, d.enable_mcp),
        mcp_config_file=Path(
            _resolve_str(
                "PHOSON_MCP_CONFIG", "mcp_config_file", fd, str(d.mcp_config_file)
            )
        ).expanduser(),
        enable_monitors=_resolve_bool(
            "PHOSON_ENABLE_MONITORS", "enable_monitors", fd, d.enable_monitors
        ),
        monitors_data_dir=Path(
            _resolve_str(
                "PHOSON_MONITORS_DIR",
                "monitors_data_dir",
                fd,
                str(d.monitors_data_dir),
            )
        ).expanduser(),
        plugins=_resolve_plugins(fd),
        compact_mode=_resolve_str(
            "PHOSON_COMPACT_MODE", "compact_mode", fd, d.compact_mode
        ).lower(),
        compact_threshold=_resolve_float(
            "PHOSON_COMPACT_THRESHOLD", "compact_threshold", fd, d.compact_threshold
        ),
        compact_min_keep_messages=_resolve_int(
            "PHOSON_COMPACT_MIN_KEEP",
            "compact_min_keep_messages",
            fd,
            d.compact_min_keep_messages,
        ),
        offload_tool_outputs=_resolve_bool(
            "PHOSON_OFFLOAD_TOOL_OUTPUTS",
            "offload_tool_outputs",
            fd,
            d.offload_tool_outputs,
        ),
        offload_max_chars=_resolve_int(
            "PHOSON_OFFLOAD_MAX_CHARS", "offload_max_chars", fd, d.offload_max_chars
        ),
        offload_head_chars=_resolve_int(
            "PHOSON_OFFLOAD_HEAD_CHARS",
            "offload_head_chars",
            fd,
            d.offload_head_chars,
        ),
        offload_tail_chars=_resolve_int(
            "PHOSON_OFFLOAD_TAIL_CHARS",
            "offload_tail_chars",
            fd,
            d.offload_tail_chars,
        ),
        compacted_dir=Path(
            _resolve_str(
                "PHOSON_COMPACTED_DIR", "compacted_dir", fd, str(d.compacted_dir)
            )
        ).expanduser(),
        key_bindings=key_bindings or None,
    )
    if cfg.compact_mode not in COMPACT_MODES:
        warnings.warn(
            f"Ignoring invalid compact_mode {cfg.compact_mode!r}; "
            "using default 'balanced'.",
            UserWarning,
            stacklevel=2,
        )
        cfg.compact_mode = "balanced"

    # #167: a typo'd notify_on_completion falls back to the default (off)
    # rather than an unknown mode.
    from phoson_cli.notify import is_valid_mode

    if not is_valid_mode(cfg.notify_on_completion):
        warnings.warn(
            f"Ignoring invalid notify_on_completion "
            f"{cfg.notify_on_completion!r}; using default 'off'.",
            UserWarning,
            stacklevel=2,
        )
        cfg.notify_on_completion = "off"

    # Mode presets fill in the knobs the user has NOT set explicitly, so
    # an explicit threshold/min-keep always wins over the mode (E1).
    preset_threshold, preset_keep = COMPACT_MODE_PRESETS[cfg.compact_mode]
    if "PHOSON_COMPACT_THRESHOLD" not in os.environ and "compact_threshold" not in fd:
        cfg.compact_threshold = preset_threshold
    if (
        "PHOSON_COMPACT_MIN_KEEP" not in os.environ
        and "compact_min_keep_messages" not in fd
    ):
        cfg.compact_min_keep_messages = preset_keep

    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def save_config(
    config: PhosonConfig, *, only_fields: frozenset[str] | set[str] | None = None
) -> Path:
    """Persist configuration defaults to ~/.phoson/config.toml.

    The existing file is updated **in place**: only the managed keys of the
    ``[defaults]`` section are touched (replaced at their original position,
    appended when missing, removed when now ``None``). Every other line —
    comments, user-added keys, extra sections — is preserved byte-for-byte,
    so saving never clobbers content the CLI does not own.

    Args:
        only_fields: When given, restrict this save to just these field
            names (e.g. ``{"model"}``). Every other managed key is left
            exactly as it already is in the file — this is what ``/model``,
            ``/provider``, and the ``/mcp`` commands use so that a narrow
            action never re-persists unrelated settings (including any
            value currently coming from an environment variable rather
            than the file). Ignored — treated as a full save — when the
            file does not exist yet, so the very first save always writes
            a complete ``[defaults]`` section.

    Before writing, the previous file (if any) is copied to
    ``config.toml.bak`` so a previous configuration is never truly lost,
    even if an unexpected value slips into this save.
    """
    config_dir = Path("~/.phoson").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"

    if only_fields is not None and not config_path.exists():
        only_fields = None  # first save ever: always write a full section

    if config_path.exists():
        try:
            backup_path = config_path.parent / f"{config_path.name}.bak"
            shutil.copy2(config_path, backup_path)
            os.chmod(backup_path, 0o600)
        except OSError:  # pragma: no cover - best-effort safety net
            pass

    def _toml_value(value: Any) -> str:
        """Render the restricted TOML values managed by this CLI.

        ``plugins`` needs an array of strings/inline tables. Keep this encoder
        deliberately small and fail before writing for values TOML cannot
        represent, rather than silently persisting Python ``repr`` output.
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        if isinstance(value, list):
            return "[" + ", ".join(_toml_value(item) for item in value) + "]"
        if isinstance(value, dict):
            rendered_items: list[str] = []
            for item_key, item_value in value.items():
                if not isinstance(item_key, str):
                    raise PhosonConfigError("TOML inline-table keys must be strings")
                rendered_key = (
                    item_key
                    if re.fullmatch(r"[A-Za-z0-9_-]+", item_key)
                    else _toml_value(item_key)
                )
                rendered_items.append(f"{rendered_key} = {_toml_value(item_value)}")
            return "{ " + ", ".join(rendered_items) + " }"
        raise PhosonConfigError(
            f"Cannot persist TOML value of type {type(value).__name__}"
        )

    def _line(key: str, value: Any) -> str | None:
        return None if value is None else f"{key} = {_toml_value(value)}"

    enabled_providers = enabled_providers_from_config(config)

    managed: dict[str, str | None] = {}
    for key, value in [
        ("provider", getattr(config, "provider", None)),
        ("enabled_providers", ",".join(enabled_providers)),
        ("model", getattr(config, "model", None)),
        ("subagent_model", getattr(config, "subagent_model", None)),
        ("reasoning_effort", getattr(config, "reasoning_effort", None)),
        ("show_reasoning", getattr(config, "show_reasoning", True)),
        ("openrouter_api_key", getattr(config, "openrouter_api_key", None)),
        ("openai_api_key", getattr(config, "openai_api_key", None)),
        ("anthropic_api_key", getattr(config, "anthropic_api_key", None)),
        ("ollama_base_url", getattr(config, "ollama_base_url", None)),
        ("github_token", getattr(config, "github_token", None)),
        ("nvidia_api_key", getattr(config, "nvidia_api_key", None)),
        ("xai_api_key", getattr(config, "xai_api_key", None)),
        ("groq_api_key", getattr(config, "groq_api_key", None)),
        ("deepseek_api_key", getattr(config, "deepseek_api_key", None)),
        ("together_api_key", getattr(config, "together_api_key", None)),
        ("perplexity_api_key", getattr(config, "perplexity_api_key", None)),
        ("azure_openai_endpoint", getattr(config, "azure_openai_endpoint", None)),
        ("azure_openai_api_key", getattr(config, "azure_openai_api_key", None)),
        ("azure_openai_deployment", getattr(config, "azure_openai_deployment", None)),
        ("gemini_api_key", getattr(config, "gemini_api_key", None)),
        ("mistral_api_key", getattr(config, "mistral_api_key", None)),
        ("fireworks_api_key", getattr(config, "fireworks_api_key", None)),
        ("cohere_api_key", getattr(config, "cohere_api_key", None)),
        ("vllm_base_url", getattr(config, "vllm_base_url", None)),
        ("vllm_api_key", getattr(config, "vllm_api_key", None)),
        ("lmstudio_base_url", getattr(config, "lmstudio_base_url", None)),
        ("sessions_dir", str(getattr(config, "sessions_dir", ""))),
        ("max_iterations", getattr(config, "max_iterations", None)),
        ("safe_mode", getattr(config, "safe_mode", None)),
        ("theme", getattr(config, "theme", None)),
        ("subagent_max_parallel", getattr(config, "subagent_max_parallel", None)),
        ("subagent_timeout_seconds", getattr(config, "subagent_timeout_seconds", None)),
        ("run_budget_seconds", getattr(config, "run_budget_seconds", None)),
        ("notify_on_completion", getattr(config, "notify_on_completion", None)),
        ("enable_mcp", getattr(config, "enable_mcp", None)),
        ("mcp_config_file", str(getattr(config, "mcp_config_file", ""))),
        ("enable_monitors", getattr(config, "enable_monitors", None)),
        ("monitors_data_dir", str(getattr(config, "monitors_data_dir", ""))),
        ("plugins", getattr(config, "plugins", None)),
        ("compact_mode", getattr(config, "compact_mode", None)),
        ("compact_threshold", getattr(config, "compact_threshold", None)),
        (
            "compact_min_keep_messages",
            getattr(config, "compact_min_keep_messages", None),
        ),
        ("offload_tool_outputs", getattr(config, "offload_tool_outputs", None)),
        ("offload_max_chars", getattr(config, "offload_max_chars", None)),
        ("offload_head_chars", getattr(config, "offload_head_chars", None)),
        ("offload_tail_chars", getattr(config, "offload_tail_chars", None)),
        ("compacted_dir", str(getattr(config, "compacted_dir", ""))),
    ]:
        if only_fields is not None and key not in only_fields:
            continue  # not part of this narrow save — leave the file's line alone
        managed[key] = _line(key, value)

    def _is_complete_value(line: str) -> bool:
        # Managed values are single-line scalars; a managed key whose line
        # has unbalanced brackets/quotes is a multi-line value we must not
        # break (leave it as-is).
        return (
            line.count("[") == line.count("]")
            and line.count("{") == line.count("}")
            and line.count('"') % 2 == 0
        )

    lines = (
        config_path.read_text(encoding="utf-8").splitlines()
        if config_path.exists()
        else []
    )

    out: list[str] = []
    in_defaults = False
    touched: set[str] = set()
    defaults_end = None  # index in `out` after the last [defaults] line
    section_open = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_defaults = stripped == "[defaults]"
            out.append(line)
            if in_defaults:
                section_open = True
                defaults_end = len(out)
            continue
        if in_defaults and stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in managed and "=" in stripped:
                if _is_complete_value(stripped):
                    replacement = managed[key]
                    if replacement is not None:
                        out.append(replacement)
                    # else: the managed key was cleared -> drop the line
                # else: multi-line value — preserved byte-for-byte
                touched.add(key)
                continue
        out.append(line)
        if in_defaults and stripped and not stripped.startswith("#"):
            defaults_end = len(out)

    if not section_open:
        # No [defaults] section in the file: start one at the top.
        block = ["[defaults]"]
        out = block + out
        defaults_end = 1

    missing = [
        line for key, line in managed.items() if line is not None and key not in touched
    ]
    if missing:
        out[defaults_end:defaults_end] = missing

    config_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    # The file holds API keys — restrict it to the owner. The parent
    # directory also stores session data, so keep it private as well.
    try:
        os.chmod(config_path, 0o600)
        os.chmod(config_dir, 0o700)
    except OSError:  # pragma: no cover - non-POSIX filesystems
        pass

    return config_path


def _credential_providers(config: PhosonConfig) -> list[str]:
    """Return the providers that have a usable credential in ``config``.

    Alias names are included alongside their primary (e.g. ``xai`` also
    enables ``grok``, ``gemini`` also enables ``google``).
    """
    providers: list[str] = []
    if getattr(config, "openrouter_api_key", None):
        providers.append("openrouter")
    if getattr(config, "openai_api_key", None):
        providers.append("openai")
    if getattr(config, "anthropic_api_key", None):
        providers.append("anthropic")
    if getattr(config, "ollama_base_url", None):
        providers.append("ollama")
    if getattr(config, "github_token", None):
        providers.append("github")
    if getattr(config, "nvidia_api_key", None):
        providers.append("nvidia")
    if getattr(config, "xai_api_key", None):
        providers.append("xai")
        providers.append("grok")
    if getattr(config, "groq_api_key", None):
        providers.append("groq")
    if getattr(config, "deepseek_api_key", None):
        providers.append("deepseek")
    if getattr(config, "together_api_key", None):
        providers.append("together")
    if getattr(config, "perplexity_api_key", None):
        providers.append("perplexity")
    if getattr(config, "azure_openai_api_key", None):
        providers.append("azure")
    if getattr(config, "gemini_api_key", None):
        providers.append("gemini")
        providers.append("google")
    if getattr(config, "mistral_api_key", None):
        providers.append("mistral")
    if getattr(config, "fireworks_api_key", None):
        providers.append("fireworks")
    if getattr(config, "cohere_api_key", None):
        providers.append("cohere")
    if getattr(config, "vllm_base_url", None) or getattr(config, "vllm_api_key", None):
        providers.append("vllm")
    if getattr(config, "lmstudio_base_url", None):
        providers.append("lmstudio")
    return providers


def enabled_providers_from_config(config: PhosonConfig) -> list[str]:
    """Return the list of usable providers derived from ``config``.

    A provider is considered enabled when its credential (API key or base URL)
    is present. The active ``config.provider`` is always included so the REPL
    never ends up with an empty list.
    """
    providers = _credential_providers(config)
    if getattr(config, "provider", None) not in providers:
        providers.append(config.provider)
    return providers


def has_configured_provider(config: PhosonConfig) -> bool:
    """Return whether ``config`` has at least one usable provider.

    True when the active provider needs no credential (see
    :data:`NO_CREDENTIAL_PROVIDERS`) or when any credential is present.
    """
    if config.provider.lower() in NO_CREDENTIAL_PROVIDERS:
        return True
    return bool(_credential_providers(config))


def _models_provider_base_url(config: PhosonConfig, provider: str) -> str | None:
    """``base_url`` override for ``provider`` from models.json, if any."""
    from .models import load_models_file, provider_settings

    return provider_settings(load_models_file(), provider).get("base_url")


def build_chat(config: PhosonConfig) -> BaseLLMChat:
    """Build the appropriate LLM chat client based on configuration."""
    provider = config.provider.lower()
    base_url = _models_provider_base_url(config, provider)
    if provider == "openrouter":
        if not config.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required for provider=openrouter")
        if base_url:
            return OpenRouterChat(api_key=config.openrouter_api_key, base_url=base_url)
        return OpenRouterChat(api_key=config.openrouter_api_key)
    if provider == "openai":
        if not config.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for provider=openai")
        return OpenAIChat(api_key=config.openai_api_key, base_url=base_url)
    if provider == "anthropic":
        if not config.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for provider=anthropic")
        return AnthropicChat(api_key=config.anthropic_api_key, base_url=base_url)
    if provider == "ollama":
        return OllamaChat(
            base_url=base_url or config.ollama_base_url or "http://localhost:11434"
        )
    if provider == "github":
        return GitHubModelsChat(api_key=config.github_token, base_url=base_url)
    if provider == "nvidia":
        return NVIDIAChat(api_key=config.nvidia_api_key, base_url=base_url)
    if provider in ("xai", "grok"):
        return GrokChat(api_key=config.xai_api_key, base_url=base_url)
    if provider == "groq":
        return GroqChat(api_key=config.groq_api_key, base_url=base_url)
    if provider == "deepseek":
        return DeepSeekChat(api_key=config.deepseek_api_key, base_url=base_url)
    if provider == "together":
        return TogetherChat(api_key=config.together_api_key, base_url=base_url)
    if provider == "perplexity":
        return PerplexityChat(api_key=config.perplexity_api_key, base_url=base_url)
    if provider == "azure":
        return AzureChat(
            azure_endpoint=config.azure_openai_endpoint,
            api_key=config.azure_openai_api_key,
            deployment=config.azure_openai_deployment,
        )
    if provider in ("gemini", "google"):
        return GeminiChat(api_key=config.gemini_api_key)
    if provider == "mistral":
        return MistralChat(api_key=config.mistral_api_key)
    if provider in ("bedrock", "aws"):
        return BedrockChat()
    if provider == "fireworks":
        return FireworksChat(api_key=config.fireworks_api_key, base_url=base_url)
    if provider == "cohere":
        return CohereChat(api_key=config.cohere_api_key, base_url=base_url)
    if provider == "vllm":
        return VLLMChat(
            base_url=base_url or config.vllm_base_url or "http://localhost:8000/v1",
            api_key=config.vllm_api_key,
        )
    if provider == "lmstudio":
        return LMStudioChat(
            base_url=base_url or config.lmstudio_base_url or "http://localhost:1234/v1"
        )
    raise ValueError(f"Unsupported provider: {config.provider}")
