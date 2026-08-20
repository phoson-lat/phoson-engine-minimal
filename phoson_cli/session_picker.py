"""Interactive session picker with pagination."""

from typing import TypedDict
from dataclasses import dataclass

from phoson_agent.sessions.models import SessionMeta

from .theme import Theme
from .pickers import BasePicker, picker_style


class _SessionState(TypedDict):
    selected: int
    page: int


@dataclass
class SessionPickerResult:
    session_id: str | None = None
    cancelled: bool = False
    delete: bool = False


_HEADER = (
    f"  {'#':>3}  {'Session ID':<10} {'Msgs':>5}"
    f"  {'Updated':<16} {'State':<8} {'Cost':>8}\n"
)


def _render_sessions(
    sessions: list[SessionMeta],
    current_id: str,
    selected: int,
    page: int,
    page_size: int,
) -> list[tuple[str, str]]:
    """Render the session list as prompt_toolkit-formatted text."""
    lines: list[tuple[str, str]] = []

    lines.append(("class:title", "  Saved Sessions\n"))
    lines.append(("class:header", _HEADER))
    lines.append(("class:header", "  " + "─" * 68 + "\n"))

    start = page * page_size
    end = min(start + page_size, len(sessions))

    for i in range(start, end):
        s = sessions[i]
        idx = i + 1
        sid = str(s.id)[:10]
        msgs = str(s.message_count)
        updated = s.updated_at.strftime("%m-%d %H:%M")
        has_cost = hasattr(s, "total_cost") and s.total_cost
        cost = f"${s.total_cost:.4f}" if has_cost else "—"

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
            f"  {marker} {idx:>2}  {sid:<10} {msgs:>5}"
            f"  {updated:<16} {state:<8} {cost:>8}\n"
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
    sessions: list[SessionMeta],
    current_id: str,
    page_size: int = 15,
    theme: "Theme | None" = None,
) -> SessionPickerResult:
    """Show an interactive session picker. Returns the selected session_id or None."""
    if not sessions:
        return SessionPickerResult(cancelled=True)

    state: _SessionState = {"selected": 0, "page": 0}

    picker: BasePicker[SessionPickerResult] = BasePicker(
        render=lambda: _render_sessions(
            sessions, current_id, state["selected"], state["page"], page_size
        ),
        style=picker_style(theme=theme),
    )

    picker.bind_paged_nav(
        get_len=lambda: len(sessions),
        get_sel=lambda: state["selected"],
        set_sel=lambda i: state.update(selected=i),
        get_page=lambda: state["page"],
        set_page=lambda p: state.update(page=p),
        page_size=page_size,
        on_enter=lambda: picker.done(
            SessionPickerResult(session_id=sessions[state["selected"]].id)
        ),
        on_cancel=lambda: picker.done(SessionPickerResult(cancelled=True)),
    )
    picker.bind("q", lambda: picker.done(SessionPickerResult(cancelled=True)))
    picker.bind(
        "d",
        lambda: picker.done(
            SessionPickerResult(session_id=sessions[state["selected"]].id, delete=True)
        ),
    )

    return await picker.run()
