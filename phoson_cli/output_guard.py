"""General handler for stray CLI output (issue #148).

Both front ends (the full-screen TUI and the classic REPL) break when
arbitrary ``print()``/logging noise lands on the terminal mid-render. The
dominant source — MCP stdio subprocesses writing raw JSON logs to stderr —
is fixed at the source (``phoson_plugin_mcp`` routes each server's stderr to
a per-server log file). This module is the backstop: while a front end is
running it redirects ``sys.stderr`` (the channel stray logs land on) to a
log file plus a bounded in-memory tail that can be inspected for debugging.

``sys.stdout`` is deliberately NOT redirected: it is the TUI's paint
channel (prompt_toolkit writes frames to it) and the one-shot result
channel; redirecting it would tear the alt-screen render.

Install it only while a front end runs so pre-launch messages (config
errors, the setup wizard) still reach the user::

    guard = OutputGuard()
    guard.install()
    try:
        asyncio.run(app.run_async())
    finally:
        guard.restore()
"""

import io
import sys
import time
import threading
from pathlib import Path

_LOG_DIR = Path.home() / ".phoson" / "logs"
_STDERR_LOG = _LOG_DIR / "cli-stderr.log"
_TAIL_LIMIT = 2000


class _TeeWriter:
    """File-like that appends written text to a log file and a bounded tail.

    Assigned to ``sys.stderr``; implements just enough of the TextIO
    surface that ``print(file=sys.stderr)`` and logging's StreamHandler
    need (``write``/``flush``/``isatty``/``writable`` + ``encoding``).
    """

    def __init__(self, log_path: Path, tail: list[str]) -> None:
        self._log_path = log_path
        self._tail = tail
        self._lock = threading.Lock()
        self._fh: io.TextIOBase | None = None
        self._buf = ""
        self.encoding = "utf-8"
        self.errors = "backslashreplace"

    def _ensure_file(self) -> io.TextIOBase:
        if self._fh is None:
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self._log_path, "a", encoding="utf-8")
            except Exception:  # noqa: BLE001 - degrade to memory-only
                self._fh = io.StringIO()
        return self._fh

    def write(self, data: str) -> int:
        if not data:
            return 0
        fh = self._ensure_file()
        try:
            fh.write(data)
            fh.flush()
        except Exception:  # noqa: BLE001
            pass
        # ``print`` splits each call into a content write plus a separate
        # ``"\n"`` write, so buffer until a newline arrives before recording
        # a (complete) line — otherwise every print would leave a spurious
        # empty line in the tail.
        with self._lock:
            self._buf += data
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._record(line)
        return len(data)

    def _record(self, line: str) -> None:
        self._tail.append(f"{time.strftime('%H:%M:%S')} {line}")
        while len(self._tail) > _TAIL_LIMIT:
            self._tail.pop(0)

    def flush(self) -> None:
        if self._fh is not None and not isinstance(self._fh, io.StringIO):
            try:
                self._fh.flush()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            if self._buf:
                self._record(self._buf)
                self._buf = ""

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


class OutputGuard:
    """Redirect ``sys.stderr`` to a log file + in-memory tail while active."""

    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path = Path(log_path) if log_path else _STDERR_LOG
        self._tail: list[str] = []
        self._writer = _TeeWriter(self._log_path, self._tail)
        self._original: object | None = None
        self._active = False

    @property
    def log_path(self) -> Path:
        return self._log_path

    def install(self) -> "OutputGuard":
        if not self._active:
            self._original = sys.stderr
            sys.stderr = self._writer
            self._active = True
            _CURRENT[0] = self
        return self

    def restore(self) -> None:
        if self._active:
            if _CURRENT[0] is self:
                _CURRENT[0] = None
            if self._original is not None:
                sys.stderr = self._original
            self._active = False
            self._original = None

    def last_lines(self, n: int = 40) -> list[str]:
        return list(self._tail[-n:])

    def __enter__(self) -> "OutputGuard":
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.restore()


# Module-level handle (single slot) so a front end can surface the captured
# tail — e.g. a /logs command — without threading the guard through the app.
_CURRENT: list[OutputGuard | None] = [None]


def current_guard() -> OutputGuard | None:
    """Return the active :class:`OutputGuard`, if any."""
    return _CURRENT[0]
