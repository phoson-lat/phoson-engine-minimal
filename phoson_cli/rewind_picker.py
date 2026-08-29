"""Rewind picker — pick which earlier user message to jump back to (G1).

The full-screen double-Esc rewind flow (IMPROVEMENTS.md G1, issue #51)
opens this as a modal Float inside the running TUI: it lists the user
turns of the active conversation path (newest first, so the initial
cursor sits on the most recent turn — the most likely rewind target)
and lands the cursor on the node *before* the selected one, so the next
message replaces the selected turn and everything after it — the same
UX as Claude Code's double-Esc. The discarded messages stay in the tree
as an abandoned branch (visible via ``/tree``); nothing is deleted.

The classic front end reuses the same picker via ``run()`` (a full-screen
prompt_toolkit Application), exactly like the session picker.
"""

from typing import TypedDict
from dataclasses import dataclass
from collections.abc import Callable

from .theme import Theme
from .pickers import BasePicker, picker_style


@dataclass
class RewindPickerResult:
    #: Node id the user landed on (the node *before* the selected user
    #: turn), or ``None`` when the picker was cancelled.
    node_id: str | None = None
    cancelled: bool = False


class _RewindState(TypedDict):
    selected: int
    page: int


def _render_rewinds(
    candidates: list[tuple[str, str]],
    selected: int,
    page: int,
    page_size: int,
) -> list[tuple[str, str]]:
    """Render the rewind list as prompt_toolkit-formatted text."""
    lines: list[tuple[str, str]] = []
    lines.append(("class:title", "  Jump back to an earlier message\n"))
    lines.append(("class:header", "  " + "─" * 68 + "\n"))

    start = page * page_size
    end = min(start + page_size, len(candidates))

    for i in range(start, end):
        node_id, preview = candidates[i]
        idx = i + 1
        marker = "▸" if i == selected else " "
        style = "class:row.selected" if i == selected else "class:row"
        lines.append((style, f"  {marker} {idx:>2}. {node_id[:8]}  {preview}\n"))

    lines.append(("\n", ""))
    total_pages = max(1, (len(candidates) + page_size - 1) // page_size)
    page_info = f" Page {page + 1}/{total_pages} " if total_pages > 1 else ""
    lines.append(
        (
            "class:footer",
            f"  {page_info}↑/↓ navigate  ·  Enter jump  ·  Esc cancel\n",
        )
    )
    return lines


async def pick_rewind(
    candidates: list[tuple[str, str]],
    page_size: int = 15,
    theme: "Theme | None" = None,
) -> RewindPickerResult:
    """Show the rewind picker (classic front end) and return the result."""
    if not candidates:
        return RewindPickerResult(cancelled=True)
    return await build_rewind_picker(candidates, page_size, theme).run()


def build_rewind_picker(
    candidates: list[tuple[str, str]],
    page_size: int = 15,
    theme: "Theme | None" = None,
    *,
    on_done: Callable[[RewindPickerResult], None] | None = None,
    invalidate: Callable[[], None] | None = None,
) -> BasePicker[RewindPickerResult]:
    """Build the picker's state/renderer/bindings without running it.

    Lets the full-screen host embed it as a Float
    (:meth:`BasePicker.as_float`) instead of spinning up its own
    full-screen ``Application`` via ``run()`` (``pick_rewind`` above does
    that for the classic REPL).

    Args:
        candidates: ``(node_id, preview)`` pairs, newest first, from
            ``SessionController.jump_candidates()``.
        page_size: Rows per page.
        theme: The active theme (resolved via ``load_theme()`` when None).
        on_done: Float-mode callback (see :class:`BasePicker`).
        invalidate: Float-mode invalidation callback.
    """
    state: _RewindState = {"selected": 0, "page": 0}

    picker: BasePicker[RewindPickerResult] = BasePicker(
        render=lambda: _render_rewinds(
            candidates, state["selected"], state["page"], page_size
        ),
        style=picker_style(theme=theme),
        on_done=on_done,
        invalidate=invalidate,
    )

    picker.bind_paged_nav(
        get_len=lambda: len(candidates),
        get_sel=lambda: state["selected"],
        set_sel=lambda i: state.update(selected=i),
        get_page=lambda: state["page"],
        set_page=lambda p: state.update(page=p),
        page_size=page_size,
        on_enter=lambda: picker.done(
            RewindPickerResult(node_id=candidates[state["selected"]][0])
        ),
        on_cancel=lambda: picker.done(RewindPickerResult(cancelled=True)),
    )
    picker.bind("q", lambda: picker.done(RewindPickerResult(cancelled=True)))

    return picker


__all__ = ["RewindPickerResult", "build_rewind_picker", "pick_rewind"]
