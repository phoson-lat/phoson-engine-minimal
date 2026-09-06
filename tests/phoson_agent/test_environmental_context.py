"""Unit tests for EnvironmentalContextMiddleware (#143) and its wiring."""

import pytest

from phoson_llm.schemas import Message, ModelConfig
from phoson_agent.models import AgentStartEvent, AgentTokenEvent
from phoson_agent.middleware import EnvironmentalContextMiddleware

# ── helpers ──────────────────────────────────────────────────────────────────


def _config() -> ModelConfig:
    return ModelConfig(model="test-model")


def _msgs(n: int = 5) -> list[Message]:
    return [Message(role="user", content=f"msg {i}") for i in range(n)]


def _block_text(result: list[Message]) -> str:
    """The text of the trailing env block (plain string content)."""
    last = result[-1]
    assert isinstance(last.content, str)
    return last.content


def _fake_clock(values):
    """A ``time.monotonic`` stand-in that yields ``values`` in order and then
    clamps to the last one (never raises) — safe for post-test teardown,
    which may read the clock again after the iterator is exhausted."""
    state = {"i": 0}

    def _clock():
        i = state["i"]
        state["i"] = i + 1
        return values[min(i, len(values) - 1)]

    return _clock


# ── block shape & position ───────────────────────────────────────────────────


class TestBlockShape:
    async def test_block_appended_at_end(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        msgs = _msgs(5)
        result = await mw.on_before_llm(msgs, _config())
        assert len(result) == 6
        assert result[-1].role == "user"
        assert _block_text(result).startswith("[env: ")
        assert _block_text(result).endswith("]")

    async def test_original_list_not_mutated(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        msgs = _msgs(5)
        snapshot = list(msgs)
        await mw.on_before_llm(msgs, _config())
        assert msgs == snapshot
        assert len(msgs) == 5  # the original is untouched

    async def test_block_is_single_line(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        result = await mw.on_before_llm(_msgs(), _config())
        assert "\n" not in _block_text(result)

    async def test_injected_on_every_call_including_first(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        result = await mw.on_before_llm(_msgs(), _config())
        assert "step 1/20" in _block_text(result)


# ── step counting ────────────────────────────────────────────────────────────


class TestStepCounting:
    async def test_step_increments_per_call(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        first = await mw.on_before_llm(_msgs(), _config())
        assert "step 1/20" in _block_text(first)
        third = None
        for _ in range(2):
            third = await mw.on_before_llm(_msgs(), _config())
        assert "step 3/20" in _block_text(third)

    async def test_max_iterations_in_block(self):
        mw = EnvironmentalContextMiddleware(max_iterations=7)
        result = await mw.on_before_llm(_msgs(), _config())
        assert "step 1/7" in _block_text(result)

    def test_invalid_max_iterations_raises(self):
        with pytest.raises(ValueError):
            EnvironmentalContextMiddleware(max_iterations=0)


# ── time / budget ────────────────────────────────────────────────────────────


class TestTimeBudget:
    async def test_with_budget_shows_elapsed_and_remaining(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20, run_budget_seconds=600)
        result = await mw.on_before_llm(_msgs(), _config())
        text = _block_text(result)
        assert "elapsed" in text
        assert "remaining" in text
        # The budget was just started, so almost the full 600s remains.
        assert "599s remaining" in text or "600s remaining" in text

    async def test_without_budget_only_step(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20, run_budget_seconds=None)
        result = await mw.on_before_llm(_msgs(), _config())
        text = _block_text(result)
        assert "step 1/20" in text
        assert "elapsed" not in text
        assert "remaining" not in text

    async def test_zero_budget_means_no_time(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20, run_budget_seconds=0)
        result = await mw.on_before_llm(_msgs(), _config())
        assert "elapsed" not in _block_text(result)

    async def test_time_uses_monotonic(self, monkeypatch):
        import phoson_agent.middleware as mw_mod

        # on_before_llm reads the clock twice: once to (re)start, once for
        # elapsed. 3rd value is a clamp for post-test teardown.
        monkeypatch.setattr(mw_mod.time, "monotonic", _fake_clock([1000.0, 1045.0]))
        mw = EnvironmentalContextMiddleware(max_iterations=20, run_budget_seconds=600)
        result = await mw.on_before_llm(_msgs(), _config())
        text = _block_text(result)
        assert "45s elapsed" in text
        assert "555s remaining" in text


# ── reset / lifecycle ────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_reset_on_agent_start(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        await mw.on_before_llm(_msgs(), _config())
        await mw.on_before_llm(_msgs(), _config())
        assert mw._step == 2
        await mw.on_agent_event(AgentStartEvent())
        assert mw._step == 0
        result = await mw.on_before_llm(_msgs(), _config())
        assert "step 1/20" in _block_text(result)

    async def test_non_start_event_does_not_reset(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        await mw.on_before_llm(_msgs(), _config())
        await mw.on_agent_event(AgentTokenEvent(content="x"))
        assert mw._step == 1

    def test_reset_restarts_clock(self, monkeypatch):
        import phoson_agent.middleware as mw_mod

        monkeypatch.setattr(mw_mod.time, "monotonic", _fake_clock([1000.0]))
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        mw.reset()
        assert mw._step == 0
        assert mw._start_time == 1000.0


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

    def test_env_middleware_present_and_after_summarizer(self):
        from phoson_agent.permissions import PermissionMiddleware

        chain = self._chain()
        env = next(m for m in chain if isinstance(m, EnvironmentalContextMiddleware))
        env_idx = chain.index(env)
        # The permission gate is always last in the chain.
        assert isinstance(chain[-1], PermissionMiddleware)
        # The env block must be appended after any summarizer compaction.
        from phoson_agent.plugins.summarizer import SummarizationMiddleware

        summarizers = [
            i for i, m in enumerate(chain) if isinstance(m, SummarizationMiddleware)
        ]
        if summarizers:
            assert env_idx > max(summarizers)

    def test_env_middleware_uses_config_values(self):
        chain = self._chain(max_iterations=42, run_budget_seconds=120.0)
        env = next(m for m in chain if isinstance(m, EnvironmentalContextMiddleware))
        assert env._max_iterations == 42
        assert env._run_budget == 120.0

    def test_env_middleware_present_even_when_loop_detect_off(self):
        chain = self._chain(loop_detect_n=0)
        assert any(isinstance(m, EnvironmentalContextMiddleware) for m in chain)
