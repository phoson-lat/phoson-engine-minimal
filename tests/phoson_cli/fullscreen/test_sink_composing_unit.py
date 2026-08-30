"""Unit tests for the composing-tool UI state (I-128, full-screen sink).

The classic ``Renderer`` relabels its spinner on
``AgentToolComposingEvent``; the full-screen front end instead keeps a
``composing_tool`` label on the in-flight turn and renders it on the
in-chat activity line. These tests pin that contract:

- composing sets the label and shows ``⚙ {verb}…`` on the activity line;
- the label is cleared the moment the real ``AgentToolStartEvent`` card
  lands, so there is never a duplicate "composing" line + start card;
- the header status reads ``Composing tool`` during that window;
- a composing-only turn (no text yet) still animates the activity frame
  instead of sitting frozen;
- unknown tool names fall back to the de-underscored name via ``tool_verb``.
"""

from phoson_cli.theme import DARK
from phoson_agent.models import (
    AgentStartEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
    AgentToolComposingEvent,
)
from phoson_cli.fullscreen.sink import FullScreenSink
from phoson_cli.fullscreen.render import render_chat


def _make_sink() -> FullScreenSink:
    return FullScreenSink(on_invalidate=lambda: None, theme=DARK)


def _start_turn(sink: FullScreenSink) -> None:
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))


def test_composing_sets_label_on_activity_line() -> None:
    sink = _make_sink()
    _start_turn(sink)
    sink.on_event(
        AgentToolComposingEvent(index=0, tool_name="write_file", args_chunk='{"path":')
    )
    assert sink.current_turn.composing_tool == "write_file"
    assert sink.activity_text() == "⚙ writing file…"
    assert "⚙ writing file…" in render_chat(sink, width=80)


def test_composing_clears_when_tool_start_card_lands() -> None:
    """No duplicate: the composing label must be gone once the start card shows."""
    sink = _make_sink()
    _start_turn(sink)
    sink.on_event(
        AgentToolComposingEvent(index=0, tool_name="read_file", args_chunk='{"path":')
    )
    assert sink.current_turn.composing_tool == "read_file"

    sink.on_event(
        AgentToolStartEvent(
            index=0, tool_call_id="r1", tool_name="read_file", args={"path": "x.txt"}
        )
    )
    assert sink.current_turn.composing_tool == ""
    # The activity line must no longer claim "composing"; the start card is
    # now the feedback.
    assert sink.activity_text() != "⚙ reading file…"
    text = render_chat(sink, width=80)
    assert text.count("reading file") == 1  # only the start card, once


def test_composing_header_status_is_composing_tool() -> None:
    sink = _make_sink()
    _start_turn(sink)
    assert sink.status_text() == "thinking · step 0/5"
    sink.on_event(
        AgentToolComposingEvent(index=0, tool_name="bash", args_chunk='{"command":')
    )
    assert sink.status_text() == "Composing tool"
    # Start returns it to the running-tool status.
    sink.on_event(
        AgentToolStartEvent(
            index=0, tool_call_id="b1", tool_name="bash", args={"command": "ls"}
        )
    )
    assert sink.status_text() == "Running tool"


def test_composing_beats_streaming_when_text_already_started() -> None:
    """The model wrote some text, then began a tool call: the verb wins."""
    sink = _make_sink()
    _start_turn(sink)
    sink.current_turn.content = "Let me check that."
    assert sink.activity_text() == "Streaming…"
    sink.on_event(
        AgentToolComposingEvent(index=0, tool_name="bash", args_chunk='{"command":')
    )
    assert sink.activity_text() == "⚙ running command…"


def test_composing_unknown_tool_falls_back_to_underscored_name() -> None:
    sink = _make_sink()
    _start_turn(sink)
    sink.on_event(
        AgentToolComposingEvent(index=0, tool_name="my_custom_tool", args_chunk="{}")
    )
    assert sink.activity_text() == "⚙ my custom tool…"


def test_composing_animates_the_activity_frame() -> None:
    """A static "⚙ writing file…" over a long args generation can look
    frozen; the spinner glyph keeps ticking while composing."""
    sink = _make_sink()
    _start_turn(sink)
    sink.on_event(
        AgentToolComposingEvent(index=0, tool_name="write_file", args_chunk='{"p":')
    )
    first = sink.activity_frame()
    assert sink.tick_activity_frame() is True
    assert sink.activity_frame() != first  # advanced
    # Plain streaming still does NOT animate the glyph (text is the feedback).
    sink2 = _make_sink()
    _start_turn(sink2)
    sink2.current_turn.content = "streaming text"
    first2 = sink2.activity_frame()
    assert sink2.tick_activity_frame() is False
    assert sink2.activity_frame() == first2


def test_composing_event_with_empty_name_is_ignored() -> None:
    """Deltas can arrive before the name is known; the sink must not
    overwrite a real label with a blank one."""
    sink = _make_sink()
    _start_turn(sink)
    sink.on_event(
        AgentToolComposingEvent(index=0, tool_name="bash", args_chunk='{"command":')
    )
    assert sink.current_turn.composing_tool == "bash"
    # An args-only fragment (name not yet known) must not clobber it.
    sink.on_event(AgentToolComposingEvent(index=0, tool_name="", args_chunk=' ls"}'))
    assert sink.current_turn.composing_tool == "bash"


def test_composing_label_cleared_on_done_and_error_finalization() -> None:
    """Terminal events clear the whole turn, so no composing ghost survives."""
    from phoson_agent.models import AgentErrorEvent

    sink = _make_sink()
    _start_turn(sink)
    sink.on_event(
        AgentToolComposingEvent(index=0, tool_name="write_file", args_chunk='{"p":')
    )
    sink.on_event(AgentErrorEvent(message="boom", code="network", retryable=True))
    assert sink.current_turn is None

    sink2 = _make_sink()
    _start_turn(sink2)
    sink2.on_event(
        AgentToolComposingEvent(index=0, tool_name="write_file", args_chunk='{"p":')
    )
    sink2.on_event(
        AgentToolStartEvent(
            index=0, tool_call_id="w1", tool_name="write_file", args={"path": "f"}
        )
    )
    sink2.on_event(
        AgentToolDoneEvent(
            index=0,
            tool_call_id="w1",
            tool_name="write_file",
            result="ok",
            duration_ms=3,
        )
    )
    assert sink2.current_turn.composing_tool == ""


def test_composing_does_not_stack_a_block() -> None:
    """The composing feedback lives on the turn, not in the transcript, so a
    stream that dies mid-composing leaves no orphan line (I-83 owns the
    error notice)."""
    sink = _make_sink()
    _start_turn(sink)
    sink.on_event(
        AgentToolComposingEvent(index=0, tool_name="write_file", args_chunk='{"p":')
    )
    # No transcript block was created for the composing state.
    assert sink.blocks == []
