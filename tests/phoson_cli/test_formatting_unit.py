"""Unit tests for the pure Rich-renderable formatters in phoson_cli.formatting.

Every function here must be a pure ``data -> Rich renderable`` builder
(no console I/O) so both the classic Renderer and the full-screen sink
can reuse them. We render each result into a throwaway ``Console`` and
assert on the plain-text output.
"""

from rich.console import Console

from phoson_cli.theme import DARK
from phoson_agent.models import (
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_cli.formatting import (
    render_notice,
    render_history,
    render_done_line,
    render_user_turn,
    render_start_line,
    tool_args_preview,
    render_error_panel,
    render_tool_done_line,
    render_reasoning_panel,
    render_streaming_panel,
    render_tool_start_line,
    subagent_tasks_from_args,
    render_subagent_start_line,
)


def _render(renderable) -> str:
    console = Console(highlight=False, width=100)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_render_start_line_shows_model_and_session() -> None:
    event = AgentStartEvent(model="gpt-4o", message_count=3, max_iterations=10)
    output = _render(render_start_line(event, "abcd1234", DARK))
    assert "gpt-4o" in output
    assert "abcd1234" in output
    assert "3 msgs" in output


def test_render_streaming_panel_shows_answer_and_thinking() -> None:
    output = _render(render_streaming_panel("hello world", "pondering...", True, DARK))
    assert "hello world" in output
    assert "pondering" in output


def test_render_streaming_panel_hides_reasoning_when_disabled() -> None:
    output = _render(render_streaming_panel("hi", "secret thoughts", False, DARK))
    assert "hi" in output
    assert "secret thoughts" not in output


def test_render_streaming_panel_placeholder_when_empty() -> None:
    output = _render(render_streaming_panel("", "", True, DARK))
    assert "thinking" in output


def test_render_streaming_panel_answer_carries_an_explicit_color() -> None:
    """Regression: Markdown's default style ("none") emits no ANSI color

    at all for plain paragraph text — harmless printed straight to a
    real terminal, but inside the full-screen app that falls through to
    prompt_toolkit's own default foreground (a muted tone, not white).
    The answer must carry an explicit color so it always renders in the
    theme's text color regardless of the surrounding app style.
    """
    console = Console(
        force_terminal=True, color_system="truecolor", width=100, highlight=False
    )
    with console.capture() as cap:
        console.print(render_streaming_panel("hello world", "", False, DARK))
    ansi = cap.get()

    # Isolate the answer's own line (the "Phoson" label above it is styled
    # regardless, so checking the whole block would pass even without the
    # fix) and require *that* line to carry a color escape.
    answer_line = next(line for line in ansi.splitlines() if "hello world" in line)
    assert "\x1b[" in answer_line


def test_render_streaming_panel_links_emit_real_osc8_hyperlinks() -> None:
    """IMPROVEMENTS.md G4 (#58): Markdown links must be real, clickable

    OSC 8 hyperlinks (``\\x1b]8;...;URL\\x1b\\\\``) — a regression to inert
    ``text (url)`` links (the previous ``hyperlinks=False`` fix for a worse
    bug: raw OSC 8 bytes leaking as literal text when printed through
    prompt_toolkit's ``ANSI()``) is exactly what G4 undoes. The safety net
    against that older bug now lives in ``phoson_cli.hyperlinks
    .osc8_passthrough``, applied only on the full-screen render path (see
    ``fullscreen/render.py`` and ``test_hyperlinks_unit.py``) — printing
    straight to a real ``Console``, as this test does, is exactly the
    classic-REPL case and needs no such treatment.
    """
    console = Console(
        force_terminal=True, color_system="truecolor", width=100, highlight=False
    )
    with console.capture() as cap:
        console.print(
            render_streaming_panel(
                "Visit [our site](https://phoson.lat) for more.", "", False, DARK
            )
        )
    ansi = cap.get()

    assert "\x1b]8;" in ansi
    assert "phoson.lat" in ansi


def test_render_history_links_emit_real_osc8_hyperlinks() -> None:
    """Same G4 hyperlink check as render_streaming_panel, for session replay."""
    from phoson_llm.schemas import Message

    messages = [
        Message(
            role="assistant",
            content="Visit [our site](https://phoson.lat) for more.",
        )
    ]
    console = Console(
        force_terminal=True, color_system="truecolor", width=100, highlight=False
    )
    with console.capture() as cap:
        console.print(render_history(messages, DARK))
    ansi = cap.get()

    assert "\x1b]8;" in ansi
    assert "phoson.lat" in ansi
    assert "phoson.lat" in ansi


def test_render_tool_start_line_includes_label_and_args() -> None:
    event = AgentToolStartEvent(tool_name="read_file", args={"path": "/tmp/x.txt"})
    output = _render(render_tool_start_line(event, DARK))
    assert "reading file" in output
    assert "/tmp/x.txt" in output


def test_render_subagent_start_line_says_spawning() -> None:
    event = AgentToolStartEvent(tool_name="agent", args={"task": "do the thing"})
    output = _render(render_subagent_start_line(event, DARK))
    assert "spawning" in output
    assert "subagent" in output


def test_render_tool_done_line_success() -> None:
    event = AgentToolDoneEvent(tool_name="bash", result="ok", duration_ms=42)
    output = _render(render_tool_done_line(event, DARK, args={"command": "echo hi"}))
    assert "✓" in output
    assert "running command" in output
    assert "42ms" in output


def test_render_tool_done_line_error() -> None:
    event = AgentToolDoneEvent(
        tool_name="write_file",
        result="",
        error="Permission denied: /etc/hosts",
        duration_ms=5,
    )
    output = _render(render_tool_done_line(event, DARK))
    assert "✗" in output
    assert "Permission denied" in output


def test_render_done_line_shows_cost_and_steps() -> None:
    result = AgentRunResult(
        final_content="done",
        history=[],
        input_messages=[],
        steps=[object(), object()],
        total_cost_usd=0.01234,
    )
    line = render_done_line(AgentDoneEvent(result=result), DARK)
    output = _render(line)
    assert "0.01234" in output
    assert "2 steps" in output


def test_render_done_line_none_when_nothing_to_show() -> None:
    # A run with zero steps and zero cost still reports "0 steps" (never None).
    result = AgentRunResult(final_content="", history=[], input_messages=[])
    line = render_done_line(AgentDoneEvent(result=result), DARK)
    assert line is not None
    assert "0 steps" in _render(line)


def test_render_error_panel_shows_message_and_code() -> None:
    event = AgentErrorEvent(message="boom", code="auth", retryable=True)
    output = _render(render_error_panel(event, DARK))
    assert "boom" in output
    assert "auth" in output
    assert "retryable" in output


def test_render_user_turn_shows_text() -> None:
    output = _render(render_user_turn("hello there", DARK))
    assert "hello there" in output


def test_render_notice_variants() -> None:
    assert "⚠" in _render(render_notice("warn", "careful", DARK))
    assert "✗" in _render(render_notice("error", "boom", DARK))
    info = _render(render_notice("info", "fyi", DARK))
    assert "fyi" in info


def test_render_reasoning_panel_shows_text() -> None:
    output = _render(render_reasoning_panel("because X and Y", DARK))
    assert "because X and Y" in output
    assert "reasoning" in output


def test_tool_args_preview_single_and_multiple() -> None:
    assert tool_args_preview("read_file", {}) == ""
    assert "x.txt" in tool_args_preview("read_file", {"path": "x.txt"})
    preview = tool_args_preview("bash", {"cmd": "ls", "cwd": "/tmp"})
    assert "cmd=" in preview and "cwd=" in preview


def test_subagent_tasks_from_args_single_and_multi() -> None:
    assert subagent_tasks_from_args("agent", {"task": "do X"}) == ["do X"]
    assert subagent_tasks_from_args("agents", {"tasks": ["a", "b"]}) == ["a", "b"]
    assert subagent_tasks_from_args("agent", {}) == []
