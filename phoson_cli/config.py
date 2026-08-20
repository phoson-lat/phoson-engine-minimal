"""
Configuration management for the Phoson CLI.

Handles loading settings from files and environment variables, and building
the LLM chat clients.
"""

import os
import tomllib
import warnings
from typing import Any
from pathlib import Path
from dataclasses import dataclass

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

    model: str = "minimax/minimax-m2.5"
    subagent_model: str | None = "google/gemini-3.1-flash-lite-preview"
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
    subagent_max_parallel: int = 4
    subagent_timeout_seconds: float = 300.0
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


def load_config() -> PhosonConfig:
    """Load configuration from files and environment variables.

    Resolution order for each setting: environment variable →
    ``~/.phoson/config.toml`` ``[defaults]`` section → built-in default.
    """
    d = PhosonConfig()
    fd = _load_file_defaults(Path("~/.phoson/config.toml").expanduser())

    cfg = PhosonConfig(
        model=_resolve_str("PHOSON_MODEL", "model", fd, d.model),
        subagent_model=_resolve_optional_str(
            "PHOSON_SUBAGENT_MODEL", "subagent_model", fd, d.subagent_model
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
        enable_mcp=_resolve_bool("PHOSON_ENABLE_MCP", "enable_mcp", fd, d.enable_mcp),
        mcp_config_file=Path(
            _resolve_str(
                "PHOSON_MCP_CONFIG", "mcp_config_file", fd, str(d.mcp_config_file)
            )
        ).expanduser(),
    )
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def save_config(config: PhosonConfig) -> Path:
    """Persist configuration defaults to ~/.phoson/config.toml."""
    config_dir = Path("~/.phoson").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"

    def _line(key: str, value: str | int | float | bool | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            rendered = f'"{escaped}"'
        return f"{key} = {rendered}"

    enabled_providers = enabled_providers_from_config(config)

    lines = ["[defaults]"]
    for line in [
        _line("provider", getattr(config, "provider", None)),
        _line("enabled_providers", ",".join(enabled_providers)),
        _line("model", getattr(config, "model", None)),
        _line("subagent_model", getattr(config, "subagent_model", None)),
        _line("openrouter_api_key", getattr(config, "openrouter_api_key", None)),
        _line("openai_api_key", getattr(config, "openai_api_key", None)),
        _line("anthropic_api_key", getattr(config, "anthropic_api_key", None)),
        _line("ollama_base_url", getattr(config, "ollama_base_url", None)),
        _line("github_token", getattr(config, "github_token", None)),
        _line("nvidia_api_key", getattr(config, "nvidia_api_key", None)),
        _line("xai_api_key", getattr(config, "xai_api_key", None)),
        _line("groq_api_key", getattr(config, "groq_api_key", None)),
        _line("deepseek_api_key", getattr(config, "deepseek_api_key", None)),
        _line("together_api_key", getattr(config, "together_api_key", None)),
        _line("perplexity_api_key", getattr(config, "perplexity_api_key", None)),
        _line("azure_openai_endpoint", getattr(config, "azure_openai_endpoint", None)),
        _line("azure_openai_api_key", getattr(config, "azure_openai_api_key", None)),
        _line(
            "azure_openai_deployment", getattr(config, "azure_openai_deployment", None)
        ),
        _line("gemini_api_key", getattr(config, "gemini_api_key", None)),
        _line("mistral_api_key", getattr(config, "mistral_api_key", None)),
        _line("fireworks_api_key", getattr(config, "fireworks_api_key", None)),
        _line("cohere_api_key", getattr(config, "cohere_api_key", None)),
        _line("vllm_base_url", getattr(config, "vllm_base_url", None)),
        _line("vllm_api_key", getattr(config, "vllm_api_key", None)),
        _line("lmstudio_base_url", getattr(config, "lmstudio_base_url", None)),
        _line("sessions_dir", str(getattr(config, "sessions_dir", ""))),
        _line("max_iterations", getattr(config, "max_iterations", None)),
        _line("safe_mode", getattr(config, "safe_mode", None)),
        _line("subagent_max_parallel", getattr(config, "subagent_max_parallel", None)),
        _line(
            "subagent_timeout_seconds",
            getattr(config, "subagent_timeout_seconds", None),
        ),
        _line("enable_mcp", getattr(config, "enable_mcp", None)),
        _line("mcp_config_file", str(getattr(config, "mcp_config_file", ""))),
    ]:
        if line:
            lines.append(line)

    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
    if provider == "github":
        return GitHubModelsChat(api_key=config.github_token)
    if provider == "nvidia":
        return NVIDIAChat(api_key=config.nvidia_api_key)
    if provider in ("xai", "grok"):
        return GrokChat(api_key=config.xai_api_key)
    if provider == "groq":
        return GroqChat(api_key=config.groq_api_key)
    if provider == "deepseek":
        return DeepSeekChat(api_key=config.deepseek_api_key)
    if provider == "together":
        return TogetherChat(api_key=config.together_api_key)
    if provider == "perplexity":
        return PerplexityChat(api_key=config.perplexity_api_key)
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
        return FireworksChat(api_key=config.fireworks_api_key)
    if provider == "cohere":
        return CohereChat(api_key=config.cohere_api_key)
    if provider == "vllm":
        return VLLMChat(
            base_url=config.vllm_base_url or "http://localhost:8000/v1",
            api_key=config.vllm_api_key,
        )
    if provider == "lmstudio":
        return LMStudioChat(
            base_url=config.lmstudio_base_url or "http://localhost:1234/v1"
        )
    raise ValueError(f"Unsupported provider: {config.provider}")
