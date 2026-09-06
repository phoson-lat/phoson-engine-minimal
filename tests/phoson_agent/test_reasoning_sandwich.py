"""Unit tests for the per-phase reasoning-effort scheduling (issue #145).

Covers:
* the pure phase-detection and effort-resolution helpers,
* the scheduler factories (fixed and live),
* the outer ``AgentEngine`` loop deriving a per-iteration effort from the
  scheduler and passing it to the LLM,
* the global override winning over the per-phase profile,
* the OpenAI-compatible adapter forwarding the per-request effort.

No LLM is called: the loop test drives ``AgentEngine`` with a recording fake
chat, and the adapter test calls the pure ``_build_request_kwargs`` directly.
"""

from collections.abc import AsyncIterator

import pytest

from phoson_agent import AgentEngine
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
)
from phoson_agent.models import AgentTool
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent.reasoning_effort import (
    DEFAULT_PHASE_PROFILE,
    detect_phase,
    make_live_scheduler,
    resolve_phase_effort,
    build_effort_scheduler,
)

# ── Pure helpers ─────────────────────────────────────────────────────────────


class TestDetectPhase:
    def test_first_iteration_is_planning(self) -> None:
        assert detect_phase(0, False) == "planning"
        # Even a tool error on the first step is still "planning" — the model
        # has not yet entered the execution loop.
        assert detect_phase(0, True) == "planning"

    def test_after_tool_failure_is_verification(self) -> None:
        assert detect_phase(1, True) == "verification"
        assert detect_phase(7, True) == "verification"

    def test_after_success_is_execution(self) -> None:
        assert detect_phase(1, False) == "execution"
        assert detect_phase(4, False) == "execution"


class TestResolvePhaseEffort:
    def test_default_profile(self) -> None:
        assert resolve_phase_effort("planning") == "high"
        assert resolve_phase_effort("execution") == "low"
        assert resolve_phase_effort("verification") == "high"

    def test_custom_profile(self) -> None:
        profile = {"planning": "max", "execution": "medium", "verification": "max"}
        assert resolve_phase_effort("planning", profile=profile) == "max"
        assert resolve_phase_effort("execution", profile=profile) == "medium"
        assert resolve_phase_effort("verification", profile=profile) == "max"

    def test_override_wins_over_profile(self) -> None:
        profile = {"planning": "high", "execution": "low", "verification": "high"}
        for phase in ("planning", "execution", "verification"):
            assert resolve_phase_effort(phase, profile=profile, override="max") == "max"

    def test_none_override_uses_profile(self) -> None:
        assert resolve_phase_effort("planning", profile=None, override=None) == "high"

    def test_invalid_override_normalised_to_none(self) -> None:
        # A typo (or a level some backends reject) must not leak through.
        assert resolve_phase_effort("planning", override="bogus") is None

    def test_invalid_profile_entry_normalised_to_none(self) -> None:
        assert resolve_phase_effort("planning", profile={"planning": "bogus"}) is None

    def test_missing_phase_in_profile_is_none(self) -> None:
        assert resolve_phase_effort("execution", profile={"planning": "high"}) is None

    def test_default_profile_is_consistent(self) -> None:
        # Guard the documented conservative default.
        assert DEFAULT_PHASE_PROFILE == {
            "planning": "high",
            "execution": "low",
            "verification": "high",
        }


class TestBuildEffortScheduler:
    def test_scheduler_applies_profile_by_iteration(self) -> None:
        sched = build_effort_scheduler()  # default profile, no override
        assert sched(0, False) == "high"  # planning
        assert sched(1, False) == "low"  # execution
        assert sched(1, True) == "high"  # verification
        assert sched(2, False) == "low"  # execution

    def test_scheduler_with_override_is_constant(self) -> None:
        sched = build_effort_scheduler(override="max")
        assert sched(0, False) == "max"
        assert sched(3, True) == "max"


class TestMakeLiveScheduler:
    def test_reads_override_dynamically(self) -> None:
        state: dict[str, str | None] = {"effort": None}
        sched = make_live_scheduler(get_override=lambda: state["effort"])
        # No override yet -> profile.
        assert sched(0, False) == "high"
        # A mid-session /reasoning-effort now wins.
        state["effort"] = "max"
        assert sched(0, False) == "max"
        assert sched(2, True) == "max"

    def test_reads_profile_dynamically(self) -> None:
        state: dict[str, dict[str, str] | None] = {"profile": None}
        sched = make_live_scheduler(
            get_override=lambda: None,
            get_profile=lambda: state["profile"],
        )
        assert sched(0, False) == "high"  # default profile
        state["profile"] = {"planning": "max", "execution": "medium"}
        assert sched(0, False) == "max"
        assert sched(1, False) == "medium"


# ── AgentEngine loop integration (fake chat, no LLM) ─────────────────────────


class _RecordingChat(BaseLLMChat):
    """Emit a tool call for the first ``tool_calls`` iterations, then finish.

    Records ``config.reasoning_effort`` on every call so the test can assert
    the per-iteration effort the engine actually sent.
    """

    def __init__(self, *, tool_calls: int) -> None:
        self._tool_calls = tool_calls
        self._n = 0
        self.seen_efforts: list[str | None] = []

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self._n += 1
        self.seen_efforts.append(config.reasoning_effort)
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        if self._n <= self._tool_calls:
            yield ToolCallEvent(
                index=0,
                tool_call_id=f"call_{self._n}",
                tool_name="flaky",
                args={},
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=10, output=5),
                cost_usd=0.0,
                cost_known=False,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
        else:
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=10, output=5),
                cost_usd=0.0,
                cost_known=False,
            )
            yield LLMDoneEvent(content="done", has_tool_calls=False)


def _make_flaky_tool(fail_on: dict[str, int]) -> AgentTool:
    """A tool that raises on a specific call number (recorded in ``fail_on``)."""

    state = {"count": 0}

    def flaky(args: dict, context: object = None) -> str:
        state["count"] += 1
        if fail_on and state["count"] == fail_on.get("n"):
            raise RuntimeError("boom: test failure")
        return "ok"

    return AgentTool(
        name="flaky",
        description="A test tool that can fail on a specific call.",
        parameters={"type": "object", "properties": {}},
        handler=flaky,
    )


@pytest.mark.asyncio
async def test_loop_schedules_per_phase_effort() -> None:
    """Planning=high, execution=low, verification=high, execution=low."""
    chat = _RecordingChat(tool_calls=3)
    # Call numbers: 1 -> ok, 2 -> FAIL, 3 -> ok, 4 -> final (no tool).
    engine = AgentEngine(
        chat=chat,
        tools=[_make_flaky_tool({"n": 2})],
        effort_scheduler=build_effort_scheduler(),  # default profile
        max_iterations=8,
    )
    await engine.run([Message(role="user", content="go")], ModelConfig(model="m"))

    assert chat.seen_efforts == ["high", "low", "high", "low"]


@pytest.mark.asyncio
async def test_loop_override_wins_over_profile() -> None:
    """A global /reasoning-effort override is used for every iteration."""
    chat = _RecordingChat(tool_calls=2)
    engine = AgentEngine(
        chat=chat,
        tools=[_make_flaky_tool({"n": 2})],
        effort_scheduler=build_effort_scheduler(override="max"),
        max_iterations=8,
    )
    await engine.run([Message(role="user", content="go")], ModelConfig(model="m"))

    assert chat.seen_efforts == ["max", "max", "max"]


@pytest.mark.asyncio
async def test_loop_no_scheduler_keeps_config_effort() -> None:
    """Without a scheduler the caller's config.reasoning_effort is unchanged."""
    chat = _RecordingChat(tool_calls=1)
    engine = AgentEngine(
        chat=chat,
        tools=[_make_flaky_tool({})],
        max_iterations=8,
    )
    await engine.run(
        [Message(role="user", content="go")],
        ModelConfig(model="m", reasoning_effort="xhigh"),
    )

    # The caller's effort is reused for every iteration.
    assert chat.seen_efforts == ["xhigh", "xhigh"]


@pytest.mark.asyncio
async def test_loop_none_override_falls_back_to_profile() -> None:
    """A live scheduler whose override is None uses the per-phase profile."""
    chat = _RecordingChat(tool_calls=2)
    state: dict[str, str | None] = {"effort": None}
    engine = AgentEngine(
        chat=chat,
        tools=[_make_flaky_tool({"n": 2})],
        effort_scheduler=make_live_scheduler(get_override=lambda: state["effort"]),
        max_iterations=8,
    )
    await engine.run([Message(role="user", content="go")], ModelConfig(model="m"))

    assert chat.seen_efforts == ["high", "low", "high"]


# ── Adapter: per-request effort forwarding ───────────────────────────────────


def test_openai_compatible_forwards_reasoning_effort() -> None:
    """The effort derived per request reaches the OpenAI-compatible call."""
    from phoson_llm.chats._openai_compatible import _build_request_kwargs

    for effort in ("low", "high", "max"):
        kwargs = _build_request_kwargs(
            config=ModelConfig(model="o3", reasoning_effort=effort),
            messages=[Message(role="user", content="hi")],
            tools=None,
            max_tokens_key="max_completion_tokens",
        )
        assert kwargs["reasoning_effort"] == effort

    # No effort -> no key (and temperature is kept).
    kwargs = _build_request_kwargs(
        config=ModelConfig(model="gpt-4o"),
        messages=[Message(role="user", content="hi")],
        tools=None,
        max_tokens_key="max_completion_tokens",
    )
    assert "reasoning_effort" not in kwargs
    assert "temperature" in kwargs
