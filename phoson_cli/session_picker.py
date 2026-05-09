"""Interactive session picker with pagination."""

from typing import Any
from dataclasses import dataclass

from .pickers import BasePicker, picker_style


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
    sessions: list[Any],
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
    sessions: list[Any],
    current_id: str,
    page_size: int = 15,
) -> SessionPickerResult:
    """Show an interactive session picker. Returns the selected session_id or None."""
    if not sessions:
        return SessionPickerResult(cancelled=True)

    state = {"selected": 0, "page": 0}
    total_pages = max(1, (len(sessions) + page_size - 1) // page_size)

    def render() -> list[tuple[str, str]]:
        return _render_sessions(
            sessions, current_id, state["selected"], state["page"], page_size
        )

    picker: BasePicker[SessionPickerResult] = BasePicker(
        render=render,
        style=picker_style(),
    )

    def _sync_page_to_selection() -> None:
        state["page"] = state["selected"] // page_size

    def go_up() -> None:
        if state["selected"] > 0:
            state["selected"] -= 1
            _sync_page_to_selection()
            picker.refresh()

    def go_down() -> None:
        if state["selected"] < len(sessions) - 1:
            state["selected"] += 1
            _sync_page_to_selection()
            picker.refresh()

    def page_up() -> None:
        if state["page"] > 0:
            state["page"] -= 1
            state["selected"] = state["page"] * page_size
            picker.refresh()

    def page_down() -> None:
        if state["page"] < total_pages - 1:
            state["page"] += 1
            state["selected"] = min(
                state["page"] * page_size, len(sessions) - 1
            )
            picker.refresh()

    def confirm() -> None:
        picker.done(
            SessionPickerResult(session_id=sessions[state["selected"]].id)
        )

    def cancel() -> None:
        picker.done(SessionPickerResult(cancelled=True))

    def delete_selected() -> None:
        sid = sessions[state["selected"]].id
        picker.done(SessionPickerResult(session_id=sid, delete=True))

    picker.bind_default_nav(
        on_up=go_up, on_down=go_down, on_enter=confirm, on_cancel=cancel
    )
    picker.bind("pageup", page_up)
    picker.bind("pagedown", page_down)
    picker.bind("q", cancel)
    picker.bind("d", delete_selected)

    return await picker.run()
