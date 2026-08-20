"""Tests for the Textual TUI (Textual migration, phase 3).

The app is driven headlessly with ``App.run_test`` and a fake engine
stream (same pattern as the controller tests). Skipped when the
optional ``tui`` extra is not installed.
"""

import asyncio
import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import Input, Static  # noqa: E402

from phoson_agent import (  # noqa: E402
    RunStep,
    AgentDoneEvent,
    AgentRunResult,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)
from phoson_cli.config import PhosonConfig  # noqa: E402
from phoson_cli.textual import PhosonTextualApp  # noqa: E402
from phoson_cli.controller import SessionController  # noqa: E402
from phoson_llm.schemas.inputs import Message  # noqa: E402
from phoson_cli.textual.widgets import (  # noqa: E402
    UserTurn,
    StatusLine,
    StreamingTurn,
)

_MODELS_OVERRIDE = {"models": {"test-model": {"context_window": 200000}}}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _step() -> RunStep:
    now = _now()
    return RunStep(
        kind="model", started_at=now, ended_at=now, duration_ms=5, payload={}
    )


def _done_result(messages: list[Message], content: str) -> AgentRunResult:
    step = _step()
    history = list(messages) + [Message(role="assistant", content=content)]
    return AgentRunResult(
        final_content=content,
        history=history,
        input_messages=list(messages),
        steps=[step],
        total_cost_usd=0.001,
    )


def _make_app(tmp_path: Path) -> PhosonTextualApp:
    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    return PhosonTextualApp(config)


def _patch_models() -> "patch":
    return patch(
        "phoson_cli.controller.load_models_file", return_value=_MODELS_OVERRIDE
    )


async def _submit(app: PhosonTextualApp, pilot, text: str) -> None:
    composer = app.query_one("#composer")
    composer.value = text
    composer.post_message(Input.Submitted(composer, text))


async def _wait_idle(app: PhosonTextualApp, pilot, max_pauses: int = 60) -> None:
    for _ in range(max_pauses):
        await pilot.pause()
        if not app._is_running():
            return
    pytest.fail("run did not finish in time")


async def _wait_turn_content(app: PhosonTextualApp, pilot, expected: str) -> None:
    """Wait until the turn's buffered tokens have all landed (async tasks)."""
    for _ in range(60):
        await pilot.pause()
        turn = app._last_turn
        if turn is not None and turn.content == expected:
            return
    pytest.fail("turn content did not settle in time")


def _rows(app: PhosonTextualApp) -> list:
    return list(app.query_one("#conversation").children)


def _status_text(app: PhosonTextualApp) -> str:
    return str(app.query_one("#status").render())


def _notify_text(app: PhosonTextualApp) -> str:
    return "\n".join(w._message for w in _rows(app) if isinstance(w, StatusLine))


async def test_app_mounts_widgets_and_controller(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for widget_id in ("conversation", "status", "composer"):
            assert app.query_one(f"#{widget_id}") is not None
        assert isinstance(app._controller, SessionController)
        assert "test-model" in _status_text(app)
        assert "idle" in _status_text(app)
        app.shutdown()


async def test_streaming_run_renders_turn_and_persists(tmp_path: Path) -> None:
    async def fake_stream(self, messages, config):
        yield AgentReasoningEvent(content="let me think…")
        yield AgentTokenEvent(content="Hola ")
        yield AgentTokenEvent(content="mundo")
        yield AgentToolStartEvent(tool_name="bash", args={"command": "ls"})
        yield AgentToolDoneEvent(tool_name="bash", result="ok", duration_ms=12)
        yield AgentStepDoneEvent(step=_step())
        yield AgentDoneEvent(result=_done_result(messages, "Hola mundo"))

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._controller.engine.stream = fake_stream.__get__(app._controller.engine)
        with _patch_models():
            await _submit(app, pilot, "hola")
            await _wait_idle(app, pilot)
            await _wait_turn_content(app, pilot, "Hola mundo")

        rows = _rows(app)
        # the conversation starts with the welcome line
        assert isinstance(rows[0], Static)
        user_idx = next(i for i, w in enumerate(rows) if isinstance(w, UserTurn))
        turn_idx = next(i for i, w in enumerate(rows) if isinstance(w, StreamingTurn))
        assert user_idx == turn_idx - 1
        turn = rows[turn_idx]
        assert isinstance(turn, StreamingTurn)
        assert turn.content == "Hola mundo"
        # chronological order: reasoning, content, tool card, status line
        names = [type(c).__name__ for c in turn.children]
        assert names == ["ReasoningView", "Markdown", "ToolCard", "Static"]
        # status bar back to idle with a session
        assert "idle" in _status_text(app)
        assert "session" in _status_text(app)
        # tree persisted: user + assistant nodes, reasoning on the node
        controller = app._controller
        path = controller.tree.get_path(controller.current_node_id)
        assert [n.role for n in path] == ["user", "assistant"]
        last_node = controller.tree.nodes[controller.current_node_id]
        assert last_node.metadata.get("reasoning") == "let me think…"
        # the reasoning text was popped from the turn (persisted once)
        assert turn.take_reasoning() == ""
        app.shutdown()


async def test_ctrl_t_toggles_reasoning(tmp_path: Path) -> None:
    async def fake_stream(self, messages, config):
        yield AgentReasoningEvent(content="thoughts")
        yield AgentDoneEvent(result=_done_result(messages, "ok"))

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._controller.engine.stream = fake_stream.__get__(app._controller.engine)
        with _patch_models():
            await _submit(app, pilot, "hi")
            await _wait_idle(app, pilot)

        turn = app._last_turn
        assert turn is not None and turn.reasoning_view is not None
        assert turn.reasoning_view.collapsed is True
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert turn.reasoning_view.collapsed is False
        app.shutdown()


async def test_ctrl_c_cancels_running_turn(tmp_path: Path) -> None:
    async def slow_stream(self, messages, config):
        for i in range(100):
            yield AgentTokenEvent(content=f"tok{i} ")
            await asyncio.sleep(0.05)

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._controller.engine.stream = slow_stream.__get__(app._controller.engine)
        with _patch_models():
            await _submit(app, pilot, "hi")
            await pilot.pause(0.3)
            assert app._is_running()
            await pilot.press("ctrl+c")
            for _ in range(40):
                await pilot.pause()
                if not app._is_running():
                    break
        assert not app._is_running()
        # cancel notification was shown and the app is still alive
        assert "cancel requested" in _notify_text(app)
        assert app.is_running
        app.shutdown()


async def test_ctrl_c_when_idle_exits(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app._is_running()
        await pilot.press("ctrl+c")
        for _ in range(40):
            await pilot.pause()
            if not app.is_running:
                break
        assert not app.is_running
        app.shutdown()


async def test_bash_confirmation_modal(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        async def _answer(text: str, action) -> bool:
            task = asyncio.ensure_future(app.ask_confirmation(text))
            for _ in range(20):
                await pilot.pause()
                if not task.done():
                    break
            await pilot.pause()
            await action()
            for _ in range(30):
                await pilot.pause()
                if task.done():
                    break
            assert task.done(), "modal did not answer in time"
            return task.result()

        assert (
            await _answer("Run bash command?  rm -rf /tmp/x", lambda: pilot.press("y"))
            is True
        )
        assert await _answer("Run bash command?  ls", lambda: pilot.press("n")) is False
        assert (
            await _answer("Run bash command?  pwd", lambda: pilot.click("#confirm-yes"))
            is True
        )
        assert (
            await _answer("Run bash command?  x", lambda: pilot.press("escape"))
            is False
        )
        app.shutdown()


async def test_confirmation_service_protocol(tmp_path: Path) -> None:
    from phoson_cli.ui_protocols import ConfirmationService

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        service = app._controller.confirmation
        assert isinstance(service, ConfirmationService)
        task = asyncio.ensure_future(service.confirm_bash("echo hi"))
        for _ in range(20):
            await pilot.pause()
            if not task.done():
                break
        await pilot.pause()
        await pilot.press("y")
        for _ in range(30):
            await pilot.pause()
            if task.done():
                break
        assert task.result() is True
        app.shutdown()


async def test_slash_commands(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        with _patch_models():

            async def _cmd(command: str) -> str:
                await _submit(app, pilot, command)
                for _ in range(20):
                    await pilot.pause()
                return _notify_text(app)

            assert "commands:" in await _cmd("/help")
            assert "provider ollama" in await _cmd("/env")
            assert "session cost" in await _cmd("/cost")
            assert "session tokens" in await _cmd("/tokens")
            assert "session steps" in await _cmd("/steps")
            assert "current model" in await _cmd("/model")
            tree_out = await _cmd("/tree")
            assert (
                "(empty tree)" in tree_out or "←" in tree_out or "current" in tree_out
            )
            assert "unknown command" in await _cmd("/bogus")
            assert "new session" in await _cmd("/new")
        app.shutdown()


async def test_exit_command_shuts_down_controller(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        called = []
        original = app._controller.shutdown

        async def wrapped(*a, **k):
            called.append(1)
            return await original(*a, **k)

        app._controller.shutdown = wrapped

        await _submit(app, pilot, "/exit")
        for _ in range(60):
            await pilot.pause()
            if not app.is_running:
                break
        assert not app.is_running
        # quit path awaits the (async) controller shutdown inside the loop
        await asyncio.sleep(0.1)
        assert called, "controller shutdown was not awaited on quit"
        app.shutdown()


async def test_textual_available_flag_still_gates_import(tmp_path: Path) -> None:
    # Guard: the TUI package must be importable (we are in a tui env),
    # and the main module's availability check must be true.
    from phoson_cli.__main__ import _textual_available

    assert _textual_available() is True


# ── polish: segments, composer state, persistence, welcome/footer ───────────


async def test_tool_cards_render_above_following_content(tmp_path: Path) -> None:
    async def fake_stream(self, messages, config):
        yield AgentTokenEvent(content="before ")
        yield AgentToolStartEvent(tool_name="bash", args={"command": "ls"})
        yield AgentToolDoneEvent(tool_name="bash", result="ok", duration_ms=3)
        yield AgentTokenEvent(content="after")
        yield AgentDoneEvent(result=_done_result(messages, "before after"))

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._controller.engine.stream = fake_stream.__get__(app._controller.engine)
        with _patch_models():
            await _submit(app, pilot, "hi")
            await _wait_idle(app, pilot)
            await _wait_turn_content(app, pilot, "before after")

        turn = app._last_turn
        assert turn is not None
        names = [type(c).__name__ for c in turn.children]
        # segment 1, then the card, then segment 2, then the status line
        assert names == ["Markdown", "ToolCard", "Markdown", "Static"]
        mds = [c for c in turn.children if type(c).__name__ == "Markdown"]
        # the two segments carry the text before and after the tool call
        from textual.widgets import Markdown

        assert isinstance(mds[0], Markdown)
        assert isinstance(mds[1], Markdown)
        app.shutdown()


async def test_composer_disabled_while_running(tmp_path: Path) -> None:
    async def slow_stream(self, messages, config):
        for _ in range(50):
            yield AgentTokenEvent(content="x")
            await asyncio.sleep(0.02)

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one("#composer")
        assert composer.disabled is False
        app._controller.engine.stream = slow_stream.__get__(app._controller.engine)
        with _patch_models():
            await _submit(app, pilot, "hi")
            await pilot.pause(0.2)
            assert app._is_running()
            assert composer.disabled is True
            for _ in range(60):
                await pilot.pause()
                if not app._is_running():
                    break
        assert composer.disabled is False
        assert app.screen.focused is composer
        app.shutdown()


async def test_model_command_persists_config(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    app = PhosonTextualApp(
        PhosonConfig(provider="ollama", model="old-model", sessions_dir=tmp_path)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        with _patch_models():
            await _submit(app, pilot, "/model new-model")
            for _ in range(20):
                await pilot.pause()
        config_file = home / ".phoson" / "config.toml"
        assert config_file.exists()
        assert 'model = "new-model"' in config_file.read_text()
        assert "saved" in _notify_text(app)
        app.shutdown()


async def test_welcome_line_and_footer_on_mount(tmp_path: Path) -> None:
    from textual.widgets import Footer

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # the footer shows the available bindings
        assert app.query_one(Footer) is not None
        # the conversation starts with a welcome line (model + session)
        first = _rows(app)[0]
        text = str(first.render())
        assert "test-model" in text
        assert "session" in text
        app.shutdown()
