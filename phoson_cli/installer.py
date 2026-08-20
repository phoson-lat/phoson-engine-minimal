from pathlib import Path
from dataclasses import replace

from rich import box
from rich.rule import Rule
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from prompt_toolkit.styles import Style
from prompt_toolkit.shortcuts import CompleteStyle, PromptSession

from phoson_cli.theme import load_theme, build_wizard_prompt_style

from .config import PhosonConfig, save_config
from .model_selector import list_available_models

_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "ollama": "Ollama",
    "github": "GitHub Models",
    "nvidia": "NVIDIA",
    "xai": "Grok (X.AI)",
    "groq": "Groq",
    "deepseek": "DeepSeek",
    "together": "Together AI",
    "perplexity": "Perplexity",
    "lmstudio": "LM Studio",
    "vllm": "vLLM",
    "azure": "Azure OpenAI",
    "gemini": "Google Gemini",
    "mistral": "Mistral AI",
    "bedrock": "AWS Bedrock",
    "fireworks": "Fireworks AI",
    "cohere": "Cohere",
}

_PHOS_ART = (
    (Path(__file__).parent / "phos-ascii.txt").read_text(encoding="utf-8").rstrip("\n")
)


class SetupWizard:
    """Interactive terminal wizard that guides the user through initial setup.

    Prompts for provider credentials, default models, and runtime options,
    then writes the result to ``~/.phoson/config.toml`` via
    :func:`~phoson_cli.config.save_config`.
    """

    def __init__(
        self, config: PhosonConfig | None = None, console: Console | None = None
    ):
        """Initialize the wizard.

        Args:
            config: Existing configuration to pre-populate prompts with.
                    Defaults to a fresh :class:`~phoson_cli.config.PhosonConfig`.
            console: Rich console for output. Defaults to a plain Console.
        """
        self.console = console or Console(highlight=False)
        self.config = config or PhosonConfig()
        self.enabled_providers = self._infer_enabled_providers(self.config)
        self.theme = load_theme(getattr(self.config, "theme", None))
        self.session = PromptSession(
            style=Style.from_dict(build_wizard_prompt_style(self.theme))
        )

    async def run(self) -> PhosonConfig:
        """Run the full wizard and return the final configuration.

        Steps through banner, provider selection, credentials, defaults, and
        runtime options. Offers to save when complete.

        Returns:
            The configured :class:`~phoson_cli.config.PhosonConfig` (saved or
            unsaved, depending on the user's choice).
        """
        self._print_banner()
        self._print_intro()
        await self._pick_enabled_providers()
        updated = replace(self.config)
        updated = await self._configure_providers(updated)
        updated = await self._configure_defaults(updated)
        updated = await self._configure_runtime(updated)
        self._print_summary(updated)
        if await self._confirm("Save this configuration?", default=True):
            path = save_config(updated)
            self.console.print(
                Panel.fit(
                    f"Saved configuration to [bold]{path}[/bold]",
                    border_style=self.theme.ok,
                )
            )
            self.config = updated
        else:
            self.console.print(
                Panel.fit("Configuration not saved.", border_style=self.theme.warn)
            )
        return self.config

    def _print_banner(self) -> None:
        """Render the ASCII art banner and wizard title."""
        art = Text(_PHOS_ART, style=self.theme.art)
        subtitle = Text()
        subtitle.append("phoson setup wizard\n", style=f"bold {self.theme.accent}")
        subtitle.append(
            "configure multiple providers, defaults, and secrets",
            style=self.theme.muted,
        )
        self.console.print()
        self.console.print(
            Panel.fit(art, border_style=self.theme.accent_soft, box=box.SQUARE)
        )
        self.console.print(subtitle)
        self.console.print(Rule(style=self.theme.accent_soft))

    def _print_intro(self) -> None:
        """Print the introductory welcome panel."""
        welcome = (
            "[bold]Welcome[/bold] — this wizard lets you enable one or more "
            "providers,\n"
            "store API credentials, and choose default models for the main agent\n"
            "and sub-agents."
        )
        self.console.print(
            Panel(
                welcome,
                border_style=self.theme.muted_deep,
                box=box.ROUNDED,
            )
        )

    async def _pick_enabled_providers(self) -> None:
        """Interactively toggle which providers are enabled.

        Displays a numbered list and lets the user toggle entries by typing
        their numbers. Updates ``self.enabled_providers`` in place.
        """
        providers = [
            "openrouter",
            "openai",
            "anthropic",
            "ollama",
            "github",
            "nvidia",
            "xai",
            "groq",
            "deepseek",
            "together",
            "perplexity",
            "lmstudio",
            "vllm",
            "azure",
            "gemini",
            "mistral",
            "bedrock",
            "fireworks",
            "cohere",
        ]
        selected = set(self.enabled_providers)

        while True:
            self.console.print()
            self.console.print(
                Text("Enable providers", style=f"bold {self.theme.accent}")
            )
            for idx, provider in enumerate(providers, start=1):
                marker = "[x]" if provider in selected else "[ ]"
                state_style = (
                    self.theme.ok if provider in selected else self.theme.muted
                )
                line = Text(f"  {idx}. ")
                line.append(f"{marker} ", style=state_style)
                line.append(_PROVIDER_LABELS[provider], style="white")
                self.console.print(line)
            self.console.print(
                Text(
                    "\nType numbers to toggle (e.g. 1 3), Enter to continue.",
                    style=self.theme.muted,
                )
            )
            raw = (await self._prompt_text("providers", default="")).strip()
            if not raw:
                if selected:
                    self.enabled_providers = [p for p in providers if p in selected]
                    return
                self.console.print(
                    Text(
                        "Select at least one provider.", style=f"bold {self.theme.err}"
                    )
                )
                continue
            for token in raw.replace(",", " ").split():
                if token.isdigit() and 1 <= int(token) <= len(providers):
                    provider = providers[int(token) - 1]
                    if provider in selected:
                        selected.remove(provider)
                    else:
                        selected.add(provider)

    async def _configure_providers(self, config: PhosonConfig) -> PhosonConfig:
        """Prompt for API credentials for each enabled provider.

        Args:
            config: Configuration object to mutate with the collected credentials.

        Returns:
            The updated configuration.
        """
        self.console.print()
        self.console.print(
            Text("Provider credentials", style=f"bold {self.theme.accent}")
        )

        if "openrouter" in self.enabled_providers:
            config.openrouter_api_key = await self._secret_prompt(
                "OpenRouter API key",
                config.openrouter_api_key,
            )
        if "openai" in self.enabled_providers:
            config.openai_api_key = await self._secret_prompt(
                "OpenAI API key",
                config.openai_api_key,
            )
        if "anthropic" in self.enabled_providers:
            config.anthropic_api_key = await self._secret_prompt(
                "Anthropic API key",
                config.anthropic_api_key,
            )
        if "ollama" in self.enabled_providers:
            config.ollama_base_url = await self._prompt_text(
                "Ollama base URL",
                config.ollama_base_url or "http://localhost:11434",
            )
        if "github" in self.enabled_providers:
            config.github_token = await self._secret_prompt(
                "GitHub token",
                config.github_token,
            )
        if "nvidia" in self.enabled_providers:
            config.nvidia_api_key = await self._secret_prompt(
                "NVIDIA API key",
                config.nvidia_api_key,
            )
        if "xai" in self.enabled_providers:
            config.xai_api_key = await self._secret_prompt(
                "xAI / Grok API key",
                config.xai_api_key,
            )
        if "groq" in self.enabled_providers:
            config.groq_api_key = await self._secret_prompt(
                "Groq API key",
                config.groq_api_key,
            )
        if "deepseek" in self.enabled_providers:
            config.deepseek_api_key = await self._secret_prompt(
                "DeepSeek API key",
                config.deepseek_api_key,
            )
        if "together" in self.enabled_providers:
            config.together_api_key = await self._secret_prompt(
                "Together AI API key",
                config.together_api_key,
            )
        if "perplexity" in self.enabled_providers:
            config.perplexity_api_key = await self._secret_prompt(
                "Perplexity API key",
                config.perplexity_api_key,
            )
        if "lmstudio" in self.enabled_providers:
            config.lmstudio_base_url = await self._prompt_text(
                "LM Studio base URL",
                config.lmstudio_base_url or "http://localhost:1234/v1",
            )
        if "vllm" in self.enabled_providers:
            config.vllm_base_url = await self._prompt_text(
                "vLLM base URL",
                config.vllm_base_url or "http://localhost:8000/v1",
            )
            config.vllm_api_key = await self._secret_prompt(
                "vLLM API key (optional, press Enter to skip)",
                config.vllm_api_key,
            )
        if "azure" in self.enabled_providers:
            config.azure_openai_endpoint = await self._prompt_text(
                "Azure OpenAI endpoint",
                config.azure_openai_endpoint or "https://<resource>.openai.azure.com",
            )
            config.azure_openai_api_key = await self._secret_prompt(
                "Azure OpenAI API key",
                config.azure_openai_api_key,
            )
            config.azure_openai_deployment = await self._prompt_text(
                "Azure OpenAI deployment name",
                config.azure_openai_deployment,
            )
        if "gemini" in self.enabled_providers:
            config.gemini_api_key = await self._secret_prompt(
                "Google Gemini API key",
                config.gemini_api_key,
            )
        if "mistral" in self.enabled_providers:
            config.mistral_api_key = await self._secret_prompt(
                "Mistral API key",
                config.mistral_api_key,
            )
        if "bedrock" in self.enabled_providers:
            self.console.print(
                Text(
                    "  AWS Bedrock uses your environment credentials "
                    "(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY).",
                    style=self.theme.muted,
                )
            )
        if "fireworks" in self.enabled_providers:
            config.fireworks_api_key = await self._secret_prompt(
                "Fireworks AI API key",
                config.fireworks_api_key,
            )
        if "cohere" in self.enabled_providers:
            config.cohere_api_key = await self._secret_prompt(
                "Cohere API key",
                config.cohere_api_key,
            )
        return config

    async def _configure_defaults(self, config: PhosonConfig) -> PhosonConfig:
        """Prompt for default provider, main model, and sub-agent model.

        Fetches available models from the provider's API and shows the top 8
        as suggestions.

        Args:
            config: Configuration object to mutate.

        Returns:
            The updated configuration.
        """
        self.console.print()
        self.console.print(
            Text("Default runtime selection", style=f"bold {self.theme.accent}")
        )

        default_provider = await self._choose_default_provider(config.provider)
        config.provider = default_provider

        models = await list_available_models(config)
        suggested = [option.id for option in models[:8]]

        if suggested:
            table = Table(box=box.SIMPLE_HEAD, border_style=self.theme.accent_soft)
            table.add_column("Suggested models", style="white")
            for model in suggested:
                table.add_row(model)
            self.console.print(table)

        config.model = await self._prompt_text(
            "Default model",
            suggested[0] if suggested else config.model,
        )
        config.subagent_model = await self._prompt_text(
            "Default sub-agent model",
            config.subagent_model or config.model,
        )
        return config

    async def _configure_runtime(self, config: PhosonConfig) -> PhosonConfig:
        """Prompt for runtime settings: sessions directory, iteration budget, safe mode.

        Args:
            config: Configuration object to mutate.

        Returns:
            The updated configuration.
        """
        self.console.print()
        self.console.print(Text("Runtime options", style=f"bold {self.theme.accent}"))
        config.sessions_dir = Path(
            await self._prompt_text("Sessions directory", str(config.sessions_dir))
        ).expanduser()
        config.max_iterations = await self._int_prompt(
            "Max iterations",
            config.max_iterations,
        )
        config.safe_mode = await self._confirm(
            "Enable safe mode?",
            config.safe_mode,
        )
        return config

    def _print_summary(self, config: PhosonConfig) -> None:
        """Render a Rich table summarizing all collected configuration values.

        Args:
            config: The configuration to display.
        """
        table = Table(title="Phoson configuration summary", box=box.ROUNDED)
        table.add_column("Setting", style=f"{self.theme.accent} bold")
        table.add_column("Value", style="white")
        table.add_row("Enabled providers", ", ".join(self.enabled_providers))
        table.add_row("Default provider", config.provider)
        table.add_row("Model", config.model)
        table.add_row("Sub-agent model", config.subagent_model or "—")
        table.add_row("OpenRouter", self._mask_secret(config.openrouter_api_key))
        table.add_row("OpenAI", self._mask_secret(config.openai_api_key))
        table.add_row("Anthropic", self._mask_secret(config.anthropic_api_key))
        table.add_row("Ollama", config.ollama_base_url or "—")
        table.add_row("GitHub", self._mask_secret(config.github_token))
        table.add_row("NVIDIA", self._mask_secret(config.nvidia_api_key))
        table.add_row("xAI / Grok", self._mask_secret(config.xai_api_key))
        table.add_row("Groq", self._mask_secret(config.groq_api_key))
        table.add_row("DeepSeek", self._mask_secret(config.deepseek_api_key))
        table.add_row("Together AI", self._mask_secret(config.together_api_key))
        table.add_row("Perplexity", self._mask_secret(config.perplexity_api_key))
        table.add_row("LM Studio", config.lmstudio_base_url or "—")
        table.add_row("vLLM", config.vllm_base_url or "—")
        table.add_row("Azure endpoint", config.azure_openai_endpoint or "—")
        table.add_row("Azure key", self._mask_secret(config.azure_openai_api_key))
        table.add_row("Gemini", self._mask_secret(config.gemini_api_key))
        table.add_row("Mistral", self._mask_secret(config.mistral_api_key))
        table.add_row(
            "AWS Bedrock",
            "env credentials" if "bedrock" in self.enabled_providers else "—",
        )
        table.add_row("Fireworks", self._mask_secret(config.fireworks_api_key))
        table.add_row("Cohere", self._mask_secret(config.cohere_api_key))
        table.add_row("Sessions dir", str(config.sessions_dir))
        table.add_row("Max iterations", str(config.max_iterations))
        table.add_row("Safe mode", "on" if config.safe_mode else "off")
        self.console.print()
        self.console.print(table)

    async def _choose_default_provider(self, current: str) -> str:
        """Prompt the user to select the default provider from enabled ones.

        Accepts a number, a provider name, or Enter to keep ``current``.

        Args:
            current: The currently active provider name.

        Returns:
            The selected provider name.
        """
        while True:
            self.console.print(
                Text(
                    "\nChoose the default provider for the main REPL:",
                    style=self.theme.muted,
                )
            )
            for idx, provider in enumerate(self.enabled_providers, start=1):
                marker = "▶" if provider == current else "•"
                self.console.print(
                    f"  {idx}. {marker} {_PROVIDER_LABELS[provider]} ({provider})"
                )
            raw = (await self._prompt_text("default provider", current)).strip().lower()
            if not raw:
                if current in self.enabled_providers:
                    return current
                return self.enabled_providers[0]
            if raw.isdigit() and 1 <= int(raw) <= len(self.enabled_providers):
                return self.enabled_providers[int(raw) - 1]
            if raw in self.enabled_providers:
                return raw
            self.console.print(
                Text("Invalid provider selection.", style=f"bold {self.theme.err}")
            )

    async def _prompt_text(self, label: str, default: str | None = None) -> str:
        """Display a labeled text prompt and return the user's input.

        Returns ``default`` when the user submits an empty response.

        Args:
            label: The prompt label shown to the user.
            default: Value returned on empty input.

        Returns:
            The entered text, or ``default`` if nothing was typed.
        """
        suffix = f" [{default}]" if default else ""
        result = await self.session.prompt_async(
            [
                ("class:wizard.label", f"{label}"),
                ("class:wizard.default", suffix),
                ("class:wizard.input", "> "),
            ],
            complete_style=CompleteStyle.COLUMN,
        )
        return result.strip() or (default or "")

    async def _int_prompt(self, label: str, default: int) -> int:
        """Prompt for an integer value, retrying until a valid integer is entered.

        Args:
            label: The prompt label shown to the user.
            default: Value used when the user submits an empty response.

        Returns:
            The parsed integer.
        """
        while True:
            value = await self._prompt_text(label, str(default))
            try:
                return int(value)
            except ValueError:
                self.console.print(
                    Text(
                        "Please enter a valid integer.", style=f"bold {self.theme.err}"
                    )
                )

    async def _secret_prompt(
        self, label: str, default: str | None = None
    ) -> str | None:
        """Prompt for a secret value (e.g. API key) with masked input.

        Displays a masked version of the current value as the default hint.
        Returns the existing value unchanged when the user submits nothing.

        Args:
            label: The prompt label shown to the user.
            default: Existing secret value; shown masked, returned on empty input.

        Returns:
            The entered secret, or ``default`` if nothing was typed.
        """
        masked = self._mask_secret(default)
        suffix = f" [{masked}]" if default else ""
        result = await self.session.prompt_async(
            [
                ("class:wizard.label", f"{label}"),
                ("class:wizard.default", suffix),
                ("class:wizard.input", "> "),
            ],
            is_password=True,
        )
        result = result.strip()
        return result or default

    async def _confirm(self, label: str, default: bool = True) -> bool:
        """Prompt for a yes/no confirmation.

        Args:
            label: Question text shown to the user.
            default: Value returned when the user submits an empty response.

        Returns:
            ``True`` for affirmative responses, ``False`` otherwise.
        """
        suffix = "Y/n" if default else "y/N"
        result = await self._prompt_text(f"{label} ({suffix})", "")
        value = result.strip().lower()
        if not value:
            return default
        return value in {"y", "yes", "1", "true", "on"}

    def _mask_secret(self, value: str | None) -> str:
        """Return a masked representation of a secret string.

        Shows the first and last 4 characters for secrets longer than 8
        characters; replaces everything else with bullet characters.

        Args:
            value: The secret to mask, or ``None``.

        Returns:
            A masked string, or ``"—"`` when ``value`` is absent.
        """
        if not value:
            return "—"
        if len(value) <= 8:
            return "•" * len(value)
        return f"{value[:4]}{'•' * (len(value) - 8)}{value[-4:]}"

    def _infer_enabled_providers(self, config: PhosonConfig) -> list[str]:
        """Infer which providers are likely enabled from the config.

        A provider is considered enabled when its credential is present *or*
        it is the currently configured active provider (so the wizard always
        shows at least one entry pre-selected).

        Args:
            config: The current configuration to inspect.

        Returns:
            Ordered list of enabled provider names.
        """
        enabled: list[str] = []
        if config.openrouter_api_key or config.provider == "openrouter":
            enabled.append("openrouter")
        if config.openai_api_key or config.provider == "openai":
            enabled.append("openai")
        if config.anthropic_api_key or config.provider == "anthropic":
            enabled.append("anthropic")
        if config.ollama_base_url or config.provider == "ollama":
            enabled.append("ollama")
        if config.github_token or config.provider == "github":
            enabled.append("github")
        if config.nvidia_api_key or config.provider == "nvidia":
            enabled.append("nvidia")
        if config.xai_api_key or config.provider in ("xai", "grok"):
            enabled.append("xai")
        if config.groq_api_key or config.provider == "groq":
            enabled.append("groq")
        if config.deepseek_api_key or config.provider == "deepseek":
            enabled.append("deepseek")
        if config.together_api_key or config.provider == "together":
            enabled.append("together")
        if config.perplexity_api_key or config.provider == "perplexity":
            enabled.append("perplexity")
        if config.lmstudio_base_url or config.provider == "lmstudio":
            enabled.append("lmstudio")
        if config.vllm_base_url or config.vllm_api_key or config.provider == "vllm":
            enabled.append("vllm")
        if config.azure_openai_api_key or config.provider == "azure":
            enabled.append("azure")
        if config.gemini_api_key or config.provider in ("gemini", "google"):
            enabled.append("gemini")
        if config.mistral_api_key or config.provider == "mistral":
            enabled.append("mistral")
        if config.provider in ("bedrock", "aws"):
            enabled.append("bedrock")
        if config.fireworks_api_key or config.provider == "fireworks":
            enabled.append("fireworks")
        if config.cohere_api_key or config.provider == "cohere":
            enabled.append("cohere")
        return enabled or [config.provider]


async def run_install_wizard(config: PhosonConfig | None = None) -> PhosonConfig:
    wizard = SetupWizard(config=config)
    return await wizard.run()
