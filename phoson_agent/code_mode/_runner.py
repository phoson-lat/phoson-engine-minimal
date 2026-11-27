"""Spawn the code-mode worker and serve its tool calls.

One :func:`run_script` call = one worker process. The worker relays every
``tools.*`` call over a JSON-lines pipe; this side executes it through the
:class:`~phoson_agent.code_mode._bridge.ToolBridge` and answers. Whatever
the script prints is captured (capped) and becomes the ``run_code`` result.
"""

import os
import sys
import json
import time
import shutil
import asyncio
import tempfile
import contextlib
from typing import TYPE_CHECKING, Any
from pathlib import Path
from dataclasses import dataclass

from phoson_agent.procutil import session_kwargs, kill_process_tree

from ._sandbox import SandboxUnavailableError, plan_command

if TYPE_CHECKING:
    from . import CodeModeConfig
    from ._bridge import ToolBridge

#: argv[1] that makes the standalone binary act as the worker (see
#: :func:`phoson_agent.code_mode.worker_entry`).
WORKER_FLAG = "--phoson-code-worker"

#: Largest single RPC line (a tool call's args or a result). Large file
#: contents legitimately travel over this pipe.
_RPC_LINE_LIMIT = 64 * 1024 * 1024

#: Environment variables passed to the worker. The rest — API keys, tokens —
#: stays in the host: tools that need credentials run there, not in the script.
_ENV_KEEP = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
)


class CodeRunError(RuntimeError):
    """The script failed, timed out or could not start.

    Raised from the ``run_code`` handler so the tool result is flagged as an
    error; ``str(exc)`` is the full result text (output + status).
    """


def _worker_source() -> str | None:
    candidates = [Path(__file__).with_name("_worker.py")]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "phoson_agent" / "code_mode" / "_worker.py")
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def worker_command(python: list[str] | None = None) -> list[str]:
    """argv that starts a worker.

    Order: an explicit ``python`` command; the running interpreter (source
    installs); a ``python3``/``python`` on PATH (the standalone binary has no
    general-purpose interpreter); finally the binary itself in worker mode
    (its bundled stdlib only).
    """
    source = _worker_source()
    if python:
        if source is None:
            raise CodeRunError("run_code: the worker source is missing from this build")
        return [*python, "-I", "-c", source]
    if not getattr(sys, "frozen", False) and source is not None:
        return [sys.executable, "-I", "-c", source]
    if source is not None:
        for candidate in ("python3", "python"):
            found = shutil.which(candidate)
            if found:
                return [found, "-I", "-c", source]
    return [sys.executable, WORKER_FLAG]


def _worker_env(scratch: str) -> dict[str, str]:
    env = {k: v for k in _ENV_KEEP if (v := os.environ.get(k)) is not None}
    env.update(
        {
            "TMPDIR": scratch,
            "TEMP": scratch,
            "TMP": scratch,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


async def _read_capped(
    stream: asyncio.StreamReader, max_bytes: int
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if total < max_bytes:
            keep = chunk[: max_bytes - total]
            chunks.append(keep)
            total += len(keep)
            truncated = truncated or len(keep) < len(chunk)
        else:
            truncated = True
    return b"".join(chunks), truncated


@dataclass
class _State:
    busy: int = 0
    done_ok: bool | None = None


async def run_script(
    code: str,
    *,
    bridge: "ToolBridge",
    config: "CodeModeConfig",
    timeout: float,
    cwd: str | None = None,
) -> str:
    """Run ``code`` in a worker; return the result text (raises CodeRunError)."""
    cwd = cwd or os.getcwd()
    scratch = tempfile.mkdtemp(prefix="phoson-code-")
    try:
        try:
            base = worker_command(config.python)
            plan = plan_command(base, mode=config.sandbox, scratch=scratch, cwd=cwd)
        except SandboxUnavailableError as exc:
            raise CodeRunError(f"run_code not started: {exc}") from None
        return await _run(
            code, plan.command, plan.note, bridge, config, timeout, cwd, scratch
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _run(
    code: str,
    command: list[str],
    note: str | None,
    bridge: "ToolBridge",
    config: "CodeModeConfig",
    timeout: float,
    cwd: str,
    scratch: str,
) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_worker_env(scratch),
            limit=_RPC_LINE_LIMIT,
            **session_kwargs(),
        )
    except OSError as exc:
        raise CodeRunError(f"run_code could not start Python: {exc}") from None
    assert (
        proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    )
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr

    state = _State()
    write_lock = asyncio.Lock()
    handlers: set[asyncio.Task[None]] = set()

    async def send(message: dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=True, default=str) + "\n"
        async with write_lock:
            try:
                stdin.write(line.encode("utf-8"))
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the worker already exited; nobody is waiting

    async def handle(message: dict[str, Any]) -> None:
        request_id = message.get("id")
        kind = message.get("type")
        ok: bool
        value: Any
        try:
            if kind == "call":
                state.busy += 1
                try:
                    ok, value = await bridge.call(
                        str(message.get("tool") or ""), message.get("args") or {}
                    )
                finally:
                    state.busy -= 1
            elif kind == "search":
                ok, value = (
                    True,
                    bridge.search(
                        str(message.get("query") or ""), message.get("limit") or 10
                    ),
                )
            elif kind == "help":
                ok, value = True, bridge.help(str(message.get("tool") or ""))
            else:
                ok, value = False, f"unknown request type {kind!r}"
        except Exception as exc:  # noqa: BLE001 - reported to the script
            ok, value = False, str(exc) or type(exc).__name__
        reply = {"id": request_id, "ok": ok}
        reply["value" if ok else "error"] = value
        await send(reply)

    async def serve() -> None:
        while True:
            try:
                line = await stdout.readline()
            except ValueError:
                # A line over the limit: the protocol is broken, stop serving.
                return
            if not line:
                return
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "done":
                state.done_ok = bool(message.get("ok"))
                continue
            task = asyncio.create_task(handle(message))
            handlers.add(task)
            task.add_done_callback(handlers.discard)

    await send({"type": "run", "code": code, "tools": bridge.specs()})
    serve_task = asyncio.create_task(serve())
    output_task = asyncio.create_task(_read_capped(stderr, config.max_output_bytes))
    wait_task = asyncio.create_task(proc.wait())

    timed_out = False
    try:
        finished = await _wait_with_budget(wait_task, timeout, lambda: state.busy > 0)
        if not finished:
            timed_out = True
            await _kill(proc, wait_task)
        await asyncio.wait({serve_task}, timeout=2)
        output, truncated = await asyncio.wait_for(output_task, timeout=2)
    except asyncio.CancelledError:
        await asyncio.shield(_kill(proc, wait_task))
        raise
    except TimeoutError:
        # Output pipe held open by a grandchild the script left behind.
        await _kill(proc, wait_task)
        output, truncated = b"", True
    finally:
        for task in (serve_task, output_task, *handlers):
            if not task.done():
                task.cancel()
        await asyncio.gather(serve_task, output_task, *handlers, return_exceptions=True)
        with contextlib.suppress(Exception):
            stdin.close()

    text = output.decode("utf-8", errors="replace").rstrip()
    if truncated:
        text += f"\n[...output truncated at {config.max_output_bytes // 1024} KB]"
    ok = (state.done_ok is True) and proc.returncode == 0 and not timed_out
    trailer = [
        line
        for line in (
            f"[run_code: timed out after {timeout:.0f}s and was stopped]"
            if timed_out
            else None,
            f"[run_code: exited with code {proc.returncode}]"
            if not ok and not timed_out and state.done_ok is None
            else None,
            bridge.call_summary() or None,
            note,
        )
        if line
    ]
    body = text if text else ("(no output — print() what you need)" if ok else "")
    result = "\n".join(part for part in [body, *trailer] if part)
    if not ok:
        raise CodeRunError(result or "run_code failed")
    return result


async def _wait_with_budget(
    wait_task: asyncio.Task[int], budget: float, busy: Any
) -> bool:
    """Wait for the worker; the clock only runs while no tool call is in flight.

    Time spent in a tool (including a permission prompt) does not count
    against the script's budget. Returns False on timeout.
    """
    remaining = budget
    last = time.monotonic()
    while True:
        done, _ = await asyncio.wait(
            {wait_task}, timeout=min(0.25, max(remaining, 0.01))
        )
        now = time.monotonic()
        if not busy():
            remaining -= now - last
        last = now
        if done:
            return True
        if remaining <= 0:
            return False


async def _kill(proc: asyncio.subprocess.Process, wait_task: asyncio.Task[int]) -> None:
    if proc.returncode is None:
        kill_process_tree(proc.pid)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=5)


__all__ = ["WORKER_FLAG", "CodeRunError", "run_script", "worker_command"]
