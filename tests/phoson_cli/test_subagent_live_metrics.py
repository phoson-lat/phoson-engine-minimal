"""Tests for IMPROVEMENTS.md E2 — live sub-agent metrics.

Covers the whole producer→consumer chain:

* the ``SubagentProgressTracker`` state machine (register / start /
  update / finalize / mark_error);
* the ``agents`` tool feeding its per-call tracker **live** from the
  inner runs' ``AgentStepDoneEvent`` stream (intermediate values
  visible while the task is still running, plus the final snap) and
  pushing it to the UI through the injected ``on_subagent_progress``
  callback;
* the running panel rendering live Time/Tokens/Cost per task (queued
  rows stay "waiting", and the fallback without a tracker is intact);
* the controller injecting the UI callback into the engine context;
* the fullscreen sink rendering the tracker into the panel.
"""

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from collections.abc import AsyncIterator

import pytest

from phoson_cli.theme import DARK
from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolCallEvent,
)
from phoson_agent.models import (
    RunStep,
    AgentDoneEvent,
    AgentRunResult,
    AgentStartEvent,
    AgentToolStartEvent,
)
from phoson_cli.controller import SessionController
from phoson_llm.chats.base import BaseLLMChat
from phoson_cli.tools.subagent import (
    SubagentProgressTracker,
    agent,
    agents,
)
from phoson_cli.fullscreen.sink import FullScreenSink
from phoson_cli.tools.subagent_panel import (
    AgentStatus,
    SubagentProgress,
    render_subagent_panel,
    render_subagent_panel_frame,
)

UTC = datetime.UTC


# ── helpers ──────────────────────────────────────────────────────────────────


def _now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _llm_step(*, cost: float = 0.001, tin: int = 10, tout: int = 5) -> RunStep:
    now = _now()
    return RunStep(
        kind="llm",
        started_at=now,
        ended_at=now,
        duration_ms=10,
        model="fake",
        usage=TokenUsage(input=tin, output=tout),
        cost_usd=cost,
        credits=cost,
    )


def _tool_step() -> RunStep:
    now = _now()
    return RunStep(kind="tool", started_at=now, ended_at=now, duration_ms=1)


class TwoTurnChat(BaseLLMChat):
    """Inner-run chat: LLM call 1 returns a tool call, call 2 the answer.

    Emits a ``UsageEvent`` after each LLM call so the inner engine builds
    real ``RunStep``s with usage — the same shape production streams have.
    The tool call in between guarantees the first LLM step completes
    (and reports tokens/cost) while the sub-agent is still running.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        if self.calls == 1:
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=10, output=5),
                cost_usd=0.001,
            )
            yield ToolCallEvent(
                tool_call_id="call_1",
                tool_name="read_file",
                args={"path": "x.txt"},
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
        else:
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=20, output=8),
                cost_usd=0.002,
            )
            yield LLMDoneEvent(content="done", has_tool_calls=False)


class EchoTool:
    """Trivial tool for the inner run's tool call."""

    name = "read_file"
    description = "read a file"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }

    async def handler(self, args, context=None):
        return "file content"


# ── tracker unit ─────────────────────────────────────────────────────────────


def test_tracker_register_queues_tasks() -> None:
    t = SubagentProgressTracker()
    assert t.register("task-a") == 0
    assert t.register("task-b") == 1
    assert [p.task for p in t.tasks] == ["task-a", "task-b"]
    assert all(p.status == AgentStatus.RUNNING for p in t.tasks)
    # Queued: no clock until start().
    assert all(p.started_at == 0 for p in t.tasks)
    t.start(0)
    assert t.tasks[0].started_at > 0
    assert t.tasks[1].started_at == 0


def test_tracker_register_many() -> None:
    t = SubagentProgressTracker()
    indexes = t.register_many(["a", "b", "c"])
    assert indexes == [0, 1, 2]
    assert len(t.tasks) == 3


def test_tracker_update_from_step_accumulates_llm_steps_only() -> None:
    t = SubagentProgressTracker()
    t.register("task")
    t.update_from_step(0, _llm_step(cost=0.001, tin=10, tout=5))
    t.update_from_step(0, _tool_step())  # ignored: not an LLM step
    t.update_from_step(0, _llm_step(cost=0.002, tin=20, tout=8))
    p = t.tasks[0]
    assert p.input_tokens == 30
    assert p.output_tokens == 13
    assert p.cost_usd == pytest.approx(0.003)
    assert p.has_tokens is True


def test_tracker_update_from_step_ignores_bad_index_and_none() -> None:
    t = SubagentProgressTracker()
    t.register("task")
    t.update_from_step(99, _llm_step())  # out of range → no-op
    t.update_from_step(0, None)  # no-op
    assert t.tasks[0].input_tokens == 0


def test_tracker_finalize_snaps_result_metrics() -> None:
    t = SubagentProgressTracker()
    t.register("task")
    t.start(0)
    t.update_from_step(0, _llm_step(cost=0.001, tin=10, tout=5))
    result = AgentRunResult(
        final_content="done",
        history=[],
        input_messages=[],
        steps=[
            _llm_step(cost=0.001, tin=10, tout=5),
            _llm_step(cost=0.002, tin=20, tout=8),
        ],
        total_cost_usd=0.003,
        total_credits=0.003,
    )
    t.finalize(0, duration_ms=420, result=result)
    p = t.tasks[0]
    assert p.status == AgentStatus.DONE
    assert p.done is True
    assert p.input_tokens == 30
    assert p.output_tokens == 13
    assert p.cost_usd == pytest.approx(0.003)
    # Elapsed time is anchored to the reported final duration (±wall-clock
    # jitter between start() and finalize()).
    assert p.elapsed_ms(p.last_update) == pytest.approx(420, abs=25)


def test_tracker_finalize_with_explicit_metrics() -> None:
    t = SubagentProgressTracker()
    t.register("task")
    t.finalize(0, duration_ms=100, input_tokens=7, output_tokens=3, cost_usd=0.0005)
    p = t.tasks[0]
    assert p.input_tokens == 7
    assert p.output_tokens == 3
    assert p.cost_usd == pytest.approx(0.0005)


def test_tracker_mark_error() -> None:
    t = SubagentProgressTracker()
    t.register("task")
    t.mark_error(0, "timeout")
    assert t.tasks[0].status == AgentStatus.ERROR


def test_progress_elapsed_ms_bounds() -> None:
    p = SubagentProgress(index=0, task="t", started_at=100.0, last_update=103.0)
    assert p.elapsed_ms(105.0) == 5000
    assert p.elapsed_ms(90.0) == 0  # now < started_at → 0 (clock anomaly guard)
    p2 = SubagentProgress(index=0, task="t")  # no started_at (queued)
    assert p2.elapsed_ms(123.0) == 0


# ── agents tool feeds its tracker live + notifies the UI ────────────────────


@pytest.mark.asyncio
async def test_agents_tool_feeds_tracker_with_intermediate_metrics() -> None:
    notified: list = []
    tracker: SubagentProgressTracker | None = None

    def spy_notify(t):
        nonlocal tracker
        if t is not None:
            tracker = t
        notified.append(t)

    result = await agents.handler(
        {
            "tasks": [
                "read PROJECT.md and summarize",
                "read README.md and summarize",
            ],
        },
        {
            "chat": TwoTurnChat(),
            "available_tools": {"read_file": EchoTool()},
            "default_model": "fake-demo-model",
            "max_iterations": 4,
            "safe_mode": False,
            "subagent_timeout_seconds": 300.0,
            "on_subagent_progress": spy_notify,
        },
    )

    assert "=== Agent 0:" in result
    assert "=== Agent 1:" in result
    assert "Error:" not in result

    # The UI was notified with the call's tracker, then cleared.
    assert len(notified) == 2
    assert notified[0] is tracker
    assert notified[1] is None
    assert tracker is not None

    # Both tasks registered and finalized.
    assert len(tracker.tasks) == 2
    assert all(p.status == AgentStatus.DONE for p in tracker.tasks)
    assert all(p.done for p in tracker.tasks)

    # Final values snap to the full run totals (two LLM calls each).
    assert tracker.tasks[0].input_tokens == 30
    assert tracker.tasks[0].output_tokens == 13
    assert tracker.tasks[0].cost_usd == pytest.approx(0.003)


@pytest.mark.asyncio
async def test_agents_tool_live_update_visible_while_running() -> None:
    """The core of E2: tokens/cost show up *before* the task finishes."""
    notified: list = []
    tracker: SubagentProgressTracker | None = None
    captured: list[tuple[int, int, float]] = []

    def spy_notify(t):
        nonlocal tracker
        if t is not None:
            tracker = t
            original_update = t.update_from_step

            def spy_update(index, step):
                original_update(index, step)
                if index == 0 and not captured:
                    p = tracker.tasks[0]
                    captured.append((p.input_tokens, p.output_tokens, p.cost_usd))

            t.update_from_step = spy_update  # type: ignore[method-assign]
        notified.append(t)

    result = await agents.handler(
        {"tasks": ["read PROJECT.md and summarize"]},
        {
            "chat": TwoTurnChat(),
            "available_tools": {"read_file": EchoTool()},
            "default_model": "fake",
            "max_iterations": 4,
            "safe_mode": False,
            "subagent_timeout_seconds": 300.0,
            "on_subagent_progress": spy_notify,
        },
    )
    assert "=== Agent 0:" in result
    assert "Error:" not in result
    assert len(captured) == 1
    # After the first LLM step (mid-run), the tracker already had tokens.
    tin, tout, cost = captured[0]
    assert tin == 10
    assert tout == 5
    assert cost == pytest.approx(0.001)
    # ... and the final values are the full totals.
    assert tracker.tasks[0].input_tokens == 30
    assert tracker.tasks[0].cost_usd == pytest.approx(0.003)


@pytest.mark.asyncio
async def test_agents_tool_marks_tracker_error_on_timeout() -> None:
    class AlwaysSlowChat(BaseLLMChat):
        async def stream(self, messages, config, tools=None):
            await asyncio.sleep(10)
            yield LLMDoneEvent(content="late", has_tool_calls=False)

    notified: list = []
    tracker: SubagentProgressTracker | None = None

    def spy_notify(t):
        nonlocal tracker
        if t is not None:
            tracker = t
        notified.append(t)

    result = await agents.handler(
        {"tasks": ["slow task"]},
        {
            "chat": AlwaysSlowChat(),
            "available_tools": {"read_file": EchoTool()},
            "default_model": "fake",
            "max_iterations": 1,
            "safe_mode": False,
            "subagent_timeout_seconds": 0.05,
            "on_subagent_progress": spy_notify,
        },
    )
    assert "timeout" in result
    assert tracker is not None
    assert len(tracker.tasks) == 1
    assert tracker.tasks[0].status == AgentStatus.ERROR
    assert notified[-1] is None


@pytest.mark.asyncio
async def test_agents_tool_works_without_callback() -> None:
    """Pre-E2 callers: no callback in the context → everything still works."""
    result = await agents.handler(
        {"tasks": ["one task"]},
        {
            "chat": TwoTurnChat(),
            "available_tools": {"read_file": EchoTool()},
            "default_model": "fake",
            "max_iterations": 4,
            "safe_mode": False,
        },
    )
    assert "=== Agent 0:" in result
    assert "Error:" not in result


@pytest.mark.asyncio
async def test_agent_tool_single_feeds_tracker() -> None:
    notified: list = []
    tracker: SubagentProgressTracker | None = None

    def spy_notify(t):
        nonlocal tracker
        if t is not None:
            tracker = t
        notified.append(t)

    result = await agent.handler(
        {"task": "read PROJECT.md"},
        {
            "chat": TwoTurnChat(),
            "available_tools": {"read_file": EchoTool()},
            "default_model": "fake",
            "max_iterations": 4,
            "safe_mode": False,
            "subagent_timeout_seconds": 300.0,
            "on_subagent_progress": spy_notify,
        },
    )
    assert "Error" not in result
    assert tracker is not None
    assert len(tracker.tasks) == 1
    assert tracker.tasks[0].status == AgentStatus.DONE
    assert tracker.tasks[0].input_tokens == 30
    assert tracker.tasks[0].output_tokens == 13
    assert notified[-1] is None


@pytest.mark.asyncio
async def test_two_sequential_calls_get_independent_trackers() -> None:
    """A run may call the sub-agent tools several times: each call's
    panel must show only its own tasks (no index drift across calls)."""
    notified: list = []
    trackers: list = []

    def spy_notify(t):
        if t is not None:
            trackers.append(t)
        notified.append(t)

    ctx = {
        "chat": TwoTurnChat(),
        "available_tools": {"read_file": EchoTool()},
        "default_model": "fake",
        "max_iterations": 4,
        "safe_mode": False,
        "subagent_timeout_seconds": 300.0,
        "on_subagent_progress": spy_notify,
    }
    r1 = await agents.handler({"tasks": ["task one"]}, ctx)
    r2 = await agents.handler({"tasks": ["task two", "task three"]}, ctx)
    assert "task one" in r1
    assert "task two" in r2
    assert len(trackers) == 2
    # Independent trackers, each starting at index 0.
    assert trackers[0] is not trackers[1]
    assert trackers[0].tasks[0].index == 0
    assert trackers[1].tasks[0].index == 0
    assert trackers[1].tasks[1].index == 1
    # Cleared after each call.
    assert notified.count(None) == 2


# ── panel rendering with live progress ──────────────────────────────────────


def _cell(table, col: int, row: int) -> str:
    """Rendered value of table cell (column ``col``, row ``row``)."""
    return str(table.columns[col]._cells[row])


def test_running_table_without_progress_shows_waiting() -> None:
    table = render_subagent_panel_frame(["Task A", "Task B"], frame_index=0)
    assert _cell(table, 3, 0) == "waiting"
    assert _cell(table, 4, 0) == "—"
    assert _cell(table, 5, 0) == "—"


def test_running_table_with_progress_shows_live_values() -> None:
    tracker = SubagentProgressTracker()
    tracker.register("Task A")
    tracker.register("Task B")
    tracker.start(0)
    tracker.start(1)
    # Only task 0 has reported usage so far.
    tracker.update_from_step(0, _llm_step(cost=0.0015, tin=42, tout=17))

    table = render_subagent_panel_frame(
        ["Task A", "Task B"], frame_index=0, progress=tracker
    )

    # Task 0 row: live time/tokens/cost, not "waiting"/"—".
    assert "42in / 17out" in _cell(table, 4, 0)
    assert "$0.00150" in _cell(table, 5, 0)
    assert _cell(table, 3, 0) != "waiting"

    # Task 1 row: running but no usage yet → dashes, live Time.
    assert _cell(table, 4, 1) == "—"
    assert _cell(table, 5, 1) == "—"
    assert _cell(table, 3, 1) != "waiting"


def test_running_table_queued_task_stays_waiting() -> None:
    tracker = SubagentProgressTracker()
    tracker.register("Task A")
    tracker.register("Task B")
    tracker.start(0)  # Task B is still queued on the semaphore
    tracker.update_from_step(0, _llm_step(cost=0.001, tin=1, tout=1))

    table = render_subagent_panel_frame(
        ["Task A", "Task B"], frame_index=0, progress=tracker
    )
    assert _cell(table, 3, 0) != "waiting"
    assert _cell(table, 3, 1) == "waiting"  # queued row
    assert _cell(table, 4, 1) == "—"


def test_running_table_with_progress_marks_done_and_error() -> None:
    tracker = SubagentProgressTracker()
    tracker.register("Task A")
    tracker.register("Task B")
    tracker.start(0)
    tracker.start(1)
    tracker.update_from_step(0, _llm_step(cost=0.001, tin=1, tout=1))
    tracker.finalize(
        0, duration_ms=100, input_tokens=1, output_tokens=1, cost_usd=0.001
    )
    tracker.mark_error(1, "boom")

    table = render_subagent_panel_frame(
        ["Task A", "Task B"], frame_index=3, progress=tracker
    )
    assert "✓" in _cell(table, 1, 0)
    assert "✗" in _cell(table, 1, 1)


def test_panel_accepts_plain_list_progress() -> None:
    p = SubagentProgress(index=0, task="Task A", started_at=100.0, last_update=101.0)
    p.input_tokens = 5
    p.output_tokens = 2
    p.cost_usd = 0.0007
    table = render_subagent_panel(["Task A"], progress=[p])
    assert "5in / 2out" in _cell(table, 4, 0)


def test_panel_progress_accepts_tracker_or_list_equivalently() -> None:
    tracker = SubagentProgressTracker()
    tracker.register("Task A")
    tracker.start(0)
    tracker.update_from_step(0, _llm_step(cost=0.0007, tin=5, tout=2))
    from_list = render_subagent_panel(["Task A"], progress=tracker.tasks)
    from_tracker = render_subagent_panel(["Task A"], progress=tracker)
    assert _cell(from_list, 4, 0) == _cell(from_tracker, 4, 0)


# ── controller wiring ────────────────────────────────────────────────────────


class _E2Sink:
    """Recording sink for the controller tests."""

    def __init__(self) -> None:
        self.progress_events: list = []
        self.notices: list[tuple[str, str]] = []

    def on_user_message(self, text, message) -> None: ...
    def on_attachments(self, sources) -> None: ...
    def on_event(self, event) -> None: ...
    def flush_line(self) -> None: ...
    def capture_partial_reasoning(self) -> None: ...

    def take_reasoning(self) -> str:
        return ""

    def set_session(self, session_id: str) -> None: ...
    def print_history(self, path, tail=None) -> None: ...

    def notify(self, kind, message) -> None:
        self.notices.append((kind, message))

    def on_subagent_progress(self, progress) -> None:
        self.progress_events.append(progress)


def _e2_controller(tmp_path, stream_factory) -> tuple[SessionController, _E2Sink]:
    sink = _E2Sink()
    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        controller = SessionController(config, sink)
    controller.engine.stream = stream_factory(controller)
    controller._save_session = AsyncMock()
    controller._refresh_context_window = AsyncMock()
    return controller, sink


@pytest.mark.asyncio
async def test_controller_injects_progress_callback(tmp_path) -> None:
    seen_in_context: list = []

    def _stream(controller):
        async def stream(path, config):
            seen_in_context.append(
                controller.engine.context.extra.get("on_subagent_progress")
            )
            yield AgentDoneEvent(
                result=AgentRunResult(
                    final_content="ok",
                    history=list(path) + [Message(role="assistant", content="ok")],
                    input_messages=list(path),
                    steps=[],
                )
            )

        return stream

    controller, sink = _e2_controller(tmp_path, _stream)
    outcome = await controller.run_turn("hello")

    assert outcome.status == "done"
    # The run saw the sink's callback bound into the engine context — the
    # tools call it (with their per-call tracker) to drive the panel.
    assert len(seen_in_context) == 1
    callback = seen_in_context[0]
    assert callback is not None
    assert callback.__self__ is sink
    assert callback.__func__.__name__ == "on_subagent_progress"


# ── fullscreen sink: panel uses the tracker ─────────────────────────────────


def test_fullscreen_sink_panel_renders_live_metrics() -> None:
    ticks: list[int] = []
    sink = FullScreenSink(on_invalidate=lambda: ticks.append(1), theme=DARK)
    sink.begin_activity()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))

    tracker = SubagentProgressTracker()
    tracker.register("Task A")
    tracker.register("Task B")
    tracker.start(0)
    tracker.start(1)
    sink.on_subagent_progress(tracker)

    # Sub-agent tool starts → the turn picks up the tasks.
    sink.on_event(
        AgentToolStartEvent(tool_name="agents", args={"tasks": ["Task A", "Task B"]})
    )

    assert sink.current_turn is not None
    assert sink.current_turn.subagent_tasks == ["Task A", "Task B"]
    assert sink.current_turn.subagent_progress is tracker

    # Feed live metrics to task 0, then render.
    tracker.update_from_step(0, _llm_step(cost=0.0025, tin=64, tout=32))

    panel = sink.render_subagent_panel()
    assert panel is not None
    assert "64in / 32out" in _cell(panel, 4, 0)
    assert "$0.00250" in _cell(panel, 5, 0)
    assert _cell(panel, 3, 0) != "waiting"

    # Clearing the progress restores the static fallback.
    sink.on_subagent_progress(None)
    assert sink.current_turn.subagent_progress is None
    panel2 = sink.render_subagent_panel()
    assert panel2 is not None
    assert _cell(panel2, 3, 0) == "waiting"


def test_fullscreen_sink_progress_before_tool_start_kept() -> None:
    """The tool notifies the tracker (from inside the run) before the
    AgentToolStartEvent that sets subagent_tasks — the tracker must not
    be lost when the turn is already live."""
    ticks: list[int] = []
    sink = FullScreenSink(on_invalidate=lambda: ticks.append(1), theme=DARK)
    sink.begin_activity()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))

    tracker = SubagentProgressTracker()
    sink.on_subagent_progress(tracker)  # current_turn already exists
    assert sink.current_turn.subagent_progress is tracker

    sink.on_event(AgentToolStartEvent(tool_name="agents", args={"tasks": ["Task A"]}))
    assert sink.current_turn.subagent_progress is tracker
    assert sink.current_turn.subagent_tasks == ["Task A"]
