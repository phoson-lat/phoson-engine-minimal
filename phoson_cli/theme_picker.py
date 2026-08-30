"""Theme picker — interactive selector for the CLI color theme (E4).

One row per theme tier, and a live preview of the selected tier: the
welcome banner (art + wordmark) and a swatch strip of the named tokens,
both rendered with the *previewed* theme's own colors into an ANSI
string — WYSIWYG, even though the picker's frame chrome keeps the
currently-active theme's palette (the frame chrome cannot change
per-frame without the host re-styling the whole application).

The picker is a :class:`~phoson_cli.pickers.BasePicker` exactly like the
model/provider/session pickers, so both front ends reuse it: the
full-screen TUI hosts it as a Float, the classic REPL runs it as its own
full-screen application.
"""

import io
from dataclasses import dataclass
from collections.abc import Callable

from rich.text import Text
from rich.columns import Columns
from rich.console import Console
from prompt_toolkit.formatted_text import ANSI, to_formatted_text

from .theme import Theme, ThemeRegistry, default_theme_registry
from ._views import _PHOS_ART
from .pickers import BasePicker, picker_style

#: (row label, one-line description) for each tier, in picker order.
_THEME_ROWS: tuple[tuple[str, str], ...] = (
    ("dark", "default, purple on dark"),
    ("light", "light background"),
    ("ansi", "16-color SSH-safe"),
    ("no-color", "plain text"),
)

#: Named tokens shown in the preview swatch strip.
_TOKEN_SWATCHES: tuple[str, ...] = (
    "text",
    "muted",
    "accent",
    "ok",
    "err",
    "warn",
    "reasoning",
)

#: Banner preview render width (narrow enough for the picker frame).
_BANNER_PREVIEW_WIDTH = 52


@dataclass
class ThemePickerResult:
    """Outcome of the theme picker."""

    theme_name: str | None = None
    cancelled: bool = False


def _banner_preview_renderable(theme: Theme):
    """Art + wordmark column, the same composition as the real banner
    minus the meta lines — compact enough to sit inside the picker."""
    art = Text(_PHOS_ART, style=theme.art)

    art_lines = _PHOS_ART.splitlines()
    mid = len(art_lines) // 2
    word_lines: list[str] = [""] * len(art_lines)
    word_lines[mid - 1] = "phoson"
    word_lines[mid] = "terminal agent"
    wordmark = Text("\n".join(word_lines))
    wordmark.highlight_words(["phoson"], style=f"bold {theme.accent}")
    wordmark.highlight_words(["terminal agent"], style=theme.muted)
    return Columns([art, wordmark], padding=(0, 4))


def _render_banner_preview(theme: Theme) -> list[str]:
    """Render the banner preview with *theme* into ANSI lines.

    Rich prints into a throwaway console (the same bridge the full-screen
    chat pane uses), so the preview shows the theme's real colors no
    matter which theme the terminal is currently displaying.
    """
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=_BANNER_PREVIEW_WIDTH,
        highlight=False,
    )
    console.print(_banner_preview_renderable(theme))
    # Drop the leading blank line so the preview hugs the top.
    lines = buf.getvalue().splitlines()
    if lines and not lines[0].strip():
        lines = lines[1:]
    return lines


def _render_token_strip(theme: Theme) -> list[tuple[str, str]]:
    """Two-line swatch strip: colored cells on top, token names below.

    Each cell is styled *directly* (style strings, not style classes) so
    the strip renders the previewed theme's colors while the rest of the
    frame keeps the active theme's palette.
    """
    if theme.name == "no-color":
        return [("class:row", "  (no color — plain text only)\n")]

    cells: list[tuple[str, str]] = []
    for label in _TOKEN_SWATCHES:
        color = getattr(theme, label)
        if not color:
            cells.append(("class:row", "   "))
        else:
            style = f"bold {color}" if label == "accent" else color
            cells.append((style, "   "))
    swatch_row = "".join(text for _style, text in cells)
    labels_row = "  ".join(label.ljust(3) for label in _TOKEN_SWATCHES)
    return [
        ("class:row", f"  {swatch_row}\n"),
        ("class:header", f"  {labels_row}\n"),
    ]


def _render_frame(
    themes: tuple[Theme, ...],
    rows: tuple[tuple[str, str], ...],
    current_name: str,
    selected: int,
    detected_name: str | None,
) -> list[tuple[str, str]]:
    """Render the entire picker frame for the current selection."""
    out: list[tuple[str, str]] = []
    out.append(("class:title", "  Theme\n"))
    out.append(("class:header", "  " + "─" * 46 + "\n"))

    for i, theme in enumerate(themes):
        is_selected = i == selected
        is_current = theme.name == current_name
        style = (
            "class:row.selected"
            if is_selected
            else ("class:row.active" if is_current else "class:row")
        )
        marker = "▸" if is_selected else ("▶" if is_current else " ")
        label, description = rows[i]
        hints = []
        if is_current:
            hints.append("current")
        if detected_name and theme.name == detected_name:
            hints.append("detected")
        suffix = f"   ({' · '.join(hints)})" if hints else ""
        out.append(
            (style, f"  {marker} {i + 1:>2}  {label:<9} {description}{suffix}\n")
        )

    # ── Live preview of the selected theme ──────────────────────────────
    selected_theme = themes[selected]
    out.append(("class:search.label", f"  Preview — {selected_theme.name}\n"))
    out.append(("class:header", "  " + "─" * 46 + "\n"))
    for line in _render_banner_preview(selected_theme):
        # Parse the SGR escapes into (style, text) fragments — raw
        # escape sequences inside a plain fragment are displayed
        # literally instead of being interpreted.
        parsed = to_formatted_text(ANSI(line + "\n"))
        for item in parsed:
            out.append((str(item[0]), str(item[1])))
    out.extend(_render_token_strip(selected_theme))
    out.append(("\n", ""))
    out.append(
        (
            "class:footer",
            "  ↑/↓ navigate  ·  Enter select  ·  Esc cancel\n",
        )
    )
    return out


def build_theme_picker(
    current_name: str,
    *,
    theme: "Theme | None" = None,
    registry: ThemeRegistry | None = None,
    detected_name: str | None = None,
    on_done: Callable[[ThemePickerResult], None] | None = None,
    invalidate: Callable[[], None] | None = None,
) -> BasePicker[ThemePickerResult]:
    """Build the theme picker without running it.

    Args:
        current_name: The active tier (marks the "current" row).
        theme: Theme used for the frame chrome; resolved via
            ``load_theme()`` when None.
        detected_name: Tier the terminal probe suggested, if any (marks
            the "detected" row).
        on_done: Float-mode result callback (see :class:`BasePicker`).
        invalidate: Float-mode repaint callback (see :class:`BasePicker`).
    """
    active_registry = registry or default_theme_registry()
    rows = active_registry.rows()
    themes = tuple(active_registry.get(name) for name, _ in rows)
    assert all(theme is not None for theme in themes)
    themes = tuple(theme for theme in themes if theme is not None)
    state: dict[str, int] = {
        "selected": next((i for i, t in enumerate(themes) if t.name == current_name), 0)
    }

    picker: BasePicker[ThemePickerResult] = BasePicker(
        render=lambda: _render_frame(
            themes, rows, current_name, state["selected"], detected_name
        ),
        style=picker_style(theme=theme),
        on_done=on_done,
        invalidate=invalidate,
    )

    picker.bind_list_nav(
        get_len=lambda: len(themes),
        get_sel=lambda: state["selected"],
        set_sel=lambda i: state.update(selected=i),
        on_enter=lambda: picker.done(
            ThemePickerResult(theme_name=themes[state["selected"]].name)
        ),
        on_cancel=lambda: picker.done(ThemePickerResult(cancelled=True)),
    )

    return picker


async def pick_theme(
    current_name: str,
    *,
    theme: "Theme | None" = None,
    registry: ThemeRegistry | None = None,
    detected_name: str | None = None,
) -> ThemePickerResult:
    """Run the theme picker as its own full-screen application (classic)."""
    return await build_theme_picker(
        current_name,
        theme=theme,
        registry=registry,
        detected_name=detected_name,
    ).run()


__all__ = [
    "ThemePickerResult",
    "build_theme_picker",
    "pick_theme",
    "_THEME_ROWS",
    "_render_banner_preview",
    "_render_token_strip",
    "_render_frame",
]
