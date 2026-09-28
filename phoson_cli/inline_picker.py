"""Inline (line-oriented) pickers for the classic REPL.

The classic REPL is line-oriented: the full-screen ``Application`` pickers
(``model_picker``, ``provider_picker``, ``theme_picker``, ``session_picker``)
need an alternate-screen capable terminal. This module prints a numbered
list and reads a selection with the REPL's own prompt — the same plain
prompt that drives the REPL — so a bare ``/model``, ``/provider``,
``/theme`` or ``/sessions pick`` always offers a picker, on any terminal.

Why not a completion menu: a prompt_toolkit completion menu (the ``@file``
mechanism) did not render reliably inside the REPL's loop (notably on
Windows), and a nested ``PromptSession`` never appeared at all. A numbered
list over the existing prompt is the robust common denominator.

The picker reuses the REPL's live ``PromptSession`` (see
:func:`set_prompt_session`) rather than creating a second one.
"""

from dataclasses import dataclass

from prompt_toolkit.styles import Style
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.key_binding import KeyBindings

from .fuzzy import fuzzy_score
from .theme import Theme, load_theme, build_prompt_style

__all__ = ["InlineOption", "pick_inline", "set_prompt_session"]

#: The classic REPL's live prompt session, reused for the selection prompt.
_prompt_session: PromptSession | None = None

#: No key bindings for the selection line (the REPL's Ctrl+T etc. must not
#: fire while the picker is on screen).
_NO_KEYS = KeyBindings()


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


def _print_options(
    title: str, options: list[InlineOption], current: str | None
) -> None:
    """Print the numbered list of options."""
    print(f"\nSelect {title}:")
    for index, option in enumerate(options, 1):
        marker = "▶" if option.value == current else " "
        meta = f"   {option.meta}" if option.meta else ""
        print(f"  {marker} {index:>2}. {option.display}{meta}")


async def _read_line(message: str, theme: Theme) -> str:
    """Read one line, reusing the REPL's session (or a fresh one)."""
    session = _prompt_session
    if session is None:
        return await PromptSession().prompt_async(message)

    saved = (
        session.completer,
        session.complete_while_typing,
        session.key_bindings,
        session.bottom_toolbar,
        session.style,
        session.reserve_space_for_menu,
    )
    session.completer = None
    session.complete_while_typing = False
    session.key_bindings = _NO_KEYS
    session.bottom_toolbar = None
    session.style = Style.from_dict(build_prompt_style(theme))
    session.reserve_space_for_menu = 0
    try:
        return await session.prompt_async(message)
    finally:
        (
            session.completer,
            session.complete_while_typing,
            session.key_bindings,
            session.bottom_toolbar,
            session.style,
            session.reserve_space_for_menu,
        ) = saved


def _resolve(text: str, options: list[InlineOption]) -> str | None:
    """Map a typed line to an option value (number, exact, or fuzzy best)."""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        index = int(text) - 1
        return options[index].value if 0 <= index < len(options) else None
    for option in options:
        if option.value == text or option.display == text:
            return option.value

    scored: list[tuple[int, InlineOption]] = []
    for option in options:
        score = fuzzy_score(text, f"{option.value} {option.display} {option.meta}")
        if score is not None:
            scored.append((score, option))
    if scored:
        scored.sort(key=lambda item: (-item[0], item[1].value.lower()))
        return scored[0][1].value
    return None


async def pick_inline(
    title: str,
    options: list[InlineOption],
    *,
    current: str | None = None,
    theme: Theme | None = None,
    notice: str | None = None,
) -> str | None:
    """Print a numbered list and return the chosen value.

    Args:
        title: Short label (e.g. ``"model"``) used in the list header.
        options: The selectable rows, in their natural order.
        current: Value to mark as active (``▶``).
        theme: Active theme; resolved via ``load_theme()`` when ``None``.
        notice: Optional line printed above the list (e.g. providers whose
            live listing failed).

    Returns:
        The chosen option's ``value``, or ``None`` when the list is empty or
        the user cancels (empty line / ``Ctrl+C`` / ``Ctrl+D``).
    """
    if not options:
        return None
    if notice:
        print(notice)

    _print_options(title, options, current)
    message = f"{title} # [1-{len(options)}] (Enter to cancel): "
    try:
        text = await _read_line(message, theme or load_theme())
    except (EOFError, KeyboardInterrupt):
        return None
    return _resolve(text, options)
