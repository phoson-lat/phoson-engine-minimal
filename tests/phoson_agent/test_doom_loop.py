"""Unit tests for DoomLoopMiddleware (#142) and its build_middlewares wiring."""

import pytest

from phoson_llm.schemas import ToolCallEvent
from phoson_agent.models import AgentStartEvent, AgentTokenEvent
from phoson_agent.exceptions import PhosonAgentError, DoomLoopDetectedError
from phoson_agent.middleware import DoomLoopMiddleware

# ── helpers ──────────────────────────────────────────────────────────────────


def _call(name: str = "bash", args: dict | None = None, call_id: str = "c1"):
    return ToolCallEvent(
        tool_call_id=call_id,
        tool_name=name,
        args=args if args is not None else {"command": "pytest -q"},
    )


async def _fail(mw, *, name="bash", args=None, call_id="c", result="boom"):
    """Feed one failing call and return the (possibly annotated) result."""
    call = _call(name=name, args=args, call_id=call_id)
    return await mw.on_after_tool(call, result, True)


# ── inject mode ──────────────────────────────────────────────────────────────


class TestInjectMode:
    async def test_triggers_on_n_consecutive_errors(self):
        mw = DoomLoopMiddleware(n=3, mode="inject")
        out1 = await _fail(mw, call_id="c0")
        out2 = await _fail(mw, call_id="c1")
        assert "doom-loop" not in out1
        assert "doom-loop" not in out2
        out3 = await _fail(mw, call_id="c2")
        assert "doom-loop" in out3
        assert "3 times" in out3

    async def test_injection_fires_only_once(self):
        # The observation is appended exactly once (on the n-th error); the
        # subsequent identical error must NOT get it appended again.
        mw = DoomLoopMiddleware(n=2, mode="inject")
        await _fail(mw, call_id="c0")
        out2 = await _fail(mw, call_id="c1")
        out3 = await _fail(mw, call_id="c2")
        assert out2.count("[⚠ doom-loop]") == 1
        # Already triggered → the 3rd error's fresh result is left untouched.
        assert out3.count("[⚠ doom-loop]") == 0

    async def test_distinct_args_do_not_trigger(self):
        mw = DoomLoopMiddleware(n=3, mode="inject")
        for i, cmd in enumerate(["pytest -q", "pytest -v", "pytest -q -x"]):
            out = await _fail(mw, args={"command": cmd}, call_id=f"c{i}")
            assert "doom-loop" not in out

    async def test_success_resets_counter(self):
        mw = DoomLoopMiddleware(n=3, mode="inject")
        await _fail(mw, call_id="c0")
        await _fail(mw, call_id="c1")
        # A success breaks the loop for this fingerprint.
        await mw.on_after_tool(_call(call_id="c2"), "ok", False)
        out = await _fail(mw, call_id="c3")
        out2 = await _fail(mw, call_id="c4")
        assert "doom-loop" not in out
        assert "doom-loop" not in out2  # only 2 errors after the reset

    async def test_different_tools_do_not_cross_trigger(self):
        mw = DoomLoopMiddleware(n=2, mode="inject")
        await _fail(mw, name="bash", call_id="c0")
        out = await _fail(mw, name="read_file", args={"path": "x"}, call_id="c1")
        assert "doom-loop" not in out


# ── normalization ────────────────────────────────────────────────────────────


class TestNormalization:
    async def test_whitespace_collapsed(self):
        mw = DoomLoopMiddleware(n=2, mode="inject")
        await _fail(mw, args={"command": "pytest  -q"}, call_id="c0")
        out = await _fail(mw, args={"command": "pytest -q "}, call_id="c1")
        assert "doom-loop" in out

    async def test_key_order_ignored(self):
        mw = DoomLoopMiddleware(n=2, mode="inject")
        await _fail(mw, args={"a": "1", "b": "2"}, call_id="c0")
        out = await _fail(mw, args={"b": "2", "a": "1"}, call_id="c1")
        assert "doom-loop" in out

    async def test_fingerprint_is_stable(self):
        mw = DoomLoopMiddleware(n=3)
        a = mw._fingerprint(_call(args={"command": "pytest -q"}, call_id="x"))
        b = mw._fingerprint(_call(args={"command": "pytest  -q "}, call_id="y"))
        assert a == b
        c = mw._fingerprint(_call(args={"command": "pytest -v"}, call_id="z"))
        assert a != c


# ── abort mode ───────────────────────────────────────────────────────────────


class TestAbortMode:
    async def test_raises_on_next_identical_call(self):
        mw = DoomLoopMiddleware(n=3, mode="abort")
        for i in range(3):
            await _fail(mw, call_id=f"c{i}")
        with pytest.raises(DoomLoopDetectedError):
            await mw.on_before_tool(_call(call_id="c3"))

    async def test_allows_distinct_args(self):
        mw = DoomLoopMiddleware(n=2, mode="abort")
        await _fail(mw, args={"command": "pytest -q"}, call_id="c0")
        await _fail(mw, args={"command": "pytest -q"}, call_id="c1")
        distinct = _call(args={"command": "pytest -v"}, call_id="c2")
        out = await mw.on_before_tool(distinct)
        assert out is not None

    async def test_allows_different_tool(self):
        mw = DoomLoopMiddleware(n=2, mode="abort")
        await _fail(mw, name="bash", call_id="c0")
        await _fail(mw, name="bash", call_id="c1")
        other = _call(name="read_file", args={"path": "x"}, call_id="c2")
        out = await mw.on_before_tool(other)
        assert out is not None

    async def test_error_carries_tool_name(self):
        mw = DoomLoopMiddleware(n=1, mode="abort")
        await _fail(mw, name="bash", call_id="c0")
        with pytest.raises(DoomLoopDetectedError) as exc_info:
            await mw.on_before_tool(_call(name="bash", call_id="c1"))
        assert exc_info.value.tool_name == "bash"

    async def test_error_is_phoson_agent_error(self):
        assert issubclass(DoomLoopDetectedError, PhosonAgentError)


# ── reset / lifecycle ────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_reset_on_agent_start(self):
        mw = DoomLoopMiddleware(n=3, mode="inject")
        await _fail(mw, call_id="c0")
        await _fail(mw, call_id="c1")
        await mw.on_agent_event(AgentStartEvent())
        out = await _fail(mw, call_id="c2")
        out2 = await _fail(mw, call_id="c3")
        assert "doom-loop" not in out
        assert "doom-loop" not in out2

    async def test_non_start_event_does_not_reset(self):
        mw = DoomLoopMiddleware(n=2, mode="inject")
        await _fail(mw, call_id="c0")
        await mw.on_agent_event(AgentTokenEvent(content="x"))
        out = await _fail(mw, call_id="c1")
        assert "doom-loop" in out

    async def test_n_zero_is_off(self):
        mw = DoomLoopMiddleware(n=0, mode="inject")
        for i in range(5):
            out = await _fail(mw, call_id=f"c{i}")
            assert "doom-loop" not in out
        # abort mode is also a no-op when disabled.
        mw2 = DoomLoopMiddleware(n=0, mode="abort")
        for i in range(5):
            await _fail(mw2, call_id=f"d{i}")
        out = await mw2.on_before_tool(_call(call_id="d5"))
        assert out is not None

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            DoomLoopMiddleware(n=3, mode="bogus")


# ── build_middlewares wiring ─────────────────────────────────────────────────


class TestBuildMiddlewaresWiring:
    def _permission(self):
        from phoson_agent.permissions import PermissionPolicy, PermissionMiddleware

        return PermissionMiddleware(PermissionPolicy())

    def _chain(self, **overrides):
        from phoson_cli.config import PhosonConfig
        from phoson_cli.session_utils import build_middlewares

        config = PhosonConfig(**overrides)
        return build_middlewares(
            config=config, offload=None, summarizer=None, permission=self._permission()
        )

    def test_doom_loop_present_by_default(self):
        from phoson_agent.middleware import EnvironmentalContextMiddleware
        from phoson_agent.permissions import PermissionMiddleware

        chain = self._chain()
        assert any(isinstance(m, DoomLoopMiddleware) for m in chain)
        # EnvironmentalContext is always present; the permission gate is last.
        assert any(isinstance(m, EnvironmentalContextMiddleware) for m in chain)
        assert isinstance(chain[-1], PermissionMiddleware)

    def test_doom_loop_off_when_n_is_zero(self):
        chain = self._chain(loop_detect_n=0)
        assert not any(isinstance(m, DoomLoopMiddleware) for m in chain)

    def test_doom_loop_uses_configured_n_and_mode(self):
        chain = self._chain(loop_detect_n=5, loop_detect_mode="abort")
        doom = next(m for m in chain if isinstance(m, DoomLoopMiddleware))
        assert doom._n == 5
        assert doom._mode == "abort"

    def test_doom_loop_sits_before_permission(self):
        from phoson_agent.permissions import PermissionMiddleware

        chain = self._chain()
        doom_idx = next(
            i for i, m in enumerate(chain) if isinstance(m, DoomLoopMiddleware)
        )
        perm_idx = next(
            i for i, m in enumerate(chain) if isinstance(m, PermissionMiddleware)
        )
        assert doom_idx < perm_idx
