"""Tests for the bash tool.

Verify that:
- It is fully async (does not block the event loop on long commands).
- Output is truncated when it exceeds ``MAX_BYTES``.
- Timeouts are honored.
- ``safe_mode=True`` short-circuits without spawning a subprocess when the
  user declines.
"""

import sys
import asyncio
import importlib

import pytest

# Note: ``phoson_cli.tools.bash`` is shadowed in the ``tools`` package by the
# ``AgentTool`` of the same name (re-exported from ``__init__.py``). To reach
# the real module — for monkeypatching internals like ``_confirm_async`` —
# we import it via ``importlib`` and keep a reference.
bash_module = importlib.import_module("phoson_cli.tools.bash")
MAX_BYTES = bash_module.MAX_BYTES
BashTool = bash_module.BashTool


@pytest.mark.asyncio
async def test_bash_runs_simple_command() -> None:
    out = await BashTool().run("echo hola")
    assert "hola" in out


@pytest.mark.asyncio
async def test_bash_handler_is_coroutine() -> None:
    """The tool handler must be async (regression).

    Previously ``bash`` was a sync function calling ``input()`` and
    ``subprocess.run`` which blocked the event loop. The handler is now a
    coroutine and the @tool decorator preserves that.
    """
    # The @tool decorator wraps the function; ``bash.handler`` expects the
    # ``args, ctx`` signature. We call the underlying coroutine directly to
    # check it's awaitable end-to-end.
    coro = BashTool().run("echo hi", safe_mode=False)
    assert asyncio.iscoroutine(coro)
    out = await coro
    assert "hi" in out


@pytest.mark.asyncio
async def test_bash_event_loop_stays_responsive() -> None:
    """While bash sleeps the event loop must keep ticking."""
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.05)
            ticks += 1

    sleep_cmd = f"{sys.executable} -c \"import time; time.sleep(0.4)\""
    bash_task = asyncio.create_task(BashTool().run(sleep_cmd))
    tick_task = asyncio.create_task(ticker())

    await asyncio.gather(bash_task, tick_task)

    # If the loop had been blocked, ticks would stay at 0 (or close to it).
    assert ticks >= 4


@pytest.mark.asyncio
async def test_bash_timeout_returns_message() -> None:
    sleep_cmd = f"{sys.executable} -c \"import time; time.sleep(2)\""
    out = await BashTool().run(sleep_cmd, timeout=0.2)
    assert "timed out" in out.lower()


@pytest.mark.asyncio
async def test_bash_truncates_long_output() -> None:
    cmd = (
        f"{sys.executable} -c "
        f"\"import sys; sys.stdout.write('A' * {MAX_BYTES + 1024})\""
    )
    out = await BashTool().run(cmd)
    assert "[...truncated]" in out
    # The total returned string is the truncated payload + the marker.
    assert len(out.encode()) <= MAX_BYTES + len("\n\n[...truncated]") + 16


@pytest.mark.asyncio
async def test_bash_safe_mode_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the user declines, no subprocess is spawned."""
    spawned = {"count": 0}

    real_create = asyncio.create_subprocess_shell

    async def fake_create(*args: object, **kwargs: object):
        spawned["count"] += 1
        return await real_create(*args, **kwargs)  # type: ignore[arg-type]

    async def fake_confirm(_command: str) -> bool:
        return False

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)
    monkeypatch.setattr(bash_module, "_confirm_async", fake_confirm)

    out = await BashTool().run("echo nope", safe_mode=True)

    assert "Cancelled" in out
    assert spawned["count"] == 0


@pytest.mark.asyncio
async def test_bash_safe_mode_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_confirm(_command: str) -> bool:
        return True

    monkeypatch.setattr(bash_module, "_confirm_async", fake_confirm)

    out = await BashTool().run("echo yes", safe_mode=True)
    assert "yes" in out
