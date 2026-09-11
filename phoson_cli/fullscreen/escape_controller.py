"""Escape-key handling for the full-screen front end (#187).

Extracted from ``app.py``: a lone Esc cancels an in-flight run; a clean
double-tap while idle opens the rewind picker. Kept as a thin delegate
on ``PhosonApp`` (``handle_escape``) so the ``keys.py`` name lookup and
the test suite keep working.
"""

import time
from typing import Any

# Double-Esc rewind (IMPROVEMENTS.md G1): a second Esc within this window
# (measured in monotonic seconds between *delivered* key presses) opens the
# rewind picker. The window must be comfortably LARGER than prompt_toolkit's
# ``ttimeoutlen`` (0.5 s): the VT100 input layer delays delivery of a lone
# Esc by ``ttimeoutlen`` to disambiguate it from the start of an escape
# sequence (arrow keys, ``\x1b[A``, ...). As a result the *delivered*
# interval between two idle Esc presses is clamped to ~``ttimeoutlen`` from
# below regardless of how quickly the user tapped — a 0.5 s window would
# therefore miss real double-taps. 1.0 s sits well above that floor while
# staying below the gap of two deliberately separate Esc presses, so a slow
# single Esc never opens the picker. The *single* Esc cancel (#68) is
# unaffected: that binding stays eager and fires immediately while a run is
# in flight, and no double-tap state is recorded then.
_REWIND_DOUBLE_ESC_WINDOW_SECONDS = 1.0


def is_prefixed_escape(app: Any) -> bool:
    """True when this Esc is the *prefix* of an Alt+<key> sequence.

    Many terminals encode **Alt+<key>** as ``ESC`` + <key> (the
    Meta/Alt convention). For Alt+Backspace the bytes are
    ``0x1b 0x7f``; prompt_toolkit's VT100 parser emits them as two
    KeyPresses — ``escape`` first, then ``c-h`` (Ctrl+H, data
    ``'\\x7f'``). Because the escape binding is registered ``eager``,
    ``handle_escape`` fires for the first KeyPress while the second
    is still in ``key_processor.input_queue``.

    The heuristic (issue #108): the second key's ``data`` is the
    *original* terminal byte. For Meta-encoded keys this is a
    printable ASCII character (0x20-0x7e) or DEL (0x7f). For
    unrelated keys that merely happen to be in the queue (Ctrl+C
    = ``\\x03``, Enter = ``\\r``, another Esc = ``\\x1b``), the
    data is a control character below 0x20. We only suppress the
    Esc when the next queued key looks like a Meta-encoded payload.
    """
    processor = getattr(app.app, "key_processor", None)
    if processor is None:
        return False
    queue = getattr(processor, "input_queue", None)
    if queue is None:
        return False
    for kp in queue:
        # The _Flush sentinel is an internal marker, not a real key.
        if kp.data == "_Flush":
            continue
        # Meta/Alt encoding: the byte after ESC is in the range
        # 0x20 (space) through 0x7f (DEL). This covers:
        #   Alt+letter  -> data = the letter (0x41-0x7a)
        #   Alt+digit   -> data = the digit  (0x30-0x39)
        #   Alt+Backspace -> data = '\\x7f' (DEL)
        # It does NOT match control characters that arrive from
        # separate key events (Ctrl+C '\\x03', Enter '\\r',
        # another Esc '\\x1b'), which are all below 0x20.
        if kp.data:
            code = ord(kp.data[0])
            if 0x20 <= code <= 0x7F:
                return True
    return False


def handle_escape(app: Any) -> None:
    """Escape: cancel the in-flight run; double-tap opens the rewind.

    Precedence (G1, coordinated with #68 and #108):
    - **Prefix guard (#108):** if this Esc is the prefix of a longer
      terminal sequence (Alt+<key>), it is silently ignored — neither
      cancelling a run nor arming the double-tap window.
    - While a run is in flight, a *clean* Esc keeps its *immediate*
      cancel role (the binding is registered ``eager`` in ``keys.py``
      so a double tap mid-run can never be swallowed as a chord) and
      no double-tap state is recorded.
    - While idle, a lone clean Esc still does nothing here (inside
      Float pickers they bind Esc themselves and take precedence). A
      second clean Esc within ``_REWIND_DOUBLE_ESC_WINDOW_SECONDS``
      opens the rewind picker (``handle_rewind``).
    """
    # Issue #108: Alt+Backspace (ESC 0x7f) arrives as escape + c-h
    # in the same batch. The eager handler fires for the escape while
    # c-h is still queued — that means this was NOT a deliberate Esc.
    if is_prefixed_escape(app):
        return
    if app._is_run_in_flight():
        app.repl.cancel_current()
        app.sink.notify("info", "Cancelling current run (Esc)...")
        return
    now = time.monotonic()
    if now - app._last_escape_at <= _REWIND_DOUBLE_ESC_WINDOW_SECONDS:
        app._last_escape_at = 0.0
        app.app.create_background_task(app.handle_rewind())
        return
    app._last_escape_at = now


__all__ = ["handle_escape", "is_prefixed_escape"]
