"""Inline (line-oriented) pickers for the classic REPL.

The classic REPL is line-oriented: the full-screen ``Application`` pickers
(``model_picker``, ``provider_picker``, ``theme_picker``, ``session_picker``)
need an alternate-screen capable terminal. This module provides a
prompt_toolkit *completion-menu* picker that works anywhere the prompt
works — the same mechanism as ``@file`` mentions — so a bare ``/model``,
``/provider``, ``/theme`` or ``/sessions pick`` always presents a dropdown
instead of asking the user to type a value by hand.

Rows are fuzzy-filtered as you type; ``↑``/``↓`` navigate, ``Enter``
confirms the highlighted row and ``Esc`` cancels.
"""

from dataclasses import dataclass

from prompt_toolkit.styles import Style
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings

from .fuzzy import fuzzy_score
from .theme import Theme, load_theme, build_prompt_style

__all__ = ["InlineOption", "pick_inline"]

#: How many rows the completion menu shows at once (it scrolls beyond this).
_MENU_ROWS = 8

_HINT = "  type to filter  ·  ↑/↓ select  ·  Enter confirm  ·  Esc cancel"


@dataclass(frozen=True)
class InlineOption:
    """One selectable row of an inline picker."""

    value: str
    display: str
    meta: str = ""


class _OptionCompleter(Completer):
    """Offer the picker's options as a fuzzy-filtered completion menu."""

    def __init__(self, options: list[InlineOption], current: str | None) -> None:
        self._options = options
        self._current = current

    def get_completions(self, document, complete_event):
        query = document.text_before_cursor.strip()
        scored: list[tuple[int, InlineOption]] = []
        for option in self._options:
            haystack = f"{option.value} {option.display} {option.meta}"
            score = fuzzy_score(query, haystack)
            if score is None:
                continue
            scored.append((score, option))
        scored.sort(key=lambda item: (-item[0], item[1].value.lower()))

        for _score, option in scored:
            marker = "▶ " if option.value == self._current else "  "
            yield Completion(
                option.value,
                start_position=-len(document.text_before_cursor),
                display=marker + option.display,
                display_meta=option.meta,
            )


async def pick_inline(
    title: str,
    options: list[InlineOption],
    *,
    current: str | None = None,
    theme: Theme | None = None,
    notice: str | None = None,
) -> str | None:
    """Show an inline completion-menu picker and return the chosen value.

    Args:
        title: Short label shown before the prompt (e.g. ``"model"``).
        options: The selectable rows, in their natural (unfiltered) order.
        current: Value to mark as active (``▶``) and to sort first.
        theme: Active theme; resolved via ``load_theme()`` when ``None``.
        notice: Optional line printed above the prompt (e.g. providers whose
            live listing failed).

    Returns:
        The chosen option's ``value``, or ``None`` when the list is empty or
        the user cancels (``Esc`` / ``Ctrl+C`` / ``Ctrl+D``).
    """
    if not options:
        return None
    if notice:
        print(notice)

    active_theme = theme or load_theme()
    key_bindings = KeyBindings()

    @key_bindings.add("enter")
    def _accept(event) -> None:
        """Confirm the highlighted row (or the typed text)."""
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is not None and state.current_completion is not None:
            buffer.apply_completion(state.current_completion)
        buffer.validate_and_handle()

    @key_bindings.add("escape")
    def _cancel(event) -> None:
        event.app.exit(result="")

    session: PromptSession[str] = PromptSession(
        completer=_OptionCompleter(options, current),
        complete_while_typing=True,
        reserve_space_for_menu=_MENU_ROWS,
        key_bindings=key_bindings,
        style=Style.from_dict(build_prompt_style(active_theme)),
        bottom_toolbar=_HINT,
    )
    try:
        text = await session.prompt_async(
            f"{title} › ",
            # Show the menu (first row highlighted) before any keystroke.
            pre_run=lambda: session.default_buffer.start_completion(select_first=True),
        )
    except (EOFError, KeyboardInterrupt):
        return None

    text = text.strip()
    if not text:
        return None
    for option in options:
        if option.value == text:
            return option.value
    for option in options:
        if option.display == text:
            return option.value
    return None
