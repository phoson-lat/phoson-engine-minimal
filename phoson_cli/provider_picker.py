"""Provider picker — interactive selector for the active LLM provider."""

from dataclasses import dataclass

from .pickers import BasePicker, picker_style

_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "ollama": "Ollama",
}


@dataclass
class ProviderPickerResult:
    provider: str | None = None
    cancelled: bool = False


def _render_providers(
    providers: list[str],
    current_provider: str,
    selected: int,
) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    lines.append(("class:title", "  Providers\n"))
    lines.append(("class:header", "  " + "─" * 42 + "\n"))

    for i, provider in enumerate(providers):
        is_current = provider == current_provider
        is_selected = i == selected

        if is_selected:
            style = "class:row.selected"
        elif is_current:
            style = "class:row.active"
        else:
            style = "class:row"

        marker = "▸" if is_selected else ("▶" if is_current else " ")
        label = _PROVIDER_LABELS.get(provider, provider)
        line = f"  {marker} {i + 1:>2}  {label:<12} ({provider})\n"
        lines.append((style, line))

    lines.append(("\n", ""))
    lines.append(
        (
            "class:footer",
            "  ↑/↓ navigate  ·  Enter select  ·  Esc cancel\n",
        )
    )
    return lines


async def pick_provider(
    providers: list[str],
    current_provider: str,
) -> ProviderPickerResult:
    """Prompt the user for a provider via a full-screen picker."""
    if not providers:
        return ProviderPickerResult(cancelled=True)

    state = {
        "selected": next(
            (i for i, p in enumerate(providers) if p == current_provider),
            0,
        )
    }

    def render() -> list[tuple[str, str]]:
        return _render_providers(providers, current_provider, state["selected"])

    picker: BasePicker[ProviderPickerResult] = BasePicker(
        render=render,
        style=picker_style(),
    )

    def go_up() -> None:
        if state["selected"] > 0:
            state["selected"] -= 1
            picker.refresh()

    def go_down() -> None:
        if state["selected"] < len(providers) - 1:
            state["selected"] += 1
            picker.refresh()

    def confirm() -> None:
        picker.done(ProviderPickerResult(provider=providers[state["selected"]]))

    def cancel() -> None:
        picker.done(ProviderPickerResult(cancelled=True))

    picker.bind_default_nav(
        on_up=go_up, on_down=go_down, on_enter=confirm, on_cancel=cancel
    )

    return await picker.run()
