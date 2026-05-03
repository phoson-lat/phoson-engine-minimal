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

from .config import PhosonConfig, save_config
from .model_selector import list_available_models

_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "ollama": "Ollama",
}

_PROMPT_STYLE = Style.from_dict(
    {
        "": "#9a8faa",
        "wizard.label": "#b57bee bold",
        "wizard.default": "#6f6780",
        "wizard.input": "#e0d0ff",
    }
)

_PHOS_ART = (
    (Path(__file__).parent / "phos-ascii.txt").read_text(encoding="utf-8").rstrip("\n")
)


class SetupWizard:
    def __init__(
        self, config: PhosonConfig | None = None, console: Console | None = None
    ):
        self.console = console or Console(highlight=False)
        self.config = config or PhosonConfig()
        self.enabled_providers = self._infer_enabled_providers(self.config)
        self.session = PromptSession(style=_PROMPT_STYLE)

    async def run(self) -> PhosonConfig:
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
                    border_style="medium_spring_green",
                )
            )
            self.config = updated
        else:
            self.console.print(
                Panel.fit("Configuration not saved.", border_style="gold3")
            )
        return self.config

    def _print_banner(self) -> None:
        art = Text(_PHOS_ART, style="medium_purple1 bold")
        subtitle = Text()
        subtitle.append("phoson setup wizard\n", style="bold medium_purple1")
        subtitle.append(
            "configure multiple providers, defaults, and secrets",
            style="grey58",
        )
        self.console.print()
        self.console.print(Panel.fit(art, border_style="plum4", box=box.SQUARE))
        self.console.print(subtitle)
        self.console.print(Rule(style="plum4"))

    def _print_intro(self) -> None:
        welcome = (
            "[bold]Welcome[/bold] — this wizard lets you enable one or more "
            "providers,\n"
            "store API credentials, and choose default models for the main agent\n"
            "and sub-agents."
        )
        self.console.print(
            Panel(
                welcome,
                border_style="grey35",
                box=box.ROUNDED,
            )
        )

    async def _pick_enabled_providers(self) -> None:
        providers = ["openrouter", "openai", "anthropic", "ollama"]
        selected = set(self.enabled_providers)

        while True:
            self.console.print()
            self.console.print(Text("Enable providers", style="bold medium_purple1"))
            for idx, provider in enumerate(providers, start=1):
                marker = "[x]" if provider in selected else "[ ]"
                state_style = (
                    "medium_spring_green" if provider in selected else "grey58"
                )
                line = Text(f"  {idx}. ")
                line.append(f"{marker} ", style=state_style)
                line.append(_PROVIDER_LABELS[provider], style="white")
                self.console.print(line)
            self.console.print(
                Text(
                    "\nType numbers to toggle (e.g. 1 3), Enter to continue.",
                    style="grey50",
                )
            )
            raw = (await self._prompt_text("providers", default="")).strip()
            if not raw:
                if selected:
                    self.enabled_providers = [p for p in providers if p in selected]
                    return
                self.console.print(
                    "[indian_red1]Select at least one provider.[/indian_red1]"
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
        self.console.print()
        self.console.print(Text("Provider credentials", style="bold medium_purple1"))

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
        return config

    async def _configure_defaults(self, config: PhosonConfig) -> PhosonConfig:
        self.console.print()
        self.console.print(
            Text("Default runtime selection", style="bold medium_purple1")
        )

        default_provider = await self._choose_default_provider(config.provider)
        config.provider = default_provider

        models = await list_available_models(config)
        suggested = [option.id for option in models[:8]]

        if suggested:
            table = Table(box=box.SIMPLE_HEAD, border_style="plum4")
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
        self.console.print()
        self.console.print(Text("Runtime options", style="bold medium_purple1"))
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
        table = Table(title="Phoson configuration summary", box=box.ROUNDED)
        table.add_column("Setting", style="medium_purple1 bold")
        table.add_column("Value", style="white")
        table.add_row("Enabled providers", ", ".join(self.enabled_providers))
        table.add_row("Default provider", config.provider)
        table.add_row("Model", config.model)
        table.add_row("Sub-agent model", config.subagent_model or "—")
        table.add_row("OpenRouter", self._mask_secret(config.openrouter_api_key))
        table.add_row("OpenAI", self._mask_secret(config.openai_api_key))
        table.add_row("Anthropic", self._mask_secret(config.anthropic_api_key))
        table.add_row("Ollama", config.ollama_base_url or "—")
        table.add_row("Sessions dir", str(config.sessions_dir))
        table.add_row("Max iterations", str(config.max_iterations))
        table.add_row("Safe mode", "on" if config.safe_mode else "off")
        self.console.print()
        self.console.print(table)

    async def _choose_default_provider(self, current: str) -> str:
        while True:
            self.console.print(
                Text(
                    "\nChoose the default provider for the main REPL:",
                    style="grey50",
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
            self.console.print("[indian_red1]Invalid provider selection.[/indian_red1]")

    async def _prompt_text(self, label: str, default: str | None = None) -> str:
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
        while True:
            value = await self._prompt_text(label, str(default))
            try:
                return int(value)
            except ValueError:
                self.console.print(
                    "[indian_red1]Please enter a valid integer.[/indian_red1]"
                )

    async def _secret_prompt(
        self, label: str, default: str | None = None
    ) -> str | None:
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
        suffix = "Y/n" if default else "y/N"
        result = await self._prompt_text(f"{label} ({suffix})", "")
        value = result.strip().lower()
        if not value:
            return default
        return value in {"y", "yes", "1", "true", "on"}

    def _mask_secret(self, value: str | None) -> str:
        if not value:
            return "—"
        if len(value) <= 8:
            return "•" * len(value)
        return f"{value[:4]}{'•' * (len(value) - 8)}{value[-4:]}"

    def _infer_enabled_providers(self, config: PhosonConfig) -> list[str]:
        enabled: list[str] = []
        if config.openrouter_api_key or config.provider == "openrouter":
            enabled.append("openrouter")
        if config.openai_api_key or config.provider == "openai":
            enabled.append("openai")
        if config.anthropic_api_key or config.provider == "anthropic":
            enabled.append("anthropic")
        if config.ollama_base_url or config.provider == "ollama":
            enabled.append("ollama")
        return enabled or [config.provider]


async def run_install_wizard(config: PhosonConfig | None = None) -> PhosonConfig:
    wizard = SetupWizard(config=config)
    return await wizard.run()
