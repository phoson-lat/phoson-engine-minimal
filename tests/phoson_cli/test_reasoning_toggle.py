"""Tests for Ctrl+T reasoning show/hide (renderer capture + REPL toggle)."""

import io
import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message
from phoson_agent.models import (
    AgentDoneEvent,
    AgentRunResult,
    AgentStartEvent,
    AgentTokenEvent,
    AgentReasoningEvent,
)
from phoson_cli.renderer import Renderer

# ── Renderer: capture ─────────────────────────────────────────────────────────


def test_finish_turn_captures_reasoning_and_hints_ctrl_t() -> None:
    console = Console(record=True)
    r = Renderer(console=console)
    r._reasoning_buf = ["let me think... ", "step by step"]

    r.finish_turn()

    assert r.take_last_reasoning() == "let me think... step by step"
    assert r.take_last_reasoning() == ""  # popped, not re-served
    assert "Ctrl+T to expand" in console.export_text()


def test_finish_turn_without_reasoning_clears_previous() -> None:
    r = Renderer(console=Console(record=True))
    r._last_reasoning = "stale reasoning"
    r._reasoning_buf = []

    r.finish_turn()

    assert r.take_last_reasoning() == ""


def test_capture_partial_reasoning_on_cancel_or_error() -> None:
    r = Renderer(console=Console(record=True))
    r._reasoning_buf = ["partial thinking"]

    r.capture_partial_reasoning()

    assert r.take_last_reasoning() == "partial thinking"
    assert r._reasoning_buf == []


def _panel_text(panel) -> str:
    console = Console(record=True, width=100)
    console.print(panel)
    return console.export_text()


def test_live_panel_toggle_hides_thinking_text() -> None:
    r = Renderer(console=Console(record=True))
    r._live_reasoning = "deep thoughts about the problem"

    shown = _panel_text(r._render_live_panel())
    assert "deep thoughts about the problem" in shown

    assert r.toggle_live_reasoning() is False  # now hidden
    hidden = _panel_text(r._render_live_panel())
    assert "deep thoughts about the problem" not in hidden

    assert r.toggle_live_reasoning() is True  # back to visible
    assert "deep thoughts about the problem" in _panel_text(r._render_live_panel())


def test_render_reasoning_panel_contains_text() -> None:
    r = Renderer(console=Console(record=True))
    out = _panel_text(r.render_reasoning_panel("the full reasoning text"))
    assert "the full reasoning text" in out
    assert "reasoning" in out


def test_reasoning_event_is_captured_but_never_streamed() -> None:
    """The classic REPL never renders reasoning live (spinner instead)."""
    r = Renderer(console=Console(record=True))
    r.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    r.on_event(AgentReasoningEvent(content="secret chain of thought"))

    assert r._live is None  # no Live panel opened for reasoning
    assert r._live_reasoning == ""  # never staged for display
    assert r._reasoning_buf == ["secret chain of thought"]  # captured for Ctrl+T


def test_token_after_reasoning_panel_omits_reasoning_text() -> None:
    r = Renderer(console=Console(record=True))
    r.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    r.on_event(AgentReasoningEvent(content="secret chain of thought"))
    r.on_event(AgentTokenEvent(content="the answer"))

    assert r._live is not None
    out = _panel_text(r._render_live_panel())
    assert "secret chain of thought" not in out
    assert "the answer" in out
    r._stop_live_streaming()


def test_renderer_seeds_live_reasoning_from_flag() -> None:
    """``show_reasoning=False`` starts with the live panel hidden (#249)."""
    hidden = Renderer(console=Console(record=True), show_reasoning=False)
    assert hidden._live_show_reasoning is False
    hidden._live_reasoning = "secret thoughts"
    assert "secret thoughts" not in _panel_text(hidden._render_live_panel())

    shown = Renderer(console=Console(record=True), show_reasoning=True)
    assert shown._live_show_reasoning is True


# ── REPL: persistence + toggle ────────────────────────────────────────────────


def _make_repl(tmp_path) -> PhosonRepl:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(provider="ollama", sessions_dir=tmp_path)
        return PhosonRepl(config)


def _no_real_save(monkeypatch) -> list[tuple[object, dict]]:
    """Capture ``save_config`` calls instead of writing ~/.phoson/config.toml."""
    saved: list[tuple[object, dict]] = []
    monkeypatch.setattr(
        "phoson_cli.repl.save_config",
        lambda config, **kwargs: saved.append((config, kwargs)),
    )
    return saved


@contextlib.asynccontextmanager
async def _inflight_run(repl: PhosonRepl):
    """Attach a never-finishing run task to *repl*, cancelled on exit.

    The task is awaited on cleanup so no dangling task leaks into the
    event loop and perturbs later async tests.
    """

    async def _long_task() -> None:
        await asyncio.sleep(3600)

    task = asyncio.get_event_loop().create_task(_long_task())
    repl.current_task = task
    try:
        yield
    finally:
        repl.current_task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_repl_seeds_renderer_from_config(tmp_path) -> None:
    """The classic front end honors ``config.show_reasoning`` (#249)."""
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama", sessions_dir=tmp_path, show_reasoning=False
        )
        repl = PhosonRepl(config)
    assert repl.renderer._live_show_reasoning is False


def _fake_stream(events):
    async def stream(path, config):
        for event in events:
            yield event

    return stream


def _done_result(question: str, answer: str) -> AgentRunResult:
    return AgentRunResult(
        final_content=answer,
        history=[
            Message(role="user", content=question),
            Message(role="assistant", content=answer),
        ],
        input_messages=[Message(role="user", content=question)],
    )


@pytest.mark.asyncio
async def test_run_persists_reasoning_on_last_assistant_node(tmp_path) -> None:
    repl = _make_repl(tmp_path)
    repl._cw_resolver.resolve = AsyncMock(return_value=128_000)
    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentReasoningEvent(content="thinking hard about it..."),
        AgentTokenEvent(content="answer"),
        AgentDoneEvent(result=_done_result("q", "answer")),
    ]
    repl.engine.stream = _fake_stream(events)

    await repl._run_agent("q")

    node = repl.tree.nodes[repl.current_node_id]
    assert node.message.role == "assistant"
    assert node.metadata["reasoning"] == "thinking hard about it..."


@pytest.mark.asyncio
async def test_run_without_reasoning_leaves_metadata_empty(tmp_path) -> None:
    repl = _make_repl(tmp_path)
    repl._cw_resolver.resolve = AsyncMock(return_value=128_000)
    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentTokenEvent(content="answer"),
        AgentDoneEvent(result=_done_result("q", "answer")),
    ]
    repl.engine.stream = _fake_stream(events)

    await repl._run_agent("q")

    assert "reasoning" not in repl.tree.nodes[repl.current_node_id].metadata


async def _run_repl_with_reasoning(tmp_path) -> PhosonRepl:
    repl = _make_repl(tmp_path)
    repl._cw_resolver.resolve = AsyncMock(return_value=128_000)
    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentReasoningEvent(content="thinking hard about it..."),
        AgentTokenEvent(content="answer"),
        AgentDoneEvent(result=_done_result("q", "answer")),
    ]
    repl.engine.stream = _fake_stream(events)
    await repl._run_agent("q")
    return repl


def _capture_console(repl: PhosonRepl) -> io.StringIO:
    buf = io.StringIO()
    repl.renderer.console = Console(file=buf)
    return buf


@pytest.mark.asyncio
async def test_ctrl_t_expands_reasoning_exactly_once(tmp_path) -> None:
    repl = await _run_repl_with_reasoning(tmp_path)
    buf = _capture_console(repl)

    repl._on_reasoning_toggle()
    first = buf.getvalue()
    assert "thinking hard about it..." in first

    buf.truncate(0)
    buf.seek(0)
    repl._on_reasoning_toggle()
    second = buf.getvalue()
    assert "thinking hard about it..." not in second
    assert "already expanded" in second


def test_ctrl_t_expands_newest_reasoning_then_older(tmp_path) -> None:
    repl = _make_repl(tmp_path)
    parent = None
    for prompt, reasoning in (("old", "old reasoning"), ("new", "new reasoning")):
        user = repl.tree.append(
            parent_id=parent, message=Message(role="user", content=prompt)
        )
        assistant = repl.tree.append(
            parent_id=user.id,
            message=Message(role="assistant", content="answer"),
            metadata={"reasoning": reasoning},
        )
        parent = assistant.id
    repl.current_node_id = parent
    buf = _capture_console(repl)

    repl._on_reasoning_toggle()
    first = buf.getvalue()
    assert "new reasoning" in first
    assert "old reasoning" not in first

    buf.truncate(0)
    buf.seek(0)
    repl._on_reasoning_toggle()
    second = buf.getvalue()
    assert "old reasoning" in second


@pytest.mark.asyncio
async def test_ctrl_t_without_reasoning_shows_info(tmp_path) -> None:
    repl = _make_repl(tmp_path)
    repl._cw_resolver.resolve = AsyncMock(return_value=128_000)
    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentTokenEvent(content="answer"),
        AgentDoneEvent(result=_done_result("q", "answer")),
    ]
    repl.engine.stream = _fake_stream(events)
    await repl._run_agent("q")

    buf = _capture_console(repl)
    repl._on_reasoning_toggle()
    assert "No reasoning captured" in buf.getvalue()


@pytest.mark.asyncio
async def test_ctrl_t_toggles_live_view_during_run(tmp_path, monkeypatch) -> None:
    _no_real_save(monkeypatch)
    repl = _make_repl(tmp_path)
    repl._cw_resolver.resolve = AsyncMock(return_value=128_000)

    # Simulate an in-flight run: a task that is not done yet.
    async with _inflight_run(repl):
        repl._on_reasoning_toggle()
        assert repl.renderer._live_show_reasoning is False
        repl._on_reasoning_toggle()
        assert repl.renderer._live_show_reasoning is True


@pytest.mark.asyncio
async def test_ctrl_t_persists_show_reasoning(tmp_path, monkeypatch) -> None:
    """Ctrl+T during a run is a durable preference, like the full-screen TUI."""
    saved = _no_real_save(monkeypatch)
    repl = _make_repl(tmp_path)
    repl._cw_resolver.resolve = AsyncMock(return_value=128_000)

    async with _inflight_run(repl):
        repl._on_reasoning_toggle()
        assert repl.config.show_reasoning is False
        assert saved and saved[-1][1].get("only_fields") == {"show_reasoning"}


@pytest.mark.asyncio
async def test_ctrl_t_after_resume_reads_reasoning_from_tree(tmp_path) -> None:
    first = await _run_repl_with_reasoning(tmp_path)
    session_id = first.tree.session_id

    # A brand-new REPL on the same session dir, resuming the session.
    second = _make_repl(tmp_path)
    assert await second.load_session(session_id) is True

    buf = _capture_console(second)
    second._on_reasoning_toggle()
    assert "thinking hard about it..." in buf.getvalue()
