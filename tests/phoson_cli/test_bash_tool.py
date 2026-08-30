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
DEFAULT_TIMEOUT_SECONDS = bash_module.DEFAULT_TIMEOUT_SECONDS
_run_bash = bash_module._run_bash


@pytest.mark.asyncio
async def test_bash_runs_simple_command() -> None:
    out = await _run_bash("echo hola")
    assert "hola" in out


@pytest.mark.asyncio
async def test_bash_handler_is_coroutine() -> None:
    """The tool handler must be async (regression).

    Previously ``bash`` was a sync function calling ``input()`` and
    ``subprocess.run`` which blocked the event loop. The handler is now a
    coroutine and the @tool decorator preserves that.
    """
    coro = _run_bash("echo hi", safe_mode=False)
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

    sleep_cmd = f'{sys.executable} -c "import time; time.sleep(0.4)"'
    bash_task = asyncio.create_task(_run_bash(sleep_cmd))
    tick_task = asyncio.create_task(ticker())

    await asyncio.gather(bash_task, tick_task)

    # If the loop had been blocked, ticks would stay at 0 (or close to it).
    assert ticks >= 4


@pytest.mark.asyncio
async def test_bash_timeout_returns_message() -> None:
    sleep_cmd = f'{sys.executable} -c "import time; time.sleep(2)"'
    out = await _run_bash(sleep_cmd, timeout=0.2)
    assert "timed out" in out.lower()


@pytest.mark.asyncio
async def test_bash_truncates_long_output() -> None:
    cmd = (
        f"{sys.executable} -c "
        f"\"import sys; sys.stdout.write('A' * {MAX_BYTES + 1024})\""
    )
    out = await _run_bash(cmd)
    assert "[...truncated]" in out
    # The total returned string is the truncated payload + the marker.
    assert len(out.encode()) <= MAX_BYTES + len("\n\n[...truncated]") + 16


class _FakeConfirmation:
    """Recording ConfirmationService double."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def confirm_bash(self, command: str) -> bool:
        self.asked.append(command)
        return self.answer


@pytest.mark.asyncio
async def test_bash_safe_mode_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the confirmation service declines, no subprocess is spawned."""
    spawned = {"count": 0}

    real_create = asyncio.create_subprocess_shell

    async def fake_create(*args: object, **kwargs: object):
        spawned["count"] += 1
        return await real_create(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)

    out = await _run_bash(
        "echo nope", safe_mode=True, confirmation=_FakeConfirmation(False)
    )

    assert "Cancelled" in out
    assert spawned["count"] == 0


@pytest.mark.asyncio
async def test_bash_safe_mode_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = _FakeConfirmation(True)
    out = await _run_bash("echo yes", safe_mode=True, confirmation=conf)
    assert "yes" in out
    assert conf.asked == ["echo yes"]


@pytest.mark.asyncio
async def test_bash_safe_mode_without_service_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-shot/scripts inject no confirmation service: refuse, never run,
    never hang."""
    spawned = {"count": 0}

    real_create = asyncio.create_subprocess_shell

    async def fake_create(*args: object, **kwargs: object):
        spawned["count"] += 1
        return await real_create(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)

    # No confirmation service provided (one-shot / scripts).
    out = await _run_bash("echo pwned", safe_mode=True)

    assert "Blocked" in out
    assert "safe_mode" in out
    assert spawned["count"] == 0


@pytest.mark.asyncio
async def test_bash_without_safe_mode_never_confirms() -> None:
    conf = _FakeConfirmation(False)  # would decline if asked
    out = await _run_bash("echo free", safe_mode=False, confirmation=conf)
    assert "free" in out
    assert conf.asked == []  # never consulted


@pytest.mark.asyncio
async def test_bash_tool_injects_confirmation_from_context() -> None:
    """The @tool handler picks bash_confirmation/safe_mode from the context
    (dict form, as _context_values accepts)."""
    conf = _FakeConfirmation(True)
    out = await bash_module.bash.handler(
        {"command": "echo injected"},
        context={"safe_mode": True, "bash_confirmation": conf},
    )
    assert "injected" in out
    assert conf.asked == ["echo injected"]


@pytest.mark.asyncio
async def test_bash_tool_without_context_key_fails_closed() -> None:
    """Context without bash_confirmation + safe_mode → refused (one-shot)."""
    out = await bash_module.bash.handler(
        {"command": "echo pwned"},
        context={"safe_mode": True},
    )
    assert "Blocked" in out


@pytest.mark.asyncio
async def test_bash_tool_schema_hides_injected_params() -> None:
    props = bash_module.bash.parameters.get("properties", {})
    assert "command" in props
    assert "safe_mode" not in props
    assert "bash_confirmation" not in props


# ---------------------------------------------------------------------------
# I-127: per-invocation timeout
# ---------------------------------------------------------------------------


class _FakeRunBash:
    """Records the timeout handed to ``_run_bash`` (monkeypatched)."""

    def __init__(self) -> None:
        self.timeout: float | None = None
        self.command: str | None = None

    async def __call__(
        self,
        command: str,
        safe_mode: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        confirmation: object | None = None,
    ) -> str:
        self.command = command
        self.timeout = timeout
        return f"ran:{command}"


@pytest.mark.asyncio
async def test_bash_tool_schema_exposes_timeout() -> None:
    """The LLM schema must advertise ``timeout`` as an optional number."""
    props = bash_module.bash.parameters.get("properties", {})
    required = bash_module.bash.parameters.get("required", [])
    assert "timeout" in props
    assert props["timeout"].get("type") == "number"
    assert "timeout" not in required
    desc = props["timeout"].get("description", "")
    assert "timeout in seconds" in desc.lower()
    assert "30" in desc
    assert "no maximum" in desc.lower()


@pytest.mark.asyncio
async def test_bash_tool_omitted_timeout_keeps_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``timeout`` in the args -> the internal 30s default is forwarded."""
    fake = _FakeRunBash()
    monkeypatch.setattr(bash_module, "_run_bash", fake)

    out = await bash_module.bash.handler({"command": "echo hi"})

    assert out == "ran:echo hi"
    assert fake.timeout == DEFAULT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_bash_tool_explicit_timeout_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRunBash()
    monkeypatch.setattr(bash_module, "_run_bash", fake)

    out = await bash_module.bash.handler({"command": "uv run pytest", "timeout": 120})

    assert out == "ran:uv run pytest"
    assert fake.timeout == 120.0


@pytest.mark.asyncio
async def test_bash_tool_no_upper_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large values are honored as-is: long training/builds are legitimate."""
    fake = _FakeRunBash()
    monkeypatch.setattr(bash_module, "_run_bash", fake)

    out = await bash_module.bash.handler(
        {"command": "python train.py", "timeout": 14400}
    )

    assert fake.timeout == 14400.0
    assert "note" not in out.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-5, 0, "abc", True, None])
async def test_bash_tool_invalid_timeout_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    bad: object,
) -> None:
    """Unusable values (<=0 for bash, non-numeric, bool, None) -> default + note."""
    fake = _FakeRunBash()
    monkeypatch.setattr(bash_module, "_run_bash", fake)

    out = await bash_module.bash.handler({"command": "x", "timeout": bad})

    assert fake.timeout == DEFAULT_TIMEOUT_SECONDS
    assert out.startswith("Note: invalid timeout")
    assert "ran:x" in out


@pytest.mark.asyncio
async def test_bash_tool_numeric_string_timeout_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some providers send numbers as strings: coerce silently, no note."""
    fake = _FakeRunBash()
    monkeypatch.setattr(bash_module, "_run_bash", fake)

    out = await bash_module.bash.handler({"command": "x", "timeout": "45"})

    assert fake.timeout == 45.0
    assert "note" not in out.lower()


@pytest.mark.asyncio
async def test_bash_timeout_override_real_e2e() -> None:
    """The override reaches ``asyncio.wait_for`` end-to-end (wire check)."""
    # 1.2 s of sleep under a 5 s budget: finishes normally.
    cmd = f'{sys.executable} -c "import time; time.sleep(1.2)"'
    out = await bash_module.bash.handler({"command": cmd, "timeout": 5})
    assert "ran" not in out  # real output, not the fake
    assert "timed out" not in out.lower()

    # 2 s of sleep under a 0.5 s budget: killed by the override.
    cmd = f'{sys.executable} -c "import time; time.sleep(2)"'
    out = await bash_module.bash.handler({"command": cmd, "timeout": 0.5})
    assert "timed out" in out.lower()
