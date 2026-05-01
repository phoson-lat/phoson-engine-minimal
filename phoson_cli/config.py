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
    model: str = "minimax/minimax-m2.5"
    subagent_model: str | None = "inception/mercury-2"
    provider: str = "openrouter"
    openrouter_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str | None = None
    sessions_dir: Path = Path("~/.phoson/sessions/").expanduser()
    max_iterations: int = 50
    safe_mode: bool = False


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default

def _load_file_defaults(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    with config_path.open("rb") as f:
        raw = tomllib.load(f)
    defaults = raw.get("defaults", {})
    return defaults if isinstance(defaults, dict) else {}

def load_config() -> PhosonConfig:
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

    cfg = PhosonConfig(
        model=str(model),
        subagent_model=str(subagent_model) if subagent_model else None,
        provider=str(provider).lower(),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL"),
        sessions_dir=Path(str(sessions_dir_raw)).expanduser(),
        max_iterations=max_iterations,
        safe_mode=safe_mode,
    )
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def build_chat(config: PhosonConfig) -> BaseLLMChat:
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
