from dataclasses import dataclass

from prompt_toolkit.styles import Style
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import HSplit, Window

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


_PROVIDER_PICKER_STYLE = Style.from_dict(
    {
        "title": "bold #b57bee",
        "header": "#808080",
        "row.selected": "bg:#3d2b6e bold #ffffff",
        "row": "#9a8faa",
        "row.active": "bold #00ff9c",
        "footer": "#5a5a5a",
    }
)


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
    if not providers:
        return ProviderPickerResult(cancelled=True)

    selected = next(
        (i for i, provider in enumerate(providers) if provider == current_provider),
        0,
    )

    def get_text() -> list[tuple[str, str]]:
        return _render_providers(providers, current_provider, selected)

    kb = KeyBindings()

    @kb.add("up")
    def _up(_event) -> None:
        nonlocal selected
        if selected > 0:
            selected -= 1
            info_window.content = FormattedTextControl(get_text)

    @kb.add("down")
    def _down(_event) -> None:
        nonlocal selected
        if selected < len(providers) - 1:
            selected += 1
            info_window.content = FormattedTextControl(get_text)

    @kb.add("enter")
    def _select(_event) -> None:
        app.exit(result=ProviderPickerResult(provider=providers[selected]))

    @kb.add("escape")
    def _quit(_event) -> None:
        app.exit(result=ProviderPickerResult(cancelled=True))

    info_window = Window(
        content=FormattedTextControl(get_text),
        always_hide_cursor=True,
    )

    app: Application = Application(
        layout=Layout(HSplit([info_window])),
        key_bindings=kb,
        full_screen=True,
        style=_PROVIDER_PICKER_STYLE,
        mouse_support=False,
    )
    return await app.run_async()
