"""
Configuration management for the Phoson CLI.

Handles loading settings from files and environment variables, and building
the LLM chat clients.
"""

import os
import warnings
import tomllib
from typing import Any
from pathlib import Path
from dataclasses import dataclass

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.ollama import OllamaChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.openrouter import OpenRouterChat


class PhosonConfigError(Exception):
    """Raised when the Phoson configuration file is malformed or invalid."""


@dataclass
class PhosonConfig:
    """Application configuration."""

    model: str = "minimax/minimax-m2.5"
    subagent_model: str | None = "google/gemini-3.1-flash-lite-preview"
    provider: str = "openrouter"
    openrouter_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str | None = None
    sessions_dir: Path = Path("~/.phoson/sessions/").expanduser()
    max_iterations: int = 50
    safe_mode: bool = False
    enable_mcp: bool = False
    mcp_config_file: Path = Path("~/.phoson/mcps.json").expanduser()


def _parse_bool(value: str | None, default: bool) -> bool:
    """Parse string to boolean."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int, *, env_var: str = "") -> int:
    """Parse string to integer, warning on malformed input."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        source = f" (from {env_var})" if env_var else ""
        warnings.warn(
            f"Ignoring invalid integer value {value!r}{source}; using default {default}.",
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


def load_config() -> PhosonConfig:
    """Load configuration from files and environment variables.

    Resolution order for each setting: environment variable →
    ``~/.phoson/config.toml`` ``[defaults]`` section → built-in default.
    """
    d = PhosonConfig()
    fd = _load_file_defaults(Path("~/.phoson/config.toml").expanduser())

    cfg = PhosonConfig(
        model=_resolve_str("PHOSON_MODEL", "model", fd, d.model),
        subagent_model=_resolve_optional_str("PHOSON_SUBAGENT_MODEL", "subagent_model", fd, d.subagent_model),
        provider=_resolve_str("PHOSON_PROVIDER", "provider", fd, d.provider).lower(),
        openrouter_api_key=_resolve_optional_str("OPENROUTER_API_KEY", "openrouter_api_key", fd, d.openrouter_api_key),
        openai_api_key=_resolve_optional_str("OPENAI_API_KEY", "openai_api_key", fd, d.openai_api_key),
        anthropic_api_key=_resolve_optional_str("ANTHROPIC_API_KEY", "anthropic_api_key", fd, d.anthropic_api_key),
        ollama_base_url=_resolve_optional_str("OLLAMA_BASE_URL", "ollama_base_url", fd, d.ollama_base_url),
        sessions_dir=Path(_resolve_str("PHOSON_SESSIONS_DIR", "sessions_dir", fd, str(d.sessions_dir))).expanduser(),
        max_iterations=_resolve_int("PHOSON_MAX_ITERATIONS", "max_iterations", fd, d.max_iterations),
        safe_mode=_resolve_bool("PHOSON_SAFE_MODE", "safe_mode", fd, d.safe_mode),
        enable_mcp=_resolve_bool("PHOSON_ENABLE_MCP", "enable_mcp", fd, d.enable_mcp),
        mcp_config_file=Path(_resolve_str("PHOSON_MCP_CONFIG", "mcp_config_file", fd, str(d.mcp_config_file))).expanduser(),
    )
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def save_config(config: PhosonConfig) -> Path:
    """Persist configuration defaults to ~/.phoson/config.toml."""
    config_dir = Path("~/.phoson").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"

    def _line(key: str, value: str | int | bool | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            rendered = f'"{escaped}"'
        return f"{key} = {rendered}"

    enabled_providers = enabled_providers_from_config(config)

    lines = ["[defaults]"]
    for line in [
        _line("provider", config.provider),
        _line("enabled_providers", ",".join(enabled_providers)),
        _line("model", config.model),
        _line("subagent_model", config.subagent_model),
        _line("openrouter_api_key", config.openrouter_api_key),
        _line("openai_api_key", config.openai_api_key),
        _line("anthropic_api_key", config.anthropic_api_key),
        _line("ollama_base_url", config.ollama_base_url),
        _line("sessions_dir", str(config.sessions_dir)),
        _line("max_iterations", config.max_iterations),
        _line("safe_mode", config.safe_mode),
        _line("enable_mcp", config.enable_mcp),
        _line("mcp_config_file", str(config.mcp_config_file)),
    ]:
        if line:
            lines.append(line)

    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def enabled_providers_from_config(config: PhosonConfig) -> list[str]:
    """Return the list of usable providers derived from ``config``.

    A provider is considered enabled when its credential (API key or base URL)
    is present. The active ``config.provider`` is always included so the REPL
    never ends up with an empty list.
    """
    providers: list[str] = []
    if config.openrouter_api_key:
        providers.append("openrouter")
    if config.openai_api_key:
        providers.append("openai")
    if config.anthropic_api_key:
        providers.append("anthropic")
    if config.ollama_base_url:
        providers.append("ollama")
    if config.provider not in providers:
        providers.append(config.provider)
    return providers


def build_chat(config: PhosonConfig) -> BaseLLMChat:
    """Build the appropriate LLM chat client based on configuration."""
    provider = config.provider.lower()
    if provider == "openrouter":
        if not config.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required for provider=openrouter")
        return OpenRouterChat(api_key=config.openrouter_api_key)
    if provider == "openai":
        if not config.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for provider=openai")
        return OpenAIChat(api_key=config.openai_api_key)
    if provider == "anthropic":
        if not config.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for provider=anthropic")
        return AnthropicChat(api_key=config.anthropic_api_key)
    if provider == "ollama":
        return OllamaChat(base_url=config.ollama_base_url or "http://localhost:11434")
    raise ValueError(f"Unsupported provider: {config.provider}")
