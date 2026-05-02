"""Interactive session picker with pagination."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style


@dataclass
class SessionPickerResult:
    session_id: str | None = None
    cancelled: bool = False
    delete: bool = False


# ── Style ─────────────────────────────────────────────────────────────────────
_SESSION_PICKER_STYLE = Style.from_dict(
    {
        "title": "bold #b57bee",
        "header": "#808080",
        "row.selected": "bg:#3d2b6e bold #ffffff",
        "row": "#9a8faa",
        "row.active": "bold #00ff9c",
        "footer": "#5a5a5a",
        "key-hint": "bold #b57bee",
    }
)


def _render_sessions(
    sessions: list[Any],
    current_id: str,
    selected: int,
    page: int,
    page_size: int,
) -> list[tuple[str, str]]:
    """Render the session list as prompt_toolkit-formatted text."""
    lines: list[tuple[str, str]] = []

    lines.append(("class:title", "  Saved Sessions\n"))
    lines.append(
        (
            "class:header",
            f"  {'#':>3}  {'Session ID':<10} {'Msgs':>5}  {'Updated':<16} {'State':<8} {'Cost':>8}\n",
        )
    )
    lines.append(("class:header", "  " + "─" * 68 + "\n"))

    start = page * page_size
    end = min(start + page_size, len(sessions))

    for i in range(start, end):
        s = sessions[i]
        idx = i + 1
        sid = str(s.id)[:10]
        msgs = str(s.message_count)
        updated = s.updated_at.strftime("%m-%d %H:%M")
        cost = f"${s.total_cost:.4f}" if hasattr(s, "total_cost") and s.total_cost else "—"

        is_current = str(s.id).startswith(current_id[:4])
        is_selected = i == selected

        if is_selected:
            style = "class:row.selected"
        elif is_current:
            style = "class:row.active"
        else:
            style = "class:row"

        marker = "▸" if is_selected else ("▶" if is_current else " ")
        state = "active" if is_current else "saved"

        line = (
            f"  {marker} {idx:>2}  {sid:<10} {msgs:>5}  {updated:<16} {state:<8} {cost:>8}\n"
        )
        lines.append((style, line))

    # Footer
    total_pages = (len(sessions) + page_size - 1) // page_size
    page_info = f" Page {page + 1}/{total_pages} " if total_pages > 1 else ""
    lines.append(("\n", ""))
    lines.append(
        (
            "class:footer",
            f"  {page_info}↑/↓ navigate  ·  Enter select  ·  q cancel  ·  d delete\n",
        )
    )

    return lines


async def pick_session(
    sessions: list[Any],
    current_id: str,
    page_size: int = 15,
) -> SessionPickerResult:
    """Show an interactive session picker. Returns the selected session_id or None."""
    if not sessions:
        return SessionPickerResult(cancelled=True)

    selected = 0
    page = 0
    total_pages = max(1, (len(sessions) + page_size - 1) // page_size)

    def get_text() -> list[tuple[str, str]]:
        return _render_sessions(sessions, current_id, selected, page, page_size)

    kb = KeyBindings()

    @kb.add("up")
    def _up(_event: Any) -> None:
        nonlocal selected, page
        if selected > 0:
            selected -= 1
            new_page = selected // page_size
            if new_page != page:
                page = new_page
            info_window.content = FormattedTextControl(get_text)

    @kb.add("down")
    def _down(_event: Any) -> None:
        nonlocal selected, page
        if selected < len(sessions) - 1:
            selected += 1
            new_page = selected // page_size
            if new_page != page:
                page = new_page
            info_window.content = FormattedTextControl(get_text)

    @kb.add("pageup")
    def _pageup(_event: Any) -> None:
        nonlocal selected, page
        if page > 0:
            page -= 1
            selected = page * page_size
            info_window.content = FormattedTextControl(get_text)

    @kb.add("pagedown")
    def _pagedown(_event: Any) -> None:
        nonlocal selected, page
        if page < total_pages - 1:
            page += 1
            selected = min(page * page_size, len(sessions) - 1)
            info_window.content = FormattedTextControl(get_text)

    @kb.add("enter")
    def _select(_event: Any) -> None:
        app.exit(result=SessionPickerResult(session_id=sessions[selected].id))

    @kb.add("q")
    @kb.add("escape")
    def _quit(_event: Any) -> None:
        app.exit(result=SessionPickerResult(cancelled=True))

    @kb.add("d")
    def _delete(_event: Any) -> None:
        # Mark for deletion — return special result
        app.exit(result=SessionPickerResult(session_id=sessions[selected].id, delete=True))

    info_window = Window(
        content=FormattedTextControl(get_text),
        always_hide_cursor=True,
    )

    layout = Layout(HSplit([info_window]))
    app: Application = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        style=_SESSION_PICKER_STYLE,
        mouse_support=False,
    )

    return await app.run_async()
