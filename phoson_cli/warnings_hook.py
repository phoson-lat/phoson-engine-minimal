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
2. A root ``logging.Handler`` routing ``phoson_*`` ``WARNING+`` records to the same
   notice (instead of ``lastResort`` to stderr).

The notice printer is a module-level mutable so the classic REPL can point it at
``Renderer.print_warn`` (styled, theme-aware) while one-shot mode keeps the plain
default. Inside the full-screen TUI both hooks route through its sink so nothing
writes around the alternate-screen renderer.
"""

import re
import logging
import warnings
from dataclasses import dataclass
from collections.abc import Callable


#: Default notice printer: one plain line to stdout. Classic replaces this with
#: ``Renderer.print_warn``; one-shot explicitly installs a stderr/JSON diagnostic
#: printer before loading configuration.
def _default_notice_printer(line: str) -> None:
    print(f"  ⚠ {line}")


#: Mutable printer the hooks call. See module docstring for why it's a module global.
notice_printer: Callable[[str], None] = _default_notice_printer

# Prefixes of our own loggers whose WARNING+ records become notices. Third-party
# loggers (httpx, prompt_toolkit, …) are left alone so their normal handling stands.
_PHOSON_LOGGER_PREFIXES = (
    "phoson_agent",
    "phoson_cli",
    "phoson_llm",
    "phoson_plugin",
    "py.warnings",
)

# While the full-screen TUI is up, both hooks use its notice sink instead of writing
# directly. ``main()``'s classic path never sets this.
_fullscreen_active = False
_fullscreen_notice_printer: Callable[[str], None] | None = None
_fullscreen_printers: list[Callable[[str], None] | None] = []

_installed = False

#: Master switch for the notice channel. When ``False`` the hooks stay
#: installed but emit nothing, so a user who finds the soft-fail notices
#: noisy can silence them (``show_warnings = false`` in config.toml, the
#: PHOSON_SHOW_WARNINGS env var, or ``/warnings off``) without also losing
#: Python's normal warning handling elsewhere.
_enabled = True


def set_enabled(enabled: bool) -> None:
    """Globally enable/disable CLI warning notices (see :data:`_enabled`)."""
    global _enabled
    _enabled = bool(enabled)


def is_enabled() -> bool:
    """Whether CLI warning notices are currently shown."""
    return _enabled


@dataclass
class _InstallScope:
    previous_printer: Callable[[str], None]
    previous_showwarning: Callable[..., None]
    restored: bool = False


_install_stack: list[_InstallScope] = []
_handler: logging.Handler | None = None


def set_fullscreen_active(
    active: bool, printer: Callable[[str], None] | None = None
) -> None:
    """Push or pop a nestable alternate-screen notice route."""
    global _fullscreen_active, _fullscreen_notice_printer
    if active:
        _fullscreen_printers.append(printer)
    elif _fullscreen_printers:
        _fullscreen_printers.pop()
    _fullscreen_active = bool(_fullscreen_printers)
    _fullscreen_notice_printer = (
        _fullscreen_printers[-1] if _fullscreen_printers else None
    )


def reset_notice_printer() -> None:
    """Restore the plain default printer (used by tests / the classic REPL teardown)."""
    global notice_printer
    notice_printer = _default_notice_printer


def _one_line(message: object) -> str:
    """Collapse a possibly multi-line message to a single notice line."""
    return " ".join(str(message).split())


_CAPTURED_WARNING = re.compile(
    r"^.*?:\d+:\s+([A-Za-z_][A-Za-z0-9_.]*):\s*([^\n]*)", re.DOTALL
)


def _record_message(record: logging.LogRecord) -> str:
    """Return a path-free warning/log message suitable for a UI notice."""
    message = record.getMessage()
    if record.name == "py.warnings":
        match = _CAPTURED_WARNING.match(message)
        if match is not None:
            return f"{match.group(1)}: {_one_line(match.group(2))}"
    return _one_line(message)


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
    if not _enabled:
        return
    if _fullscreen_active:
        if _fullscreen_notice_printer is not None:
            _fullscreen_notice_printer(f"{category.__name__}: {_one_line(message)}")
        return
    notice_printer(f"{category.__name__}: {_one_line(message)}")


class _PhosonNoticeHandler(logging.Handler):
    """Route our own ``WARNING+`` log records to the CLI notice channel.

    Prevents ``logging.lastResort`` from printing raw ``WARNING:phoson_…`` lines to
    stderr. Third-party loggers and sub-WARNING records are skipped so this handler
    never shadows a real logging setup.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if not _enabled:
            return
        if record.levelno < logging.WARNING:
            return
        if not record.name.startswith(_PHOSON_LOGGER_PREFIXES):
            return
        if _fullscreen_active:
            if _fullscreen_notice_printer is not None:
                _fullscreen_notice_printer(_record_message(record))
            return
        notice_printer(_record_message(record))


def capture_warnings() -> Callable[[], None]:
    """Enable logging capture for one scope without disabling an outer capture."""
    if getattr(logging, "_warnings_showwarning", None) is not None:
        return lambda: None
    previous_showwarning = warnings.showwarning
    logging.captureWarnings(True)
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        logging.captureWarnings(False)
        warnings.showwarning = previous_showwarning

    return restore


def install() -> Callable[[], None]:
    """Install both hooks; return an idempotent ``restore()``.

    ``restore()`` puts ``warnings.showwarning`` back to the previous implementation
    and removes our handler from the root logger. ``main()`` calls it in a
    ``finally`` so ``SystemExit`` (the ``sys.exit`` calls) still triggers it.
    """
    global _handler, _installed, notice_printer
    scope = _InstallScope(notice_printer, warnings.showwarning)
    _install_stack.append(scope)
    if len(_install_stack) == 1:
        _handler = _PhosonNoticeHandler()
        logging.getLogger().addHandler(_handler)
        warnings.showwarning = _hooked_showwarning
    _installed = True

    def restore() -> None:
        global _handler, _installed, notice_printer
        if scope.restored:
            return
        scope.restored = True
        while _install_stack and _install_stack[-1].restored:
            completed = _install_stack.pop()
            notice_printer = completed.previous_printer
            if _install_stack:
                continue
            warnings.showwarning = completed.previous_showwarning
            if _handler is not None:
                root = logging.getLogger()
                if _handler in root.handlers:
                    root.removeHandler(_handler)
            _handler = None
            _installed = False

    return restore
