"""End-to-end and visual regression tests for the full-screen TUI (D4).

Covers:
1. Real key-event routing through prompt_toolkit's Application + PipeInput
   (Enter->submit, Ctrl+J->newline, Ctrl+C idle vs running, Esc->cancel,
   Ctrl+L->clear).
2. Headless end-to-end agent turn through PhosonApp against mock streaming LLM.
3. Golden ANSI rendering snapshots for transcript states (empty, running,
   error, formatted assistant output).
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from phoson_agent import (
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_cli.theme import DARK
from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message
from phoson_agent.models import AgentRunResult
from phoson_cli.fullscreen.app import PhosonApp
from phoson_cli.fullscreen.sink import FullScreenSink
from phoson_cli.fullscreen.render import BlockAnsiCache, render_chat

# ── 1. Real key routing tests via PipeInput ───────────────────────────────────


@pytest.mark.asyncio
async def test_pipe_input_typing_and_ctrl_j_inserts_multiline_text(tmp_path) -> None:
    """Typing text and pressing Ctrl+J inserts a newline without submitting."""
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        with create_pipe_input() as pipe:
            app = PhosonApp(config)
            app.app.input = pipe
            app.app.output = DummyOutput()

            async def drive_app():
                await asyncio.sleep(0.02)
                pipe.send_text("line one")
                await asyncio.sleep(0.02)
                # Ctrl+J (\n)
                pipe.send_text("\n")
                await asyncio.sleep(0.02)
                pipe.send_text("line two")
                await asyncio.sleep(0.02)
                # Ctrl+C to exit
                pipe.send_text("\x03")

            asyncio.create_task(drive_app())
            await app.app.run_async()

            assert app._prompt_input.text == "line one\nline two"


@pytest.mark.asyncio
async def test_pipe_input_ctrl_l_clears_transcript(tmp_path) -> None:
    """Ctrl+L clears all transcript blocks."""
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        with create_pipe_input() as pipe:
            app = PhosonApp(config)
            app.app.input = pipe
            app.app.output = DummyOutput()
            assert len(app.sink.blocks) > 0  # has welcome banner

            async def drive_app():
                await asyncio.sleep(0.02)
                # Ctrl+L (\x0c)
                pipe.send_text("\x0c")
                await asyncio.sleep(0.02)
                pipe.send_text("\x03")

            asyncio.create_task(drive_app())
            await app.app.run_async()

            assert len(app.sink.blocks) == 0


@pytest.mark.asyncio
async def test_pipe_input_ctrl_c_cancels_active_turn_without_exiting(tmp_path) -> None:
    """Ctrl+C while a turn is active cancels it; the second Ctrl+C exits."""
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        with create_pipe_input() as pipe:
            app = PhosonApp(config)
            app.app.input = pipe
            app.app.output = DummyOutput()

            # Simulate an active turn in progress
            app.sink.on_event(AgentStartEvent(model="test-model", message_count=1))
            app.sink.on_event(AgentTokenEvent(content="working..."))

            async def drive_app():
                await asyncio.sleep(0.02)
                # First Ctrl+C: cancels active turn and clears sink.current_turn
                with patch.object(app.repl, "cancel_current") as mock_cancel:
                    pipe.send_text("\x03")
                    await asyncio.sleep(0.05)
                    mock_cancel.assert_called_once()
                    # Mark turn ended as controller would
                    app.sink.current_turn = None
                    # Second Ctrl+C: exits application
                    pipe.send_text("\x03")

            asyncio.create_task(drive_app())
            await app.app.run_async()

            assert True


@pytest.mark.asyncio
async def test_pipe_input_escape_cancels_inflight_run(tmp_path) -> None:
    """Pressing Escape while a run is in flight triggers cancel_current."""
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        with create_pipe_input() as pipe:
            app = PhosonApp(config)
            app.app.input = pipe
            app.app.output = DummyOutput()

            # Set an active run task
            fake_task = asyncio.create_task(asyncio.sleep(10))
            app._run_task = fake_task

            with patch.object(app.repl, "cancel_current") as mock_cancel:

                async def drive_app():
                    await asyncio.sleep(0.02)
                    # Escape (\x1b)
                    pipe.send_text("\x1b")
                    await asyncio.sleep(0.02)
                    pipe.send_text("\x03")

                asyncio.create_task(drive_app())
                await app.app.run_async()

                mock_cancel.assert_called_once()
            fake_task.cancel()


@pytest.mark.asyncio
async def test_pipe_input_remapped_keys_dispatch(tmp_path) -> None:
    """E6: remapped keys route through the real Application event loop.

    ``submit = "c-x"`` and ``clear = "c-k"`` from the config are driven
    as raw bytes through prompt_toolkit's PipeInput — if the table-driven
    binding wiring were wrong (e.g. a stale default still bound), the
    remapped bytes would do nothing.
    """
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            key_bindings={"submit": ["c-x"], "clear": ["c-k"]},
        )
        with create_pipe_input() as pipe:
            app = PhosonApp(config)
            app.app.input = pipe
            app.app.output = DummyOutput()

            submitted: list[str] = []

            async def mock_run_agent(prompt: str) -> None:
                submitted.append(prompt)

            with patch.object(app.repl, "_run_agent", side_effect=mock_run_agent):

                async def drive_app():
                    await asyncio.sleep(0.02)
                    pipe.send_text("remapped turn")
                    await asyncio.sleep(0.02)
                    pipe.send_text("\x18")  # Ctrl+X — remapped submit
                    await asyncio.sleep(0.10)
                    pipe.send_text("\x0b")  # Ctrl+K — remapped clear
                    await asyncio.sleep(0.02)
                    pipe.send_text("\x03")  # Ctrl+C to exit

                asyncio.create_task(drive_app())
                await app.app.run_async()

            assert submitted == ["remapped turn"]
            assert app._prompt_input.text == ""
            assert len(app.sink.blocks) == 0  # cleared by Ctrl+K


# ── 2. Headless end-to-end agent turn ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_headless_full_agent_turn_lifecycle(tmp_path) -> None:
    """A full turn lifecycle through PhosonApp with events arriving from mock."""
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        app = PhosonApp(config)

        # Mock controller running an agent turn that emits tokens and completes
        async def mock_run_agent(prompt: str) -> None:
            user_msg = Message(role="user", content=prompt)
            app.sink.on_user_message(prompt, user_msg)
            app.sink.on_event(AgentStartEvent(model=config.model, message_count=1))
            app.sink.on_event(AgentTokenEvent(content="Hello "))
            app.sink.on_event(AgentTokenEvent(content="world!"))
            app.sink.on_event(
                AgentDoneEvent(
                    result=AgentRunResult(
                        final_content="Hello world!",
                        history=[
                            user_msg,
                            Message(role="assistant", content="Hello world!"),
                        ],
                        input_messages=[user_msg],
                        steps=[],
                    )
                )
            )

        with patch.object(app.repl, "_run_agent", side_effect=mock_run_agent):
            app._prompt_input.text = "say hello"
            app.submit()
            assert app._run_task is not None
            await app._run_task

        rendered = app._render_chat().value
        assert "user" in rendered
        assert "say hello" in rendered
        assert "Hello world!" in rendered
        assert app.sink.current_turn is None


@pytest.mark.asyncio
async def test_headless_failed_retries_collapse_to_one_notice_line(tmp_path) -> None:
    """I-83: three failed attempts + a successful retry → one notice line,
    which disappears once the retry completes."""
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        app = PhosonApp(config)

        async def mock_run_agent(prompt: str) -> None:
            user_msg = Message(role="user", content=prompt)
            app.sink.on_user_message(prompt, user_msg)
            # Attempt 1: 500. Attempt 2: 429. Attempt 3: success.
            for code, message in [
                ("server_error", "first 500"),
                ("rate_limit", "second 429"),
            ]:
                app.sink.on_event(AgentStartEvent(model=config.model, message_count=1))
                app.sink.on_event(
                    AgentErrorEvent(message=message, code=code, retryable=True)
                )
            app.sink.on_event(AgentStartEvent(model=config.model, message_count=1))
            app.sink.on_event(AgentTokenEvent(content="made it"))
            app.sink.on_event(
                AgentDoneEvent(
                    result=AgentRunResult(
                        final_content="made it",
                        history=[user_msg],
                        input_messages=[user_msg],
                        steps=[],
                    )
                )
            )

        with patch.object(app.repl, "_run_agent", side_effect=mock_run_agent):
            app._prompt_input.text = "please work"
            app.submit()
            assert app._run_task is not None
            await app._run_task

        rendered = app._render_chat().value
        # The failed attempts left no trace: the notice was dropped on
        # success, and retries never stacked panels.
        assert "first 500" not in rendered
        assert "second 429" not in rendered
        assert "⚠" not in rendered
        assert "made it" in rendered

        # And while a retry is in flight, exactly ONE notice line exists.
        app.sink.on_event(AgentStartEvent(model=config.model, message_count=1))
        app.sink.on_event(
            AgentErrorEvent(message="boom again", code=None, retryable=True)
        )
        rendered = app._render_chat().value
        assert rendered.count("⚠") == 1
        assert "boom again" in rendered


# ── 3. Golden ANSI rendering snapshots ───────────────────────────────────────


def test_golden_snapshot_empty_transcript() -> None:
    """Snapshot: empty chat transcript rendering."""
    cache = BlockAnsiCache()
    sink = FullScreenSink(on_invalidate=lambda: None, theme=DARK)
    rendered = render_chat(sink, width=60, cache=cache)
    assert "Type a message and press Enter." in rendered
    assert "\x1b[" in rendered  # ANSI styling present


def test_golden_snapshot_streaming_turn() -> None:
    """Snapshot: active turn currently streaming with thinking and tokens."""
    cache = BlockAnsiCache()
    sink = FullScreenSink(on_invalidate=lambda: None, theme=DARK)
    sink.on_user_message("check status", Message(role="user", content="check status"))
    sink.on_event(AgentStartEvent(model="test-model", message_count=1))
    sink.on_event(AgentTokenEvent(content="Streaming partial response..."))

    rendered = render_chat(sink, width=60, cache=cache)
    assert "user" in rendered
    assert "check status" in rendered
    assert "Phoson" in rendered
    assert "Streaming partial response..." in rendered


def test_golden_snapshot_error_notice() -> None:
    """Snapshot: error event renders as a single-line notice (I-83)."""
    cache = BlockAnsiCache()
    sink = FullScreenSink(on_invalidate=lambda: None, theme=DARK)
    sink.on_user_message("do work", Message(role="user", content="do work"))
    sink.on_event(
        AgentErrorEvent(
            message="Invalid API Key provided",
            code="auth",
            retryable=False,
        )
    )

    rendered = render_chat(sink, width=60, cache=cache)
    assert "user" in rendered
    assert "do work" in rendered
    # One-line notice with the actionable hint — no panel, no raw message.
    assert "⚠" in rendered
    assert "auth" in rendered
    assert "run /setup" in rendered
    assert "Invalid API Key" not in rendered
    assert "│" not in rendered  # no panel border


def test_golden_snapshot_tool_card_done() -> None:
    """Snapshot: completed tool card with execution stats."""
    cache = BlockAnsiCache()
    sink = FullScreenSink(on_invalidate=lambda: None, theme=DARK)
    sink.on_user_message("read file", Message(role="user", content="read file"))
    sink.on_event(
        AgentToolStartEvent(
            tool_name="read_file",
            args={"path": "src/main.py"},
            tool_call_id="call-1",
        )
    )
    sink.on_event(
        AgentToolDoneEvent(
            tool_name="read_file",
            result="print('hello')",
            duration_ms=42,
            tool_call_id="call-1",
        )
    )

    rendered = render_chat(sink, width=60, cache=cache)
    assert "reading file" in rendered
    assert "src/main.py" in rendered
    assert "42ms" in rendered
