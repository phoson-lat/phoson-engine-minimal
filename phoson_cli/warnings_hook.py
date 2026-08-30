"""Route warnings and phoson logging through the CLI notice channel (I-112).

Before this module, a soft-fail that emitted ``warnings.warn(...)`` (e.g. the vLLM
context-window "response did not include <model>" fallback) produced **two** outputs
in classic / one-shot mode: the intended styled notice *and* the raw Python warning
written straight to stderr by the default ``warnings.showwarning`` hook — with source
file + line number, corrupting the TUI aesthetic and exposing internal paths.
Soft-fail ``logger.warning`` calls leaked to stderr too, via ``logging.lastResort``
(no handler configured on the CLI's loggers).

:func:`install` wires two hooks for the duration of a ``main()`` run:

1. ``warnings.showwarning`` → the CLI notice printer (stdout, never stderr).
2. A root ``logging.Handler`` routing ``phoson_*`` ``WARNING+`` records → the same
   notice (instead of ``lastResort`` → stderr).

The notice printer is a module-level mutable so the classic REPL can point it at
``Renderer.print_warn`` (styled, theme-aware) while one-shot mode keeps the plain
default. Inside the full-screen TUI both hooks are no-ops (the alternate screen
cannot accept prints outside its buffer) — :func:`set_fullscreen_active` toggles that.
"""

import logging
import warnings
from collections.abc import Callable


#: Default notice printer: one plain line to **stdout**. Classic REPL replaces this
#: with ``Renderer.print_warn`` so the line inherits the active theme. One-shot mode
#: (scripts / CI) keeps the default — script-friendly, never stderr.
def _default_notice_printer(line: str) -> None:
    print(f"  ⚠ {line}")


#: Mutable printer the hooks call. See module docstring for why it's a module global.
notice_printer: Callable[[str], None] = _default_notice_printer

# Prefixes of our own loggers whose WARNING+ records become notices. Third-party
# loggers (httpx, prompt_toolkit, …) are left alone so their normal handling stands.
_PHOSON_LOGGER_PREFIXES = ("phoson_agent", "phoson_cli", "phoson_llm", "phoson_plugin")

# While the full-screen TUI is up, both hooks must stay silent (the alt-screen owns
# the terminal; a stray stdout write would tear the render). ``main()``'s classic
# path never sets this; only ``PhosonApp.run_async`` does, around the app run.
_fullscreen_active = False

_installed = False


def set_fullscreen_active(active: bool) -> None:
    """Toggle the "inside the alt-screen" mute flag (called by the TUI shell)."""
    global _fullscreen_active
    _fullscreen_active = active


def reset_notice_printer() -> None:
    """Restore the plain default printer (used by tests / the classic REPL teardown)."""
    global notice_printer
    notice_printer = _default_notice_printer


def _one_line(message: object) -> str:
    """Collapse a possibly multi-line message to a single notice line."""
    return " ".join(str(message).split())


def _hooked_showwarning(
    message: object,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: object = None,  # noqa: ARG001 - signature required by warnings module
    line: str | None = None,  # noqa: ARG001 - signature required by warnings module
) -> None:
    """Replacement for ``warnings.showwarning``: emit a notice, never raw stderr.

    ``filename``/``lineno`` are deliberately unused — that is the whole point (no
    internal paths / code lines reach the user).
    """
    if _fullscreen_active:
        return
    notice_printer(f"{category.__name__}: {_one_line(message)}")


class _PhosonNoticeHandler(logging.Handler):
    """Route our own ``WARNING+`` log records to the CLI notice channel.

    Prevents ``logging.lastResort`` from printing raw ``WARNING:phoson_…`` lines to
    stderr. Third-party loggers and sub-WARNING records are skipped so this handler
    never shadows a real logging setup.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if _fullscreen_active:
            return
        if record.levelno < logging.WARNING:
            return
        if not record.name.startswith(_PHOSON_LOGGER_PREFIXES):
            return
        notice_printer(_one_line(record.getMessage()))


def install() -> Callable[[], None]:
    """Install both hooks; return an idempotent ``restore()``.

    ``restore()`` puts ``warnings.showwarning`` back to the previous implementation
    and removes our handler from the root logger. ``main()`` calls it in a
    ``finally`` so ``SystemExit`` (the ``sys.exit`` calls) still triggers it.
    """
    global _installed
    if _installed:
        # Already installed (nested / re-entrant) — return a no-op restore.
        return lambda: None

    previous_showwarning = warnings.showwarning
    root = logging.getLogger()
    handler = _PhosonNoticeHandler()
    root.addHandler(handler)
    warnings.showwarning = _hooked_showwarning
    _installed = True

    def restore() -> None:
        global _installed
        if not _installed:
            return
        warnings.showwarning = previous_showwarning
        root.removeHandler(handler)
        _installed = False

    return restore
