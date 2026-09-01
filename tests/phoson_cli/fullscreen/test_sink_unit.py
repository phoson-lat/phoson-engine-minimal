"""Unit tests for FullScreenSink (AgentEventSink over an in-memory transcript).

Feeds synthetic AgentEvent sequences and asserts on the resulting
``blocks``/``current_turn`` state and the rendered ANSI text, without a
running prompt_toolkit Application.
"""

import re
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

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x1b]*\x1b\\|[\x01\x02]")


def _strip_ansi(text: str) -> str:
    """ANSI/SGR/OSC 8 stripped plain text (for counting fragments)."""
    return _ANSI_RE.sub("", text)


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


def test_activity_indicator_is_visible_before_the_first_agent_event() -> None:
    """A4: Enter feedback starts immediately, not only after provider I/O."""
    sink, _ = _make_sink()

    sink.begin_activity()

    assert sink.current_turn is not None
    assert sink.activity_text() == "Thinking 0s"
    first_frame = sink.activity_frame()
    assert sink.tick_activity_frame() is True
    assert sink.activity_frame() != first_frame
    assert "Thinking" in render_chat(sink, width=80)


def test_activity_indicator_describes_the_live_turn_phase() -> None:
    sink, _ = _make_sink()
    sink.begin_activity()
    assert sink.activity_text() == "Thinking 0s"

    sink.current_turn.content = "hello"
    assert sink.activity_text() == "Streaming…"

    sink.current_turn.running_tool = True
    assert sink.activity_text() == "Running tool…"

    sink.current_turn.subagent_tasks = ["inspect tests"]
    assert sink.activity_text() == "Running subagents…"


def test_thinking_phase_counts_elapsed_seconds() -> None:
    """T-5: the *thinking* label shows wall-clock elapsed seconds (truncated)
    instead of rotating stock phrases, so a long wait reads as real time."""
    sink, _ = _make_sink()
    sink.begin_activity()
    # A fresh episode starts at 0 seconds.
    assert sink.activity_text() == "Thinking 0s"
    assert sink.current_turn.thinking_since is not None

    # Backdate the episode origin by 2.4 s → the counter reads 2 s
    # (whole seconds, truncated).
    sink.current_turn.thinking_since -= 2.4
    assert sink.activity_text() == "Thinking 2s"

    # It keeps counting from the same origin, not resetting on each read.
    sink.current_turn.thinking_since -= 1.0  # now ~3.4 s elapsed
    assert sink.activity_text() == "Thinking 3s"

    # A fresh episode (thinking_since reset to None, as AgentToolStartEvent
    # does) re-arms the counter from 0.
    sink.current_turn.thinking_since = None
    assert sink.activity_text() == "Thinking 0s"


def test_thinking_timer_is_inert_outside_the_thinking_phase() -> None:
    """Once the turn is streaming / running a tool, the phase label is fixed
    and the thinking counter is no longer consulted."""
    sink, _ = _make_sink()
    sink.begin_activity()
    assert sink.activity_text() == "Thinking 0s"  # arms the timer

    # Streaming takes over: the fixed label survives any number of ticks.
    sink.current_turn.content = "hello"
    for _ in range(10):
        sink.tick_activity_frame()
    assert sink.activity_text() == "Streaming…"


def test_thinking_timer_re_arms_for_each_thinking_episode() -> None:
    """T-5: the "Thinking {n}s" counter measures the *current* wait, not the
    whole run. A tool call ends the episode; the next one (model generating
    after the result) starts from 0 again."""
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=10))
    assert sink.activity_text() == "Thinking 0s"  # arms the episode timer
    sink.current_turn.thinking_since -= 3.0  # ~3 s of pre-tool thinking
    assert sink.activity_text() == "Thinking 3s"

    # The tool start ends the episode (also freezes any streamed text).
    sink.on_event(AgentToolStartEvent(tool_name="bash", args={"cmd": "pytest"}))
    assert sink.current_turn.thinking_since is None
    assert sink.activity_text() == "Running tool…"

    # Tool done → the model thinks again: fresh episode, counting from 0.
    sink.on_event(AgentToolDoneEvent(tool_name="bash", result="ok", duration_ms=5))
    assert sink.activity_text() == "Thinking 0s"
    assert sink.current_turn.thinking_since is not None


def test_hidden_reasoning_does_not_duplicate_the_activity_spinner() -> None:
    """Regression: while thinking is hidden, the transient activity line is
    the only in-chat feedback — do not add the streaming panel's separate
    ``Phoson / thinking...`` placeholder below it.
    """
    sink, _ = _make_sink()
    sink.begin_activity()
    assert sink.current_turn is not None
    sink.current_turn.reasoning = "internal reasoning"
    sink.current_turn.show_reasoning = False

    text = render_chat(sink, width=80)

    assert "Thinking" in text
    assert "Phoson" not in text
    assert "thinking..." not in text


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
    # The in-chat spinner is transient, never part of finished scrollback.
    assert "Thinking" not in text
    assert "Streaming…" not in text


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
            "listing directory",
            "Now running the tests.",
            "running command",
            "All green, done.",
        )
    }
    ordered = sorted(positions, key=positions.get)
    assert ordered == [
        "Let me check the directory first.",
        "listing directory",
        "Now running the tests.",
        "running command",
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


def test_tool_start_and_done_replace_the_live_card_header() -> None:
    """A completed tool must replace its live start line, never duplicate it."""
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(
        AgentToolStartEvent(
            tool_name="read_file", args={"path": "x.txt"}, tool_call_id="read-1"
        )
    )
    assert sink.current_turn.running_tool is True
    assert len(sink.blocks) == 1

    sink.on_event(
        AgentToolDoneEvent(
            tool_name="read_file",
            result="contents",
            duration_ms=5,
            tool_call_id="read-1",
        )
    )
    assert sink.current_turn.running_tool is False
    assert len(sink.blocks) == 1

    text = render_chat(sink, width=80)
    assert text.count("reading file") == 1
    assert "x.txt" in text


def test_parallel_tool_cards_replace_their_own_start_lines() -> None:
    """Parallel calls are replaced by tool_call_id, not tool name/order."""
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(
        AgentToolStartEvent(
            tool_name="read_file", args={"path": "a.txt"}, tool_call_id="a"
        )
    )
    sink.on_event(
        AgentToolStartEvent(
            tool_name="read_file", args={"path": "b.txt"}, tool_call_id="b"
        )
    )
    # Complete in reverse order to prove identity-based replacement.
    sink.on_event(
        AgentToolDoneEvent(
            tool_name="read_file", result="B", duration_ms=2, tool_call_id="b"
        )
    )
    sink.on_event(
        AgentToolDoneEvent(
            tool_name="read_file", result="A", duration_ms=1, tool_call_id="a"
        )
    )

    assert len(sink.blocks) == 2
    text = render_chat(sink, width=80)
    plain = _strip_ansi(text)
    assert plain.count("reading file") == 2
    # Count the header fragment, not the bare path: the path also appears
    # inside its OSC 8 file:// link URI (T-7).
    assert plain.count("·  a.txt") == 1
    assert plain.count("·  b.txt") == 1


def test_step_done_advances_counters() -> None:
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentStepDoneEvent(step=_run_step(0.01)))
    sink.on_event(AgentStepDoneEvent(step=_run_step(0.02)))

    assert sink.current_turn.current_step == 2
    assert round(sink.current_turn.run_cost_usd, 3) == 0.03


def test_step_done_invalidates_for_live_header() -> None:
    """I-88: a completed step must repaint the UI so the header's
    cost/token indicators (which the controller updates live) are shown
    without waiting for the turn to finish."""
    sink, ticks = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    ticks.clear()

    sink.on_event(AgentStepDoneEvent(step=_run_step(0.01)))

    assert ticks, "AgentStepDoneEvent must invalidate (live header repaint)"


def test_error_event_finalizes_turn_and_shows_notice() -> None:
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentReasoningEvent(content="thinking"))
    # code=None: no hint, so the (sanitized) message is what the notice shows.
    sink.on_event(AgentErrorEvent(message="boom", code=None))

    assert sink.current_turn is None
    assert sink.take_reasoning() == "thinking"
    text = render_chat(sink, width=80)
    assert "boom" in text
    # I-83: single-line notice, not a panel.
    assert "⚠" in text
    assert sink._error_notice_idx is not None


def test_error_notice_is_overwritten_on_each_failed_retry() -> None:
    """Three failed attempts → exactly ONE notice block (I-83)."""
    sink, _ = _make_sink()

    for message in ("first failure", "second failure", "third failure"):
        sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
        sink.on_event(AgentErrorEvent(message=message, code=None, retryable=True))

    assert sink._error_notice_idx is not None
    notices = [b for b in sink.blocks if "failure" in str(b)]
    assert len(notices) == 1
    # The surviving notice shows the LATEST error, not the first.
    text = render_chat(sink, width=80)
    assert "third failure" in text
    assert "first failure" not in text
    assert "second failure" not in text


def test_error_notice_dropped_when_retry_succeeds() -> None:
    """The notice stays while the retry runs and disappears on success (I-83)."""
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentErrorEvent(message="boom", code=None, retryable=True))
    assert sink._error_notice_idx is not None
    assert any("boom" in str(b) for b in sink.blocks)

    # The retry starts: the notice is still there (the attempt is in flight).
    sink.on_event(AgentStartEvent(model="m", message_count=2, max_iterations=5))
    assert any("boom" in str(b) for b in sink.blocks)

    # The retry succeeds: the notice disappears, transcript is clean.
    sink.on_event(AgentTokenEvent(content="hello"))
    sink.on_event(
        AgentDoneEvent(
            result=AgentRunResult(final_content="hello", history=[], input_messages=[])
        )
    )
    assert sink._error_notice_idx is None
    assert not any("boom" in str(b) for b in sink.blocks)


def test_error_notice_index_survives_transcript_reset() -> None:
    """A stale index (blocks cleared without telling the sink) self-heals (I-83)."""
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentErrorEvent(message="boom", code=None, retryable=True))
    assert sink._error_notice_idx is not None

    # Simulate clear()/rewind re-draw: blocks dropped, index untouched.
    sink.blocks.clear()

    # Next error must append (not crash on the stale index).
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentErrorEvent(message="again", code=None, retryable=True))
    assert sink._error_notice_idx == 0
    text = render_chat(sink, width=80)
    assert "again" in text
    assert "boom" not in text


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
    assert "/ commands" in render_chat(sink, width=80)


# ── I-84: repaint throttling ─────────────────────────────────────────────────


def test_token_events_do_not_invalidate_per_token() -> None:
    """I-84: the unconditional per-event _touch() used to defeat the
    touch_streaming() throttle — every token invalidated. A burst of tokens
    inside one REPAINT_INTERVAL window must produce exactly one immediate
    invalidation (the rest is coalesced into the trailing repaint timer).
    Runs inside a live loop: without one the throttle degrades to
    repaint-now by design (sync callers can't schedule timers)."""
    import asyncio

    sink, ticks = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=4))

    async def burst() -> None:
        ticks.clear()
        for _ in range(25):
            sink.on_event(AgentTokenEvent(content="x "))

    asyncio.run(burst())
    assert len(ticks) == 1  # one immediate repaint, not one per token


def test_streaming_trailing_repaint_never_lost() -> None:
    """I-84: the throttled repaint schedules a trailing timer; when it fires,
    the last streamed chunk is still painted (never left unrendered)."""
    import asyncio

    from phoson_cli.fullscreen.sink import REPAINT_INTERVAL_SECONDS

    sink, ticks = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=4))

    async def drive() -> None:
        ticks.clear()
        sink.on_event(AgentTokenEvent(content="first "))
        sink.on_event(AgentTokenEvent(content="last chunk"))
        # Wait for the trailing timer to fire.
        await asyncio.sleep(REPAINT_INTERVAL_SECONDS + 0.05)
        sink.on_event(AgentDoneEvent(result=_done_result()))

    asyncio.run(drive())
    # 1 immediate (first token) + 1 trailing repaint, then the done line.
    assert len(ticks) >= 2
    # The final turn must have frozen the streamed content into a block —
    # assert via the rendered ANSI (blocks are Rich renderables, not text).
    assert "last chunk" in render_chat(sink, width=80)


def _done_result() -> "AgentRunResult":
    return AgentRunResult(
        final_content="first  last chunk",
        history=[],
        input_messages=[],
    )


def test_tick_activity_frame_frozen_while_streaming() -> None:
    """I-84/I-128: streaming and subagents keep the glyph frozen (the text /
    panel already move); thinking, composing, and tool-running animate it."""
    sink, _ = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=4))

    first_frame = sink.activity_frame()
    # Pure thinking: ticks animate.
    assert sink.tick_activity_frame() is True
    assert sink.activity_frame() != first_frame

    # Streaming: ticks are inert (the growing text is the feedback).
    sink.current_turn.content = "streamed"
    frame = sink.activity_frame()
    for _ in range(5):
        assert sink.tick_activity_frame() is False
    assert sink.activity_frame() == frame

    # Composing (with text already present) still animates: the verb line
    # is static, so the glyph keeps it from looking frozen (I-128).
    sink.current_turn.composing_tool = "write_file"
    assert sink.tick_activity_frame() is True

    # Tool-running: the start card is static and the text is frozen, so
    # without the glyph nothing would move during a long bash/build.
    sink.current_turn.content = ""
    sink.current_turn.composing_tool = ""
    sink.current_turn.running_tool = True
    frame = sink.activity_frame()
    assert sink.tick_activity_frame() is True
    assert sink.activity_frame() != frame

    # Subagents: the panel animates itself, so the glyph stays frozen.
    sink.current_turn.running_tool = False
    sink.current_turn.subagent_tasks = ["inspect tests"]
    frame = sink.activity_frame()
    assert sink.tick_activity_frame() is False
    assert sink.activity_frame() == frame

    # Idle sink: no turn, no repaint.
    sink.current_turn = None
    assert sink.tick_activity_frame() is False


def test_thinking_counter_runs_on_the_clock_not_on_ticks() -> None:
    """T-5: the "Thinking {n}s" seconds come from the monotonic clock, not
    from the spinner tick cadence. Ticks keep the braille glyph animating;
    the counter must not jump (or stall) with them."""
    sink, _ = _make_sink()
    sink.begin_activity()
    turn = sink.current_turn
    assert turn is not None
    assert sink.activity_text() == "Thinking 0s"  # arms the episode timer
    origin = turn.thinking_since
    assert origin is not None

    # Many fast ticks (would have rotated phrases at 21-tick intervals):
    # the glyph advances, the label holds at 0 s — these loops run far
    # shorter than one second of wall time.
    frame = sink.activity_frame()
    for _ in range(5):
        assert sink.tick_activity_frame() is True
    assert sink.activity_frame() != frame
    assert sink.activity_text() == "Thinking 0s"

    # 1.3 s of elapsed time (backdated, deterministic): the label reads
    # 1 s even though no ticks happened in between.
    turn.thinking_since = origin - 1.3
    assert sink.activity_text() == "Thinking 1s"


def test_repaint_intervals_match_target_fps() -> None:
    """I-84: the tuning constants must stay at their intended cadences
    (~10 fps stream repaint, ~8.3 fps activity ticks for a smooth
    braille spinner)."""
    from phoson_cli.fullscreen.app import _SUBAGENT_TICK_SECONDS
    from phoson_cli.fullscreen.sink import REPAINT_INTERVAL_SECONDS

    assert 0.09 <= REPAINT_INTERVAL_SECONDS <= 0.11  # ~10 fps
    assert 0.10 <= _SUBAGENT_TICK_SECONDS <= 0.14  # ~8.3 fps


def test_step_done_invalidation_is_throttled_like_streaming() -> None:
    """I-84: AgentStepDoneEvent (I-88 live header metrics) shares the
    streaming throttle — inside a live loop, a burst of step-done events
    in one window coalesces into a single immediate repaint + trailing
    timer. Without a loop it degrades to repaint-now by design."""
    import asyncio

    sink, ticks = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=4))

    async def burst() -> None:
        ticks.clear()
        for _ in range(5):
            sink.on_event(AgentStepDoneEvent(step=_run_step()))

    asyncio.run(burst())
    assert len(ticks) == 1


def test_error_notice_still_immediate_with_stream_event_flag() -> None:
    """I-84: an error arriving after streamed tokens must invalidate
    immediately (the notice cannot wait for the trailing timer) and must
    not leave the _stream_event flag latched."""
    sink, ticks = _make_sink()
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=4))
    # Prime the throttle so the next token would NOT invalidate immediately.
    sink.on_event(AgentTokenEvent(content="partial "))
    ticks.clear()

    sink.on_event(AgentErrorEvent(message="boom", code="server_error"))

    assert len(ticks) == 1  # immediate, not throttled
    assert sink._stream_event is False  # flag not latched


# ─── T-7: collapsed tool cards + /details toggle ─────────────────────────────


def _transcript_text(sink: FullScreenSink) -> str:
    return render_chat(sink, 120)


def _finish_patch_call(sink: FullScreenSink) -> None:
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=4))
    sink.on_event(
        AgentToolStartEvent(
            tool_name="patch_file",
            tool_call_id="c1",
            args={"path": "f.py", "old_content": "a\nb\n", "new_content": "a\nc\n"},
        )
    )
    sink.on_event(
        AgentToolDoneEvent(
            tool_name="patch_file", result="ok", duration_ms=5, tool_call_id="c1"
        )
    )


def test_tool_card_expanded_by_default_with_details_marker() -> None:
    sink, _ = _make_sink()
    _finish_patch_call(sink)
    text = _transcript_text(sink)
    assert "-b" in text  # diff body visible
    assert "/details" in text  # the card offers the toggle


def test_tool_details_toggle_collapse_then_expand() -> None:
    sink, ticks = _make_sink()
    _finish_patch_call(sink)

    ticks.clear()
    assert sink.set_tool_details() is False  # first toggle collapses
    collapsed = _transcript_text(sink)
    assert "-b" not in collapsed  # body hidden
    assert "+c" not in collapsed
    assert len(ticks) == 1  # the re-render invalidates

    assert sink.set_tool_details() is True  # and back
    expanded = _transcript_text(sink)
    assert "-b" in expanded
    assert "+c" in expanded
