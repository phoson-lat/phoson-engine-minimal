"""Provider picker — interactive selector for the active LLM provider."""

from typing import TypedDict
from dataclasses import dataclass

from .pickers import BasePicker, picker_style


class _ProviderState(TypedDict):
    selected: int


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

    state: _ProviderState = {
        "selected": next(
            (i for i, p in enumerate(providers) if p == current_provider), 0
        )
    }

    picker: BasePicker[ProviderPickerResult] = BasePicker(
        render=lambda: _render_providers(
            providers, current_provider, state["selected"]
        ),
        style=picker_style(),
    )

    picker.bind_list_nav(
        get_len=lambda: len(providers),
        get_sel=lambda: state["selected"],
        set_sel=lambda i: state.update(selected=i),
        on_enter=lambda: picker.done(
            ProviderPickerResult(provider=providers[state["selected"]])
        ),
        on_cancel=lambda: picker.done(ProviderPickerResult(cancelled=True)),
    )

    return await picker.run()
