"""
Configuration management for the Phoson CLI.

Handles loading settings from files and environment variables, and building
the LLM chat clients.
"""

import os
import tomllib
from pathlib import Path
from dataclasses import dataclass

from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.ollama import OllamaChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.openrouter import OpenRouterChat


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


def _parse_int(value: str | None, default: int) -> int:
    """Parse string to integer."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _load_file_defaults(config_path: Path) -> dict:
    """Load defaults from the config TOML file."""
    if not config_path.exists():
        return {}
    with config_path.open("rb") as f:
        raw = tomllib.load(f)
    defaults = raw.get("defaults", {})
    return defaults if isinstance(defaults, dict) else {}


def load_config() -> PhosonConfig:
    """Load configuration from files and environment."""
    defaults = PhosonConfig()
    cfg_file = Path("~/.phoson/config.toml").expanduser()
    file_defaults = _load_file_defaults(cfg_file)

    model = (
        os.environ.get("PHOSON_MODEL") or file_defaults.get("model") or defaults.model
    )
    subagent_model = (
        os.environ.get("PHOSON_SUBAGENT_MODEL")
        or file_defaults.get("subagent_model")
        or defaults.subagent_model
    )
    provider = (
        os.environ.get("PHOSON_PROVIDER")
        or file_defaults.get("provider")
        or defaults.provider
    )
    sessions_dir_raw = (
        os.environ.get("PHOSON_SESSIONS_DIR")
        or file_defaults.get("sessions_dir")
        or str(defaults.sessions_dir)
    )
    max_iterations = _parse_int(
        os.environ.get("PHOSON_MAX_ITERATIONS")
        or str(file_defaults.get("max_iterations", defaults.max_iterations)),
        defaults.max_iterations,
    )
    safe_mode = _parse_bool(
        os.environ.get("PHOSON_SAFE_MODE")
        if "PHOSON_SAFE_MODE" in os.environ
        else (
            str(file_defaults["safe_mode"])
            if "safe_mode" in file_defaults
            else str(defaults.safe_mode)
        ),
        defaults.safe_mode,
    )
    enable_mcp = _parse_bool(
        os.environ.get("PHOSON_ENABLE_MCP")
        if "PHOSON_ENABLE_MCP" in os.environ
        else (
            str(file_defaults["enable_mcp"])
            if "enable_mcp" in file_defaults
            else str(defaults.enable_mcp)
        ),
        defaults.enable_mcp,
    )
    mcp_config_file_raw = (
        os.environ.get("PHOSON_MCP_CONFIG")
        or file_defaults.get("mcp_config_file")
        or "~/.phoson/mcps.json"
    )

    cfg = PhosonConfig(
        model=str(model),
        subagent_model=str(subagent_model) if subagent_model else None,
        provider=str(provider).lower(),
        openrouter_api_key=(
            os.environ.get("OPENROUTER_API_KEY")
            or file_defaults.get("openrouter_api_key")
        ),
        openai_api_key=os.environ.get("OPENAI_API_KEY")
        or file_defaults.get("openai_api_key"),
        anthropic_api_key=(
            os.environ.get("ANTHROPIC_API_KEY")
            or file_defaults.get("anthropic_api_key")
        ),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL")
        or file_defaults.get("ollama_base_url"),
        sessions_dir=Path(str(sessions_dir_raw)).expanduser(),
        max_iterations=max_iterations,
        safe_mode=safe_mode,
        enable_mcp=enable_mcp,
        mcp_config_file=Path(str(mcp_config_file_raw)).expanduser(),
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

    enabled_providers: list[str] = []
    if config.openrouter_api_key:
        enabled_providers.append("openrouter")
    if config.openai_api_key:
        enabled_providers.append("openai")
    if config.anthropic_api_key:
        enabled_providers.append("anthropic")
    if config.ollama_base_url:
        enabled_providers.append("ollama")
    if config.provider not in enabled_providers:
        enabled_providers.append(config.provider)

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
