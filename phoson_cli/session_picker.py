"""Interactive session picker with pagination."""

from typing import TypedDict
from dataclasses import dataclass
from collections.abc import Callable

from phoson_agent.sessions.models import SessionMeta

from .theme import Theme
from .pickers import BasePicker, picker_style


class _SessionState(TypedDict):
    selected: int
    page: int
    marked: set[str]


@dataclass
class SessionPickerResult:
    session_id: str | None = None
    cancelled: bool = False
    delete: bool = False
    #: Session ids to delete (multi-delete mode); empty unless delete_many.
    delete_ids: list[str] | None = None


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
    marked: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Render the session list as prompt_toolkit-formatted text."""
    marked = marked or set()
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
        title = getattr(s, "title", None)

        is_current = str(s.id).startswith(current_id[:4])
        is_selected = i == selected

        if is_selected:
            style = "class:row.selected"
        elif is_current:
            style = "class:row.active"
        else:
            style = "class:row"

        marker = "▸" if is_selected else ("▶" if is_current else " ")
        if str(s.id) in marked:
            marker = f"{marker}✓"
        state = "active" if is_current else "saved"

        line = (
            f"  {marker} {idx:>2}  {sid:<10} {msgs:>5}"
            f"  {updated:<16} {state:<8} {cost:>8}"
        )
        if title:
            line += f"  [{title[:24]}]"
        lines.append((style, line + "\n"))

    # Footer
    total_pages = (len(sessions) + page_size - 1) // page_size
    page_info = f" Page {page + 1}/{total_pages} " if total_pages > 1 else ""
    marked_note = f"  {len(marked)} marked for delete" if marked else ""
    lines.append(("\n", ""))
    lines.append(
        (
            "class:footer",
            f"  {page_info}↑/↓ navigate  ·  Enter select  ·  q cancel"
            f"  ·  d delete  ·  space mark  ·  X delete marked (asks){marked_note}\n",
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
    return await build_session_picker(sessions, current_id, page_size, theme).run()


def build_session_picker(
    sessions: list[SessionMeta],
    current_id: str,
    page_size: int = 15,
    theme: "Theme | None" = None,
    *,
    on_done: Callable[[SessionPickerResult], None] | None = None,
    invalidate: Callable[[], None] | None = None,
) -> BasePicker[SessionPickerResult]:
    """Build the picker's state/renderer/bindings without running it.

    Lets a host embed it as a Float (:meth:`BasePicker.as_float`) instead
    of it spinning up its own full-screen ``Application`` via ``run()``
    (``pick_session`` above does that for the classic REPL).
    """
    state: _SessionState = {"selected": 0, "page": 0, "marked": set()}

    picker: BasePicker[SessionPickerResult] = BasePicker(
        render=lambda: _render_sessions(
            sessions,
            current_id,
            state["selected"],
            state["page"],
            page_size,
            marked=state["marked"],
        ),
        style=picker_style(theme=theme),
        on_done=on_done,
        invalidate=invalidate,
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

    def _toggle_mark() -> None:
        """Space: (un)mark the selected session for multi-delete."""
        sid = str(sessions[state["selected"]].id)
        if sid in state["marked"]:
            state["marked"].discard(sid)
        else:
            if sid == str(current_id):
                return  # current active session can't be deleted
            state["marked"].add(sid)
        picker.refresh()

    def _delete_marked() -> None:
        """X: delete all marked sessions; the picker stays open."""
        if not state["marked"]:
            return
        picker.done(
            SessionPickerResult(
                session_id=None,
                delete_ids=sorted(state["marked"]),
            )
        )
        state["marked"].clear()

    picker.bind("space", _toggle_mark)

    # X (shift+x): delete all marked sessions without closing — after the
    # host applies the deletes it reopens/re-renders with a fresh list.
    picker.bind("X", _delete_marked)

    return picker
