"""Monitor kinds: the background watchers.

A *kind* validates its spec and runs an async watch loop that calls a fire
callback when its condition holds. Kinds are deliberately tiny and
dependency-free (stdlib only):

- ``interval`` — fire after N seconds (once) or every N seconds.
- ``file`` — poll a path or glob and fire on creation/change (mtime+size).
- ``command`` — run a shell command on an interval; fire on non-zero exit
  or when the (truncated) output changes.

``sleep_fn`` and ``now_fn`` are injectable so tests can run the loops
deterministically with a fake clock; production uses ``asyncio.sleep`` and
``time.monotonic``.
"""

import os
import signal
import asyncio
import hashlib
import logging
from typing import Any
from pathlib import Path
from collections.abc import Callable, Awaitable

from .storage import MonitorDef

logger = logging.getLogger(__name__)

# Fire callback: (monitor_name, kind, payload) — called in the task context.
FireCallback = Callable[[str, str, dict], Awaitable[None] | None]

MAX_COMMAND_OUTPUT_CHARS = 4000


def _require_str(spec: dict, key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"spec.{key} must be a non-empty string")
    return value


def _require_number(
    spec: dict, key: str, *, minimum: float = 0.0, default: float | None = None
) -> float:
    if key not in spec and default is not None:
        return float(default)
    value = spec.get(key)
    if value is None and default is not None:
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"spec.{key} must be a number")
    if value < minimum:
        raise ValueError(f"spec.{key} must be >= {minimum}")
    return float(value)


class IntervalKind:
    """Fire after N seconds (``once: true``) or every N seconds."""

    kind_name = "interval"

    @staticmethod
    def validate_spec(spec: dict) -> dict:
        seconds = _require_number(spec, "seconds", minimum=1.0)
        once = spec.get("once", False)
        if not isinstance(once, bool):
            raise ValueError("spec.once must be a boolean")
        return {"seconds": seconds, "once": once}

    @staticmethod
    async def run(
        defn: MonitorDef,
        fire: FireCallback,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        seconds = float(defn.spec.get("seconds", 0))
        once = bool(defn.spec.get("once", False))
        await sleep_fn(seconds)
        await _call_fire(
            fire, defn.name, IntervalKind.kind_name, {"interval_seconds": seconds}
        )
        if once:
            return
        while True:
            await sleep_fn(seconds)
            await _call_fire(
                fire, defn.name, IntervalKind.kind_name, {"interval_seconds": seconds}
            )


def _file_snapshot(pattern: str, base_dir: str) -> dict[str, tuple[int, int]]:
    """Map files matching ``pattern`` (relative to ``base_dir``) to (mtime_ns, size)."""
    snapshots: dict[str, tuple[int, int]] = {}
    try:
        candidates = Path(base_dir).glob(pattern)
    except (OSError, ValueError):
        return snapshots
    for path in candidates:
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshots[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return snapshots


class FileKind:
    """Poll a path or glob; fire on creation and/or mtime+size change."""

    kind_name = "file"

    @staticmethod
    def validate_spec(spec: dict) -> dict:
        path = _require_str(spec, "path")
        event = spec.get("event", "changed")
        if event not in ("created", "changed", "both"):
            raise ValueError("spec.event must be 'created', 'changed' or 'both'")
        poll_seconds = spec.get("poll_seconds", 2.0)
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)):
            raise ValueError("spec.poll_seconds must be a number")
        if poll_seconds < 0.2:
            raise ValueError("spec.poll_seconds must be >= 0.2")
        # A plain path that exists or could exist resolves relative to the
        # host cwd; globs are used as-is (they may be absolute).
        return {"path": path, "event": event, "poll_seconds": float(poll_seconds)}

    @staticmethod
    async def run(
        defn: MonitorDef,
        fire: FireCallback,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        path_spec = str(defn.spec.get("path", ""))
        event = str(defn.spec.get("event", "changed"))
        poll_seconds = float(defn.spec.get("poll_seconds", 2.0))

        # Both literal paths and globs reduce to Path(parent).glob(name);
        # pathlib supports wildcards in the parent (e.g. "logs/*/app.log").
        resolved = Path(path_spec)
        if not resolved.is_absolute():
            resolved = Path(os.getcwd()) / resolved
        base_dir = str(resolved.parent) or "."
        pattern = resolved.name

        want_created = event in ("created", "both")
        want_changed = event in ("changed", "both")
        first = True
        previous = _file_snapshot(pattern, base_dir)
        seen: set[str] = set(previous)

        while True:
            await sleep_fn(poll_seconds)
            current = _file_snapshot(pattern, base_dir)

            created = [p for p in current if p not in seen]
            changed = [
                p for p in current if p in seen and current[p] != previous.get(p)
            ]
            removed = [p for p in seen if p not in current]

            hits: list[str] = []
            if want_created:
                hits.extend(created)
            if want_changed:
                hits.extend(changed)

            if hits or (not first and removed):
                payload = {
                    "path": path_spec,
                    "event": event,
                    "created": created,
                    "changed": changed,
                    "removed": removed,
                }
                await _call_fire(fire, defn.name, FileKind.kind_name, payload)

            seen = set(current)
            previous = current
            first = False


class CommandKind:
    """Run a shell command on an interval; fire on exit!=0 or output change."""

    kind_name = "command"

    @staticmethod
    def validate_spec(spec: dict) -> dict:
        command = _require_str(spec, "command")
        interval = _require_number(spec, "interval_seconds", minimum=0.0, default=60.0)
        timeout = spec.get("timeout_seconds", 60.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("spec.timeout_seconds must be a number")
        if timeout <= 0:
            raise ValueError("spec.timeout_seconds must be > 0")
        return {
            "command": command,
            "interval_seconds": interval,
            "timeout_seconds": float(timeout),
        }

    @staticmethod
    def spec_fingerprint(spec: dict) -> str:
        return hashlib.sha256(str(spec.get("command", "")).encode()).hexdigest()[:16]

    @staticmethod
    async def run(
        defn: MonitorDef,
        fire: FireCallback,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        command = str(defn.spec.get("command", ""))
        interval = float(defn.spec.get("interval_seconds", 60.0))
        timeout = float(defn.spec.get("timeout_seconds", 60.0))
        new_session = os.name != "nt"
        last_output: str | None = None
        has_baseline = False

        while True:
            if has_baseline:
                await sleep_fn(interval)

            returncode: int | None = None
            output = ""
            timed_out = False
            try:
                subprocess_kw: dict = {
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                }
                if new_session:
                    subprocess_kw["start_new_session"] = True
                proc = await asyncio.create_subprocess_shell(command, **subprocess_kw)
                try:
                    out, err = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                    returncode = proc.returncode
                    output = ((out or b"") + (err or b"")).decode(errors="replace")
                except TimeoutError:
                    timed_out = True
                    # Kill the whole process group: the shell's children
                    # (e.g. ``sleep 30`` in a pipeline) would otherwise
                    # keep the stdout pipe open and the drain below would
                    # hang until EOF.
                    try:
                        if new_session:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        else:
                            proc.kill()
                    except (ProcessLookupError, PermissionError, OSError):
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                    try:
                        out, err = await proc.communicate()
                        output = ((out or b"") + (err or b"")).decode(errors="replace")
                    except (OSError, ValueError):
                        pass
                    await proc.wait()
            except OSError as exc:
                returncode = -1
                output = f"<failed to start process: {exc}>"

            output_tail = output[-MAX_COMMAND_OUTPUT_CHARS:]
            changed = output_tail != last_output
            # First tick establishes the output baseline; fire only on
            # failure, timeout, or a change of output afterwards.
            should_fire = (
                returncode not in (0, None) or timed_out or (has_baseline and changed)
            )
            last_output = output_tail
            has_baseline = True

            if should_fire:
                payload = {
                    "command": command,
                    "returncode": returncode,
                    "timed_out": timed_out,
                    "output_tail": output_tail[:2000],
                }
                await _call_fire(fire, defn.name, CommandKind.kind_name, payload)


async def _call_fire(
    fire: FireCallback, name: str, kind: str, payload: dict[str, Any]
) -> None:
    result = fire(name, kind, payload)
    if asyncio.iscoroutine(result):
        await result


_KINDS: dict[str, type] = {
    "interval": IntervalKind,
    "file": FileKind,
    "command": CommandKind,
}


def known_kinds() -> list[str]:
    return list(_KINDS)


def validate_spec(kind: str, spec: dict) -> dict:
    """Validate (and normalize) ``spec`` for ``kind``.

    Raises:
        ValueError: Unknown kind or invalid spec fields.
    """
    cls = _KINDS.get(kind)
    if cls is None:
        raise ValueError(f"Unknown monitor kind {kind!r}. Known: {known_kinds()}")
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object")
    return cls.validate_spec(spec)


async def run_kind(
    defn: MonitorDef,
    fire: FireCallback,
    *,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run the watch loop of ``defn.kind`` until cancelled."""
    cls = _KINDS.get(defn.kind)
    if cls is None:  # pragma: no cover - guarded by validate_spec
        raise ValueError(f"Unknown monitor kind {defn.kind!r}")
    await cls.run(defn, fire, sleep_fn=sleep_fn)


__all__ = [
    "CommandKind",
    "FileKind",
    "IntervalKind",
    "FireCallback",
    "known_kinds",
    "run_kind",
    "validate_spec",
]
