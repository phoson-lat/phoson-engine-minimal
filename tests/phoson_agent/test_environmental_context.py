"""Disabled env injection, legacy cleanup and compatible lifecycle/wiring."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from phoson_llm.schemas import Message, TextBlock, ModelConfig
from phoson_agent.models import AgentStartEvent, AgentTokenEvent
from phoson_agent.middleware import EnvironmentalContextMiddleware, is_env_context

# ── helpers ──────────────────────────────────────────────────────────────────


def _config() -> ModelConfig:
    return ModelConfig(model="test-model")


def _msgs(n: int = 5) -> list[Message]:
    return [Message(role="user", content=f"msg {i}") for i in range(n)]


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


# ── injection is disabled; legacy blocks are cleaned up ─────────────────────


class TestInjectionDisabled:
    async def test_no_env_block_appended(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        result = await mw.on_before_llm(_msgs(5), _config())
        assert len(result) == 5
        assert result == _msgs(5)
        assert all(not is_env_context(m) for m in result)

    async def test_original_list_not_mutated(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        msgs = _msgs(5)
        snapshot = list(msgs)
        result = await mw.on_before_llm(msgs, _config())
        assert msgs == snapshot
        assert result is not msgs

    async def test_legacy_env_block_stripped(self):
        """#212: env blocks are request artifacts, not genuine turns. Even
        with injection off, any legacy block already in the history is
        dropped instead of being sent to the provider or persisted."""
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        legacy = Message(role="user", content="[env: step 1/20]")
        result = await mw.on_before_llm([legacy] + _msgs(3), _config())
        assert len(result) == 3
        assert all(not is_env_context(m) for m in result)

    async def test_multiple_legacy_blocks_stripped(self):
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        legacy_a = Message(role="user", content="[env: step 1/20]")
        legacy_b = Message(
            role="user",
            content="[env: step 2/20, time 45s elapsed, 555s remaining]",
        )
        kept = [
            Message(role="system", content="System prompt"),
            Message(role="user", content=[TextBlock(text="User text")]),
            Message(role="assistant", content="Answer", reasoning="Reasoning"),
        ]
        msgs = [legacy_a, kept[0], legacy_b, *kept[1:], legacy_a]
        snapshot = deepcopy(msgs)
        result = await mw.on_before_llm(msgs, _config())
        assert result == kept
        assert all(actual is original for actual, original in zip(result, kept))
        assert result is not msgs
        assert msgs == snapshot
        assert all(not is_env_context(m) for m in result)
        assert await mw.on_before_llm(result, _config()) == kept

    async def test_genuine_user_turn_with_env_like_text_kept(self):
        """The prefix match requires an exact ``[env: `` start; real turns
        mentioning env text elsewhere (or with different casing/spacing) are
        preserved untouched."""
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        kept = [
            Message(role="user", content="Please explain the [env: prefix]"),
            Message(role="user", content="[env] no space-colon"),
            Message(role="user", content="[Env: step 1/20]"),
            Message(role="user", content="[env:step 1/20]"),
            Message(role="user", content=" [env: step 1/20]"),
            Message(role="assistant", content="[env: step 1/20]"),
            Message(role="system", content="[env: step 1/20]"),
            Message(role="user", content=[TextBlock(text="[env: step 1/20]")]),
        ]
        result = await mw.on_before_llm(kept, _config())
        assert result == kept

    async def test_no_block_on_repeated_calls(self):
        """Repeated calls (as the engine makes per LLM turn) never add an
        env block, so nothing accumulates in history."""
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        history = _msgs(3)
        for _ in range(3):
            history = await mw.on_before_llm(
                history + [Message(role="assistant", content="a")], _config()
            )
        assert all(not is_env_context(m) for m in history)
        assert len(history) == 6  # 3 initial + 3 assistant, nothing added

    @pytest.mark.parametrize("budget", [None, 0, -1, 600])
    @pytest.mark.parametrize("count", [0, 2])
    async def test_disabled_with_any_budget_or_empty_history(self, budget, count):
        mw = EnvironmentalContextMiddleware(max_iterations=7, run_budget_seconds=budget)
        result = await mw.on_before_llm(_msgs(count), _config())
        assert result == _msgs(count)
        assert all(not is_env_context(m) for m in result)

    async def test_only_legacy_blocks_produce_empty_history(self):
        mw = EnvironmentalContextMiddleware()
        msgs = [Message(role="user", content="[env: step 1/20]")]
        snapshot = deepcopy(msgs)
        assert await mw.on_before_llm(msgs, _config()) == []
        assert msgs == snapshot


# ── step counting ────────────────────────────────────────────────────────────


class TestStepCounting:
    async def test_step_increments_per_call_without_output(self):
        """The per-run counter still advances (compat), but no step text is
        computed or attached to any message."""
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        result = await mw.on_before_llm(_msgs(2), _config())
        assert mw._step == 1
        await mw.on_before_llm(_msgs(2), _config())
        assert mw._step == 2
        assert result == _msgs(2)

    @pytest.mark.parametrize("max_iterations", [0, -1])
    def test_invalid_max_iterations_raises(self, max_iterations):
        with pytest.raises(ValueError, match="max_iterations must be > 0"):
            EnvironmentalContextMiddleware(max_iterations=max_iterations)


# ── time / budget ────────────────────────────────────────────────────────────


class TestTimeBudget:
    async def test_clock_only_used_to_start_run_not_compute_budget(self, monkeypatch):
        import phoson_agent.middleware as mw_mod

        clock = Mock(return_value=1000.0)
        # Replace the module reference, not the event loop's shared clock.
        monkeypatch.setattr(mw_mod, "time", Mock(monotonic=clock))
        mw = EnvironmentalContextMiddleware(run_budget_seconds=600)
        msgs = _msgs(2)
        assert await mw.on_before_llm(msgs, _config()) == msgs
        clock.assert_called_once_with()
        clock.return_value = 2000.0  # Even an exhausted budget adds nothing.
        assert await mw.on_before_llm(msgs, _config()) == msgs
        clock.assert_called_once_with()


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
        assert mw._step == 1
        assert result == _msgs()

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

    async def test_start_time_lazily_set_on_first_call(self, monkeypatch):
        import phoson_agent.middleware as mw_mod

        monkeypatch.setattr(mw_mod.time, "monotonic", _fake_clock([1000.0]))
        mw = EnvironmentalContextMiddleware(max_iterations=20)
        assert mw._start_time is None
        await mw.on_before_llm(_msgs(), _config())
        assert mw._start_time == 1000.0


# ── build_middlewares wiring ─────────────────────────────────────────────────


class TestBuildMiddlewaresWiring:
    def _permission(self):
        from phoson_agent.permissions import PermissionPolicy, PermissionMiddleware

        return PermissionMiddleware(PermissionPolicy())

    def _chain(self, *, summarizer=None, **overrides):
        from phoson_cli.config import PhosonConfig
        from phoson_cli.session_utils import build_middlewares

        config = PhosonConfig(**overrides)
        return build_middlewares(
            config=config,
            offload=None,
            summarizer=summarizer,
            permission=self._permission(),
        )

    def test_env_middleware_present_and_after_summarizer(self):
        from phoson_agent.permissions import PermissionMiddleware
        from phoson_agent.plugins.summarizer import SummarizationMiddleware

        summarizer = SummarizationMiddleware(provider="echo", model="echo")
        chain = self._chain(summarizer=summarizer)
        env = next(m for m in chain if isinstance(m, EnvironmentalContextMiddleware))
        env_idx = chain.index(env)
        # The permission gate is always last in the chain.
        assert isinstance(chain[-1], PermissionMiddleware)
        # The env middleware must sit after any summarizer compaction.
        summarizers = [
            i for i, m in enumerate(chain) if isinstance(m, SummarizationMiddleware)
        ]
        assert summarizers
        assert env_idx > max(summarizers)

    def test_env_middleware_uses_config_values(self):
        chain = self._chain(max_iterations=42, run_budget_seconds=120.0)
        env = next(m for m in chain if isinstance(m, EnvironmentalContextMiddleware))
        assert env._max_iterations == 42
        assert env._run_budget == 120.0

    def test_env_middleware_present_even_when_loop_detect_off(self):
        chain = self._chain(loop_detect_n=0)
        assert any(isinstance(m, EnvironmentalContextMiddleware) for m in chain)
