"""Terminal notification on run completion (#167).

When the user has asked for it (``notify_on_completion`` in ``config.toml`` /
``PHOSON_NOTIFY_ON_COMPLETION`` / the ``/notify`` command), a finished agent
run emits a cue to the terminal so a backgrounded window gets attention:

- ``bell`` — a BEL (``\\a``).
- ``desktop`` — OSC 9 / OSC 777 desktop-notification sequences (iTerm2,
  WezTerm, Windows Terminal, Kitty, XTerm) plus a BEL fallback for terminals
  that do not support those.
- ``off`` (default) — silent, the historical behaviour.

The writer is **TTY-gated**: escape sequences only make sense on a real
terminal, and piping a run to another program (``phoson-cli -p ... | tee``)
must not be polluted with a BEL byte. :func:`emit` checks ``isatty`` so it is
safe to call unconditionally; the pure helpers (:func:`build_sequence`,
:func:`is_valid_mode`) are unit-testable without a TTY.

Writing a BEL / OSC to the terminal the full-screen TUI renders on is safe:
those are terminal *control* sequences the terminal processes out-of-band
(not rendered as text) — the same mechanism the OSC 8 hyperlink bridge relies
on.
"""

import sys
from typing import Any

#: Valid values for ``notify_on_completion``.
NOTIFY_MODES: tuple[str, ...] = ("off", "bell", "desktop")

#: ``\\a`` — the ASCII BEL. Triggers the terminal's audible/visual bell.
_BEL = "\x07"
#: OSC 9 — desktop notification (iTerm2, WezTerm, Windows Terminal, XTerm).
_OSC9 = "\x1b]9;"
#: OSC 777 — desktop notification (Kitty, WezTerm). ``notify`` is the action.
_OSC777 = "\x1b]777;notify;"
#: ST (String Terminator): ``ESC \\``.
_ST = "\x1b\\"

#: Default notification title for desktop-notification terminals.
DEFAULT_TITLE = "Phoson"


def build_sequence(mode: str, title: str = DEFAULT_TITLE) -> str:
    """Build the terminal control sequence for a finished run (pure).

    Args:
        mode: One of :data:`NOTIFY_MODES` (``off`` → empty string).
        title: The notification text shown by desktop-notification
            terminals (OSC 9 / OSC 777).

    Returns:
        The escape sequence to write to the terminal, or ``""`` for
        ``off``. For ``desktop`` the OSC sequences are followed by a BEL
        fallback so terminals that ignore OSCs still ring.
    """
    if mode == "off":
        return ""
    if mode == "bell":
        return _BEL
    # desktop: OSC 9 + OSC 777 (each ST-terminated) + a BEL fallback.
    return f"{_OSC9}{title}{_ST}{_OSC777}{title};{title}{_ST}{_BEL}"


def is_valid_mode(value: object) -> bool:
    """Whether *value* is a recognised notification mode (case-insensitive)."""
    return isinstance(value, str) and value.strip().lower() in NOTIFY_MODES


def _write(file: Any, text: str) -> None:
    """Best-effort write + flush of *text* to *file*."""
    file.write(text)
    try:
        file.flush()
    except Exception:  # noqa: BLE001 — a flush failure must never kill a run
        pass


def emit(
    mode: str,
    title: str = DEFAULT_TITLE,
    file: Any = None,
    *,
    interactive: bool | None = None,
) -> bool:
    """Emit a completion notification for *mode* to the terminal.

    Gated on *interactive* (whether the output is a real TTY): when it is
    not a TTY (piped / redirected / non-interactive script output) nothing
    is written, so escape sequences never leak into program output.

    Args:
        mode: One of :data:`NOTIFY_MODES` (``off`` is a no-op).
        title: Notification text for desktop-notification terminals.
        file: The output stream to write to. Defaults to ``sys.stdout``.
        interactive: Whether *file* is an interactive terminal. When
            ``None`` it is probed via ``isatty()`` on *file*.

    Returns:
        ``True`` when a sequence was actually written (testable); ``False``
        for ``off``, a non-TTY, or an invalid mode.
    """
    if not is_valid_mode(mode):
        return False
    text = build_sequence(mode.strip().lower(), title)
    if not text:
        return False
    target = file if file is not None else sys.stdout
    if interactive is None:
        isatty = getattr(target, "isatty", None)
        if isatty is None or not isatty():
            return False
    elif not interactive:
        return False
    _write(target, text)
    return True


def notify_run_done(
    mode: str,
    status: str,
    title: str = DEFAULT_TITLE,
    file: Any = None,
    *,
    interactive: bool | None = None,
) -> bool:
    """Convenience: notify when a run's terminal status is ``"done"``.

    Front ends call this with the :class:`~phoson_cli.controller.RunOutcome`
    ``status`` after ``run_turn``. Only a successful completion (``"done"``)
    notifies — errors and cancellations did not finish the run, so they stay
    silent (the user is usually looking at the terminal to deal with a
    failure). ``mode`` of ``"off"`` (or an invalid value) is a no-op, and the
    write is TTY-gated so piped/script output is never polluted.

    Returns:
        ``True`` when a sequence was written, ``False`` otherwise.
    """
    if status != "done":
        return False
    return emit(mode, title, file, interactive=interactive)
