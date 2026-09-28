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

The picker reuses the REPL's live ``PromptSession`` (see
:func:`set_prompt_session`) rather than creating a second one: a nested
``PromptSession`` is fragile on Windows, where the console input/output is
already claimed by the session that drives the prompt (the same one the
``@file`` menu uses).
"""

from dataclasses import dataclass

from prompt_toolkit.styles import Style
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings

from .fuzzy import fuzzy_score
from .theme import Theme, load_theme, build_prompt_style

__all__ = ["InlineOption", "pick_inline", "set_prompt_session"]

#: How many rows the completion menu shows at once (it scrolls beyond this).
_MENU_ROWS = 6

#: The classic REPL's live prompt session, reused so the dropdown runs on
#: the console input/output the ``@file`` menu already drives.
_prompt_session: PromptSession | None = None


def set_prompt_session(session: PromptSession | None) -> None:
    """Register (or clear) the classic REPL's prompt session for reuse.

    Pass the session created in ``PhosonRepl.run`` while the REPL is
    running, and ``None`` when it exits.
    """
    global _prompt_session
    _prompt_session = session


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


def _picker_key_bindings() -> KeyBindings:
    """Enter confirms the best row; Esc cancels."""
    key_bindings = KeyBindings()

    @key_bindings.add("enter")
    def _accept(event) -> None:
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is not None:
            completion = state.current_completion
            # ``complete_while_typing`` shows the menu without selecting a
            # row, so fall back to the first (best-scoring) completion.
            if completion is None and state.completions:
                completion = state.completions[0]
            if completion is not None:
                buffer.apply_completion(completion)
        buffer.validate_and_handle()

    @key_bindings.add("escape")
    def _cancel(event) -> None:
        event.app.exit(result="")

    return key_bindings


async def _run_prompt(
    session: PromptSession,
    title: str,
    completer: Completer,
    key_bindings: KeyBindings,
    style: Style,
) -> str:
    """Drive *session* as the picker, restoring its prompt config after.

    No ``bottom_toolbar``: the main prompt (whose ``@file`` menu works on
    every terminal) has none either, and adding one is the one structural
    difference that could disturb the menu layout.
    """
    saved = (
        session.completer,
        session.key_bindings,
        session.style,
        session.reserve_space_for_menu,
    )
    session.completer = completer
    session.key_bindings = key_bindings
    session.style = style
    session.reserve_space_for_menu = _MENU_ROWS
    try:
        return await session.prompt_async(
            f"{title} › ",
            # Show the menu (first row highlighted) before any keystroke.
            pre_run=lambda: session.default_buffer.start_completion(select_first=True),
        )
    finally:
        (
            session.completer,
            session.key_bindings,
            session.style,
            session.reserve_space_for_menu,
        ) = saved


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
    completer = _OptionCompleter(options, current)
    key_bindings = _picker_key_bindings()
    style = Style.from_dict(build_prompt_style(active_theme))

    session = _prompt_session
    if session is None:
        session = PromptSession(complete_while_typing=True)
    try:
        text = await _run_prompt(session, title, completer, key_bindings, style)
    except (EOFError, KeyboardInterrupt):
        return None

    text = text.strip()
    if not text:
        return None
    for option in options:
        if option.value == text or option.display == text:
            return option.value

    # Fallback for a partially-typed value whose completion had not landed
    # yet: resolve to the best fuzzy match (e.g. "gpt" -> "openai/gpt-4o").
    scored: list[tuple[int, InlineOption]] = []
    for option in options:
        score = fuzzy_score(text, f"{option.value} {option.display} {option.meta}")
        if score is not None:
            scored.append((score, option))
    if scored:
        scored.sort(key=lambda item: (-item[0], item[1].value.lower()))
        return scored[0][1].value
    return None
