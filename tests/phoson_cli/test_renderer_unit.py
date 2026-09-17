"""Unit tests for phoson_cli.renderer."""

import datetime
import threading
from io import StringIO
from time import sleep, monotonic

import pytest
from rich.console import Console

from phoson_agent.models import (
    RunStep,
    AgentStartEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
    AgentToolComposingEvent,
)
from phoson_cli.renderer import Renderer, ClassicSink, WaitingSpinner, SubagentSpinner
from phoson_cli.ui_protocols import TransactionalSessionViewSink

UTC = datetime.UTC


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_run_step(cost: float = 0.001) -> RunStep:
    now = datetime.datetime.now(UTC)
    return RunStep(
        kind="llm",
        started_at=now,
        ended_at=now,
        duration_ms=100,
        cost_usd=cost,
        credits=1.0,
    )


def _renderer_with_capture() -> tuple[Renderer, Console]:
    """Return a Renderer that uses a capture-capable Console."""
    console = Console(highlight=False)
    renderer = Renderer(console=console)
    return renderer, console


def test_classic_sink_does_not_advertise_replaceable_session_view() -> None:
    console = Console(record=True)
    sink = ClassicSink(Renderer(console=console))
    console.print("existing scrollback")

    sink.set_session("another-session")

    assert not isinstance(sink, TransactionalSessionViewSink)
    assert "existing scrollback" in console.export_text()


# ── WaitingSpinner tests ───────────────────────────────────────────────────────


def test_waiting_spinner_lifecycle() -> None:
    """start() creates a thread; stop() cleans up thread and stop event."""
    console = Console(highlight=False)
    spinner = WaitingSpinner(console, enabled=True)

    assert spinner._thread is None
    assert spinner._stop is None

    spinner.start("working")
    assert spinner._thread is not None
    assert spinner._thread.is_alive()
    assert spinner._stop is not None

    spinner.stop()
    assert spinner._thread is None
    assert spinner._stop is None


def test_waiting_spinner_update_while_running() -> None:
    """update() changes _label without restarting the thread."""
    console = Console(highlight=False)
    spinner = WaitingSpinner(console)

    spinner.start("initial")
    thread_id_before = id(spinner._thread)

    spinner.update("updated label")

    # The label changed but the thread identity is the same.
    assert spinner._label == "updated label"
    assert id(spinner._thread) == thread_id_before

    spinner.stop()


def test_waiting_spinner_blocked_write_keeps_lifecycle_until_retry() -> None:
    class BlockingStream:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.flushed = 0

        def write(self, text: str) -> None:
            self.entered.set()
            self.release.wait(timeout=2)

        def flush(self) -> None:
            self.flushed += 1

        def isatty(self) -> bool:
            return True

    stream = BlockingStream()
    spinner = WaitingSpinner(Console(file=stream, force_terminal=True), enabled=True)
    spinner.start("blocked")
    assert stream.entered.wait(timeout=1)
    thread = spinner._thread
    stop = spinner._stop

    spinner.stop()

    assert spinner._thread is thread
    assert spinner._stop is stop
    assert thread is not None and thread.is_alive()
    stream.release.set()
    thread.join(timeout=1)
    spinner.stop()

    assert spinner._thread is None
    assert spinner._stop is None
    assert spinner._visible is False
    assert stream.flushed == 0


def test_subagent_spinner_stores_tasks() -> None:
    """After start(tasks), _tasks equals the passed list."""
    console = Console(highlight=False)
    spinner = SubagentSpinner(console)

    tasks = ["task alpha", "task beta"]
    spinner.start(tasks)
    assert spinner._tasks == tasks
    spinner.stop()


def test_subagent_spinner_uses_only_rich_live_refresh_owner(monkeypatch) -> None:
    instances = []

    class FakeLive:
        def __init__(self, *args, **kwargs) -> None:
            self.started = False
            self.stopped = False
            instances.append(self)

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def update(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr("phoson_cli.renderer.Live", FakeLive)
    spinner = SubagentSpinner(Console(), enabled=True)

    spinner.start(["one"])
    spinner.start(["two"])
    spinner.stop()
    spinner.stop()

    assert len(instances) == 2
    assert all(instance.started and instance.stopped for instance in instances)
    assert spinner._live is None
    assert spinner._thread is None


def test_subagent_spinner_recovers_from_live_exceptions(monkeypatch) -> None:
    class FailingLive:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("start failed")

        def stop(self) -> None:
            pass

    monkeypatch.setattr("phoson_cli.renderer.Live", FailingLive)
    spinner = SubagentSpinner(Console(), enabled=True)

    with pytest.raises(RuntimeError, match="start failed"):
        spinner.start(["one"])
    spinner.stop()

    assert spinner._live is None
    assert spinner._thread is None


def test_subagent_spinner_keeps_live_reference_when_stop_fails(monkeypatch) -> None:
    class RetriableLive:
        def __init__(self, *args, **kwargs) -> None:
            self.fail = True

        def start(self) -> None:
            pass

        def stop(self) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("slow worker still active")

    monkeypatch.setattr("phoson_cli.renderer.Live", RetriableLive)
    spinner = SubagentSpinner(Console(), enabled=True)
    spinner.start(["one"])
    live = spinner._live

    with pytest.raises(RuntimeError, match="still active"):
        spinner.stop()
    assert spinner._live is live

    spinner.stop()
    assert spinner._live is None


def test_subagent_spinner_update_exception_does_not_poison_lifecycle(
    monkeypatch,
) -> None:
    class UpdateFailingLive:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def refresh(self) -> None:
            raise RuntimeError("update failed")

        def stop(self) -> None:
            pass

    monkeypatch.setattr("phoson_cli.renderer.Live", UpdateFailingLive)
    spinner = SubagentSpinner(Console(), enabled=True)
    spinner.start(["one"])

    with pytest.raises(RuntimeError, match="update failed"):
        spinner.set_progress(object())
    spinner.stop()

    assert spinner._live is None


@pytest.mark.parametrize("terminal_status", ["done", "error"])
def test_subagent_spinner_real_live_tracks_mutations(
    monkeypatch, terminal_status
) -> None:
    """Only attach once: Rich's worker must read subsequent producer mutations."""
    from types import SimpleNamespace

    from phoson_cli.tools.subagent import SubagentProgressTracker

    clock = SimpleNamespace(now=101.0)
    monkeypatch.setattr(
        "phoson_cli.tools.subagent_panel.time",
        SimpleNamespace(monotonic=lambda: clock.now),
    )
    console = Console(
        file=StringIO(),
        force_terminal=True,
        width=120,
        record=True,
    )
    spinner = SubagentSpinner(console, enabled=True)
    tracker = SubagentProgressTracker()
    tracker.register("one")

    def wait_for(*values: str) -> str:
        deadline = monotonic() + 2
        while monotonic() < deadline:
            text = console.export_text()
            if all(value in text for value in values):
                return text
            sleep(0.01)
        pytest.fail(f"Rich worker did not render {values!r}; last output: {text!r}")

    try:
        spinner.start(["one"])
        live = spinner._live
        assert live is not None
        worker = live._refresh_thread
        assert worker is not None and worker.is_alive()
        assert spinner._thread is None
        spinner.set_progress(tracker)
        wait_for("waiting")

        tracker.start(0)
        tracker.tasks[0].started_at = 100.0
        tracker.tasks[0].input_tokens = 42
        tracker.tasks[0].output_tokens = 17
        wait_for("42in / 17out", "1s")

        # Time must advance even with no new producer event.
        clock.now = 107.0
        wait_for("7s")
        if terminal_status == "done":
            tracker.finalize(0, duration_ms=9000, input_tokens=84, output_tokens=34)
            status = "✓"
        else:
            tracker.mark_error(0)
            tracker.tasks[0].last_update = 109.0
            tracker.tasks[0].input_tokens = 84
            tracker.tasks[0].output_tokens = 34
            status = "✗"
        wait_for(status, "84in / 34out", "9s")
        clock.now = 150.0
        assert "50s" not in wait_for(status, "9s")
        assert live._refresh_thread is worker

        spinner.set_progress(None)
        wait_for("waiting")
    finally:
        spinner.stop()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert spinner._live is None
    assert spinner._progress is None
    assert console._live_stack == []
    spinner.stop()

    # Restart must use a fresh worker and must not retain the previous tracker.
    console.export_text()
    try:
        spinner.start(["two"])
        replacement = spinner._live._refresh_thread
        assert replacement is not worker
        text = wait_for("two", "waiting")
        assert "84in" not in text
    finally:
        spinner.stop()
    replacement.join(timeout=1)
    assert not replacement.is_alive()
    assert console._live_stack == []


@pytest.mark.parametrize("disabled_by", ["explicit", "pipe", "term", "env", "theme"])
def test_subagent_spinner_animations_off_never_starts_live(monkeypatch, disabled_by):
    from phoson_cli.theme import get_theme

    class TtyStream(StringIO):
        def isatty(self) -> bool:
            return True

    def unexpected(*args, **kwargs):
        pytest.fail("disabled animations must not create Live or render frames")

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    if disabled_by == "term":
        monkeypatch.setenv("TERM", "dumb")
    if disabled_by == "env":
        monkeypatch.setenv("NO_COLOR", "1")
    stream = StringIO() if disabled_by == "pipe" else TtyStream()
    monkeypatch.setattr("phoson_cli.renderer.Live", unexpected)
    monkeypatch.setattr("phoson_cli.renderer.render_subagent_panel_frame", unexpected)
    spinner = SubagentSpinner(
        Console(file=stream),
        theme=get_theme("no-color" if disabled_by == "theme" else "dark"),
        enabled=False if disabled_by == "explicit" else None,
    )
    spinner.start(["one"])
    spinner.set_progress(object())
    spinner.stop()
    spinner.stop()

    assert spinner._live is None
    assert spinner._thread is None
    assert spinner._progress is None
    assert stream.getvalue() == ""


# ── Renderer.on_event step counter ────────────────────────────────────────────


def test_renderer_step_counter() -> None:
    """AgentStartEvent resets to 0; two AgentStepDoneEvents give _current_step==2."""
    renderer, _ = _renderer_with_capture()

    renderer.on_event(AgentStartEvent(max_iterations=10))
    assert renderer._current_step == 0

    renderer.on_event(AgentStepDoneEvent(step=_make_run_step(0.001)))
    renderer.on_event(AgentStepDoneEvent(step=_make_run_step(0.002)))

    renderer._spinner.stop()

    assert renderer._current_step == 2
    assert renderer._run_cost_usd > 0


# ── _on_tool_start: non-subagent tool ─────────────────────────────────────────


def test_renderer_tool_start_no_print() -> None:
    """_on_tool_start for a regular tool updates the spinner label; does not print."""
    renderer, console = _renderer_with_capture()

    with console.capture() as cap:
        renderer._on_tool_start(
            AgentToolStartEvent(tool_name="read_file", args={"path": "/tmp/x.txt"})
        )

    output = cap.get()
    # A regular tool should NOT print a console line — it only updates the spinner.
    assert output == ""
    # But the spinner label should contain the tool name.
    assert "read_file" in renderer._spinner._label

    renderer._spinner.stop()


# ── _on_tool_composing (I-128) ───────────────────────────────────────────────


def test_renderer_tool_composing_relabels_spinner_with_verb() -> None:
    """Composing relabels the (already running) spinner to the tool verb."""
    renderer, console = _renderer_with_capture()

    with console.capture() as cap:
        renderer._on_tool_start(
            AgentToolStartEvent(tool_name="read_file", args={"path": "/tmp/x.txt"})
        )
        label_before_compose = renderer._spinner._label
        renderer._on_tool_composing(
            AgentToolComposingEvent(index=0, tool_name="write_file", args_chunk='{"p":')
        )

    assert cap.get() == ""  # composing must not print a line
    assert renderer._spinner._label == "✍  writing file…"
    assert renderer._spinner._label != label_before_compose

    renderer._spinner.stop()


def test_renderer_tool_composing_starts_spinner_when_idle() -> None:
    """Composing before any tool start still produces a verb spinner."""
    renderer, _ = _renderer_with_capture()
    renderer._spinner._enabled = True

    renderer._on_tool_composing(
        AgentToolComposingEvent(index=0, tool_name="bash", args_chunk='{"command":')
    )

    assert renderer._spinner._label == "⌘  running command…"
    assert renderer._spinner._thread is not None
    renderer._spinner.stop()


def test_renderer_tool_composing_ignores_empty_name() -> None:
    """Deltas can arrive before the tool name is known — no label change."""
    renderer, _ = _renderer_with_capture()

    renderer._on_tool_composing(
        AgentToolComposingEvent(index=0, tool_name="", args_chunk='{"command":')
    )

    # No spinner thread was started by the nameless composing event.
    assert renderer._spinner._thread is None


def test_renderer_tool_composing_noop_while_live_streaming() -> None:
    """When the Live panel is open, the text is the feedback — don't touch it."""
    renderer, _ = _renderer_with_capture()
    fake_live = object()
    renderer._live = fake_live  # simulate an open Live streaming panel

    renderer._on_tool_composing(
        AgentToolComposingEvent(index=0, tool_name="bash", args_chunk='{"command":')
    )

    assert renderer._spinner._thread is None
    assert renderer._spinner._label == ""
    renderer._live = None


def test_renderer_on_event_dispatches_composing() -> None:
    """The public on_event() path routes composing to the spinner relabel."""
    renderer, console = _renderer_with_capture()

    with console.capture() as cap:
        renderer.on_event(
            AgentToolComposingEvent(index=0, tool_name="web_search", args_chunk='{"q":')
        )

    assert cap.get() == ""
    assert renderer._spinner._label == "🔎  searching the web…"
    renderer._spinner.stop()


# ── _on_tool_done: success and error ──────────────────────────────────────────


def test_renderer_tool_done_compact_success() -> None:
    """Successful tool done card contains '✓', the human verb, and duration."""
    renderer, console = _renderer_with_capture()

    with console.capture() as cap:
        renderer._on_tool_start(
            AgentToolStartEvent(
                tool_name="bash", args={"command": "pytest"}, tool_call_id="call-1"
            )
        )
        renderer._on_tool_done(
            AgentToolDoneEvent(
                tool_name="bash",
                result="ok",
                duration_ms=42,
                tool_call_id="call-1",
            )
        )

    output = cap.get()
    assert "✓" in output
    assert "running command" in output
    assert "42ms" in output


def test_renderer_tool_done_compact_error() -> None:
    """Failed tool done line contains '✗' and the error text."""
    renderer, console = _renderer_with_capture()

    with console.capture() as cap:
        renderer._on_tool_done(
            AgentToolDoneEvent(
                tool_name="write_file",
                result="",
                error="Permission denied: /etc/hosts",
                duration_ms=5,
            )
        )

    output = cap.get()
    assert "✗" in output
    assert "Permission denied" in output


# ── Reasoning buffering ────────────────────────────────────────────────────────


def test_renderer_reasoning_buffered() -> None:
    """AgentReasoningEvent chunks accumulate; finish_turn prints summary and clears."""
    renderer, console = _renderer_with_capture()

    # Send reasoning events — nothing should print yet.
    with console.capture() as cap_during:
        renderer.on_event(AgentReasoningEvent(content="thinking about "))
        renderer.on_event(AgentReasoningEvent(content="the problem"))

    assert cap_during.get() == ""
    assert len(renderer._reasoning_buf) == 2

    # finish_turn() should flush the reasoning summary.
    with console.capture() as cap_after:
        renderer.finish_turn()

    output = cap_after.get()
    assert "reasoning" in output
    assert renderer._reasoning_buf == []


# ── print_history tail rule ────────────────────────────────────────────────────


def _make_user_message(text: str = "hello"):
    from phoson_llm.schemas import Message

    return Message(role="user", content=text)


def test_print_history_tail_rule() -> None:
    """10 messages with tail=4 prints '6 messages above'."""
    renderer, console = _renderer_with_capture()
    messages = [_make_user_message(f"msg {i}") for i in range(10)]

    with console.capture() as cap:
        renderer.print_history(messages, tail=4)

    output = cap.get()
    assert "6 messages above" in output


def test_print_history_no_tail_no_rule() -> None:
    """3 messages with tail=6 (fewer than tail) does NOT print 'messages above'."""
    renderer, console = _renderer_with_capture()
    messages = [_make_user_message(f"msg {i}") for i in range(3)]

    with console.capture() as cap:
        renderer.print_history(messages, tail=6)

    output = cap.get()
    assert "messages above" not in output


# ── print_user_turn: lightweight badge ────────────────────────────────────────


def test_print_user_turn_no_panel() -> None:
    """print_user_turn output does NOT contain Panel box characters (│)."""
    renderer, console = _renderer_with_capture()

    with console.capture() as cap:
        renderer.print_user_turn("Hello, agent!")

    output = cap.get()
    # The new lightweight badge should not use Panel borders.
    assert "│" not in output


# ── print_history lightweight ─────────────────────────────────────────────────


def test_print_history_lightweight() -> None:
    """print_history for a user message does NOT use Panel box character (│)."""
    renderer, console = _renderer_with_capture()
    messages = [_make_user_message("simple question")]

    with console.capture() as cap:
        renderer.print_history(messages)

    output = cap.get()
    assert "│" not in output
