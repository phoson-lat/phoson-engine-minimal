"""Shared human-readable labels for provider ids.

Single source of truth for the provider id → display-name mapping used by
every UI surface (setup wizard, provider picker, ...). Previously the same
table was duplicated in ``installer.py`` and ``provider_picker.py`` and had
already drifted (the picker carried ``grok``/``google``/``aws`` aliases the
wizard lacked).
"""

#: Provider id → display name. Aliases (e.g. ``grok`` → ``xai``) share the
#: canonical label.
PROVIDER_LABELS: dict[str, str] = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "ollama": "Ollama",
    "github": "GitHub Models",
    "nvidia": "NVIDIA",
    "xai": "Grok (X.AI)",
    "grok": "Grok (X.AI)",
    "groq": "Groq",
    "deepseek": "DeepSeek",
    "alibaba": "Alibaba Cloud",
    "together": "Together AI",
    "perplexity": "Perplexity",
    "lmstudio": "LM Studio",
    "vllm": "vLLM",
    "azure": "Azure OpenAI",
    "gemini": "Google Gemini",
    "google": "Google Gemini",
    "mistral": "Mistral AI",
    "bedrock": "AWS Bedrock",
    "aws": "AWS Bedrock",
    "fireworks": "Fireworks AI",
    "cohere": "Cohere",
    "omniroute": "OmniRoute",
}


#: Bundled plugin spec → (display name, short description) tuple.
#:
#: Keys are the exact spec strings written to ``[defaults].plugins`` in
#: ``config.toml`` — i.e. the importable package names with dashes (the plugin
#: loader maps ``phoson-plugin-x`` → ``phoson_plugin_x``). Ordered by how
#: user-facing they are, which is also the order shown in the setup wizard.
PLUGIN_LABELS: dict[str, tuple[str, str]] = {
    "phoson-plugin-monitor": (
        "Background Monitor",
        "Wake the agent on file changes, intervals or command output",
    ),
    "phoson-plugin-bgjobs": (
        "Background Jobs",
        "Run long shell tasks in the background without blocking the CLI",
    ),
    "phoson-plugin-mcp": (
        "MCP Servers",
        "Connect external tools via the Model Context Protocol",
    ),
    "phoson-plugin-questions": (
        "Interactive Q&A",
        "Let the agent ask you multiple-choice questions mid-task",
    ),
    "phoson-plugin-swarm": (
        "Agent Swarms",
        "Create teams of collaborating sub-agents",
    ),
    "phoson-plugin-ssh": (
        "SSH",
        "Run commands on remote hosts over SSH",
    ),
    "phoson-plugin-stt": (
        "Speech-to-Text",
        "Transcribe microphone input offline (Moonshine, multilingual)",
    ),
    "phoson-plugin-computeruse": (
        "Computer Use",
        "Screenshot, click and type — full desktop automation",
    ),
    "phoson-plugin-memory": (
        "Long-term Memory",
        "Persistent memory across sessions (Redis / Postgres / Qdrant)",
    ),
    "phoson-plugin-checkpoint": (
        "Checkpoints",
        "Save and restore agent state (requires Postgres)",
    ),
    "phoson-plugin-otel": (
        "OpenTelemetry",
        "Export traces and metrics to an OTLP collector",
    ),
    "phoson-plugin-peers": (
        "Peers",
        "Discover and talk to other Phoson instances on the network",
    ),
}


#: Optional dependency each bundled plugin needs at runtime, if any:
#: spec → (module to probe with importlib, ``[extra]`` that provides it).
#: Used by the setup wizard to warn when a selected plugin can't import.
PLUGIN_REQUIRED_EXTRAS: dict[str, tuple[str, str]] = {
    "phoson-plugin-mcp": ("mcp", "mcp"),
    "phoson-plugin-ssh": ("asyncssh", "ssh"),
    "phoson-plugin-stt": ("moonshine_voice", "stt"),
    "phoson-plugin-computeruse": ("mss", "computeruse"),
    "phoson-plugin-memory": ("redis", "memory"),
    "phoson-plugin-checkpoint": ("asyncpg", "checkpoint"),
}


def provider_label(provider: str) -> str:
    """Display name for a provider id (the id itself when unknown)."""
    return PROVIDER_LABELS.get(provider, provider)


def plugin_label(spec: str) -> str:
    """Display name for a plugin spec (the spec itself when unknown)."""
    entry = PLUGIN_LABELS.get(spec)
    return entry[0] if entry else spec


__all__ = [
    "PLUGIN_LABELS",
    "PLUGIN_REQUIRED_EXTRAS",
    "PROVIDER_LABELS",
    "plugin_label",
    "provider_label",
]
