"""Unit tests for FullScreenSink (AgentEventSink over an in-memory transcript).

Feeds synthetic AgentEvent sequences and asserts on the resulting
``blocks``/``current_turn`` state and the rendered ANSI text, without a
running prompt_toolkit Application.
"""

import datetime

from phoson_cli.theme import DARK
from phoson_llm.schemas import Message
from phoson_agent.models import (
    RunStep,
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)
from phoson_cli.fullscreen.sink import FullScreenSink
from phoson_cli.fullscreen.render import render_chat

UTC = datetime.UTC


def _make_sink() -> tuple[FullScreenSink, list[int]]:
    ticks: list[int] = []
    sink = FullScreenSink(on_invalidate=lambda: ticks.append(1), theme=DARK)
    return sink, ticks


def _run_step(cost: float = 0.001) -> RunStep:
    now = datetime.datetime.now(UTC)
    return RunStep(
        kind="llm",
        started_at=now,
        ended_at=now,
        duration_ms=10,
        cost_usd=cost,
        credits=1.0,
    )


def test_on_user_message_appends_block_and_invalidates() -> None:
    sink, ticks = _make_sink()
    sink.on_user_message("hi", Message(role="user", content="hi"))
    assert len(sink.blocks) == 1
    assert ticks


def test_start_token_done_builds_streaming_panel_then_finalizes() -> None:
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="gpt-4o", message_count=1, max_iterations=10))
    assert sink.current_turn is not None
    assert sink.current_turn.model == "gpt-4o"

    sink.on_event(AgentTokenEvent(content="Hello "))
    sink.on_event(AgentTokenEvent(content="world"))
    assert sink.current_turn.content == "Hello world"
    assert "Streaming" == sink.status_text()

    result = AgentRunResult(
        final_content="Hello world", history=[], input_messages=[], steps=[_run_step()]
    )
    sink.on_event(AgentDoneEvent(result=result))

    assert sink.current_turn is None
    text = render_chat(sink, width=80)
    assert "Hello world" in text
    assert "1 step" in text


def test_text_interleaves_with_tool_calls_in_chronological_order() -> None:
    """Regression: a user reported all tool calls rendering before any

    answer text, with the text segments smashed into one paragraph —
    because tool cards land in ``blocks`` immediately while streamed
    text only accumulated on ``current_turn`` until the whole turn
    finished. Text must freeze into its own block right before each
    tool card so the transcript preserves the real chronological order:
    text, tool, text, tool, text.
    """
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=10))

    sink.on_event(AgentTokenEvent(content="Let me check the directory first."))
    sink.on_event(AgentToolStartEvent(tool_name="list_dir", args={"path": "."}))
    sink.on_event(AgentToolDoneEvent(tool_name="list_dir", result="ok", duration_ms=5))

    sink.on_event(AgentTokenEvent(content="Now running the tests."))
    sink.on_event(AgentToolStartEvent(tool_name="bash", args={"cmd": "pytest"}))
    sink.on_event(AgentToolDoneEvent(tool_name="bash", result="ok", duration_ms=10))

    sink.on_event(AgentTokenEvent(content="All green, done."))
    result = AgentRunResult(
        final_content="All green, done.", history=[], input_messages=[]
    )
    sink.on_event(AgentDoneEvent(result=result))

    text = render_chat(sink, width=100)
    positions = {
        marker: text.index(marker)
        for marker in (
            "Let me check the directory first.",
            "list_dir",
            "Now running the tests.",
            "bash",
            "All green, done.",
        )
    }
    ordered = sorted(positions, key=positions.get)
    assert ordered == [
        "Let me check the directory first.",
        "list_dir",
        "Now running the tests.",
        "bash",
        "All green, done.",
    ]
    # Each answer segment got its own frozen block, not one giant blob.
    assert text.count("Phoson") >= 3


def test_reasoning_is_captured_and_taken_once() -> None:
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentReasoningEvent(content="because "))
    sink.on_event(AgentReasoningEvent(content="X"))
    assert sink.current_turn.reasoning == "because X"

    result = AgentRunResult(final_content="", history=[], input_messages=[])
    sink.on_event(AgentDoneEvent(result=result))

    assert sink.take_reasoning() == "because X"
    assert sink.take_reasoning() == ""  # popped once


def test_flush_line_finalizes_turn_on_cancel() -> None:
    """Regression: cancelling mid-stream must not leave a "thinking..."

    panel stuck forever — flush_line() (called by SessionController right
    before capture_partial_reasoning on cancel) has to freeze whatever was
    streamed so far into a real block and clear current_turn, while still
    preserving the reasoning for the follow-up capture_partial_reasoning
    call to find (a no-op at that point, since it's already captured).
    """
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentTokenEvent(content="partial answer"))
    sink.on_event(AgentReasoningEvent(content="mid-thought"))

    sink.flush_line()
    sink.capture_partial_reasoning()  # controller calls this right after

    assert sink.current_turn is None
    assert sink.take_reasoning() == "mid-thought"
    text = render_chat(sink, width=80)
    assert "partial answer" in text
    assert "thinking..." not in text


def test_flush_line_is_a_noop_when_idle() -> None:
    sink, ticks = _make_sink()
    sink.flush_line()
    assert sink.current_turn is None
    assert ticks == []


def test_tool_start_and_done_append_lines() -> None:
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentToolStartEvent(tool_name="read_file", args={"path": "x.txt"}))
    assert sink.current_turn.running_tool is True

    sink.on_event(
        AgentToolDoneEvent(tool_name="read_file", result="contents", duration_ms=5)
    )
    assert sink.current_turn.running_tool is False

    text = render_chat(sink, width=80)
    assert "read_file" in text


def test_step_done_advances_counters() -> None:
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentStepDoneEvent(step=_run_step(0.01)))
    sink.on_event(AgentStepDoneEvent(step=_run_step(0.02)))

    assert sink.current_turn.current_step == 2
    assert round(sink.current_turn.run_cost_usd, 3) == 0.03


def test_error_event_finalizes_turn_and_shows_panel() -> None:
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentReasoningEvent(content="thinking"))
    sink.on_event(AgentErrorEvent(message="boom", code="auth"))

    assert sink.current_turn is None
    assert sink.take_reasoning() == "thinking"
    text = render_chat(sink, width=80)
    assert "boom" in text


def test_subagent_tasks_tracked_and_summary_replaces_panel() -> None:
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentToolStartEvent(tool_name="agents", args={"tasks": ["a", "b"]}))

    assert sink.current_turn.subagent_tasks == ["a", "b"]
    assert sink.render_subagent_panel() is not None
    assert sink.tick_subagent_frame() is True
    assert sink.current_turn.subagent_frame == 1

    sink.on_event(AgentToolDoneEvent(tool_name="agents", result="", duration_ms=100))
    assert sink.current_turn.subagent_tasks is None
    assert sink.render_subagent_panel() is None


def test_notify_and_attachments_append_blocks() -> None:
    sink, _ = _make_sink()
    sink.notify("warn", "careful now")
    sink.on_attachments(["/tmp/a.png"])

    text = render_chat(sink, width=80)
    assert "careful now" in text
    assert "/tmp/a.png" in text


def test_render_chat_placeholder_when_empty() -> None:
    sink, _ = _make_sink()
    assert "Type a message" in render_chat(sink, width=80)
