"""Tests for the Textual TUI (Textual migration, phase 3).

The app is driven headlessly with ``App.run_test`` and a fake engine
stream (same pattern as the controller tests). Skipped when the
optional ``tui`` extra is not installed.
"""

import os
import asyncio
import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

textual = pytest.importorskip("textual")

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
    ToolCard,
    UserTurn,
    StatusLine,
    HistoryRule,
    AssistantTurn,
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
    composer.text = text
    app.submit_composer()
    await pilot.pause()


async def _wait_idle(app: PhosonTextualApp, pilot, max_pauses: int = 60) -> None:
    for _ in range(max_pauses):
        await pilot.pause()
        if not app._is_running():
            return
    pytest.fail("run did not finish in time")


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

        rows = _rows(app)
        assert isinstance(rows[0], UserTurn)
        turn = rows[1]
        assert isinstance(turn, StreamingTurn)
        assert turn.content == "Hola mundo"
        # reasoning block + tool card inside the turn
        assert turn.reasoning_view is not None
        assert any(type(c).__name__ == "ToolCard" for c in turn.children)
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
            assert "/provider" in await _cmd("/help")
            assert "provider=" in await _cmd("/env")
            assert "cost=$" in await _cmd("/cost")
            assert "tokens=" in await _cmd("/tokens")
            assert "steps=" in await _cmd("/steps")
            with patch("phoson_cli.commands.save_config"):
                assert "Model →" in await _cmd("/model test-model")
            tree_out = await _cmd("/tree")
            assert (
                "(empty session)" in tree_out
                or "←" in tree_out
                or "current" in tree_out
            )
            assert "Unknown command" in await _cmd("/bogus")
            assert "New session" in await _cmd("/new")
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


# ── scroll + input improvements ────────────────────────────────────


async def test_streaming_follows_viewport_bottom(tmp_path: Path) -> None:
    """The viewport stays pinned to the bottom while the answer streams."""

    async def many_lines_stream(self, messages, config):
        for i in range(300):
            yield AgentTokenEvent(content=f"line {i}\n\n")
        yield AgentDoneEvent(result=_done_result(messages, "done"))

    app = _make_app(tmp_path)
    async with app.run_test(size=(60, 15)) as pilot:
        await pilot.pause()
        app._controller.engine.stream = many_lines_stream.__get__(
            app._controller.engine
        )
        with _patch_models():
            await _submit(app, pilot, "hi")
            conv = app.conversation()
            # wait until the content actually overflows (Markdown renders
            # its blocks asynchronously, so the height converges in a pass)
            for _ in range(100):
                await pilot.pause()
                if conv.max_scroll_y > 50:
                    break
            assert conv.max_scroll_y > 50, "content should overflow the view"
            # the viewport must be pinned to the bottom while streaming
            assert conv.scroll_offset.y >= conv.max_scroll_y - 24
            await _wait_idle(app, pilot)
            for _ in range(50):
                await pilot.pause()
            assert conv.scroll_offset.y >= conv.max_scroll_y - 24
        app.shutdown()


async def test_follow_releases_when_user_scrolls_up(tmp_path: Path) -> None:
    """Scrolling up to read history is never fought by the auto-follow."""

    async def slow_many_lines(self, messages, config):
        for i in range(300):
            yield AgentTokenEvent(content=f"line {i}\n\n")
            await asyncio.sleep(0.02)
        yield AgentDoneEvent(result=_done_result(messages, "done"))

    app = _make_app(tmp_path)
    async with app.run_test(size=(60, 15)) as pilot:
        await pilot.pause()
        app._controller.engine.stream = slow_many_lines.__get__(app._controller.engine)
        with _patch_models():
            await _submit(app, pilot, "hi")
            conv = app.conversation()
            for _ in range(100):
                await pilot.pause()
                if conv.max_scroll_y > 200:
                    break
            assert conv.max_scroll_y > 200
            # user scrolls up one page to read history
            await pilot.press("pageup")
            await pilot.pause()
            assert conv.scroll_offset.y < conv.max_scroll_y - 24
            before = conv.scroll_offset.y
            # more tokens keep arriving — the viewport must NOT be pulled
            # back down (a couple of pages of growth is well over a page)
            await pilot.pause(1.0)
            assert conv.scroll_offset.y < conv.max_scroll_y - 24
            assert conv.scroll_offset.y <= before + 5
        app.shutdown()


async def test_pageup_pagedown_scroll_with_composer_focused(tmp_path: Path) -> None:
    """PgUp/PgDn page-scroll the conversation with the composer focused.

    The auto-follow pins the viewport to the bottom while content is
    mounted; PgUp releases the pin and moves up a page, PgDn moves back
    down and re-arms the pin at the bottom.
    """
    app = _make_app(tmp_path)
    async with app.run_test(size=(60, 15)) as pilot:
        await pilot.pause()
        # build a tall conversation without streaming
        for i in range(40):
            await app.conversation().mount(
                AssistantTurn(f"answer {i}\n\n" + "word " * 40)
            )
        await pilot.pause()
        conv = app.conversation()
        assert conv.max_scroll_y > 50
        # the auto-follow pins the viewport to the bottom (poll: layout
        # and the 0.1s tick converge within a few passes)
        for _ in range(30):
            if conv.scroll_offset.y >= conv.max_scroll_y - 24:
                break
            await pilot.pause()
        assert conv.scroll_offset.y >= conv.max_scroll_y - 24
        first = conv.scroll_offset.y
        await pilot.press("pageup")
        await pilot.pause()
        assert conv.scroll_offset.y < first - 2  # a whole page up
        assert app._follow is False
        second = conv.scroll_offset.y
        await pilot.press("pagedown")
        await pilot.pause()
        assert conv.scroll_offset.y > second  # a whole page down
        # focus is still on the composer (the bindings must not steal it)
        assert app.focused is not None
        assert app.focused.id == "composer"
        app.shutdown()


async def test_debug_log_records_keys_and_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PHOSON_TEXTUAL_DEBUG captures what the user's terminal sent."""
    log_file = tmp_path / "tui-debug.log"
    monkeypatch.setenv("PHOSON_TEXTUAL_DEBUG", str(log_file))
    app = _make_app(tmp_path)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer")
            composer.focus()
            await pilot.press("a")
            await pilot.press("b")
            await pilot.press("c")
            await pilot.press("ctrl+t")
            await pilot.pause()
            text = log_file.read_text(encoding="utf-8")
    finally:
        monkeypatch.delenv("PHOSON_TEXTUAL_DEBUG", raising=False)

    assert "mounted" in text
    # printable chars go through the composer hook (Input stops their
    # propagation, so the app-level hook would not see them)
    for ch in ("a", "b", "c"):
        assert f"composer-key key={ch} character={ch}" in text
    # control keys bubble to the app level
    assert "app-key key=ctrl+t" in text


async def test_legacy_keys_env_forces_xterm_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PHOSON_TEXTUAL_LEGACY_KEYS maps onto Textual's kitty-key opt-out."""
    from phoson_cli.__main__ import _apply_textual_key_env

    monkeypatch.delenv("TEXTUAL_DISABLE_KITTY_KEY", raising=False)
    monkeypatch.delenv("PHOSON_TEXTUAL_LEGACY_KEYS", raising=False)
    _apply_textual_key_env()
    assert "TEXTUAL_DISABLE_KITTY_KEY" not in os.environ

    monkeypatch.setenv("PHOSON_TEXTUAL_LEGACY_KEYS", "1")
    _apply_textual_key_env()
    assert os.environ["TEXTUAL_DISABLE_KITTY_KEY"] == "1"


def test_kitty_associated_text_flag_is_zeroed_for_tui() -> None:
    """The TUI never asks Kitty for associated-text or report-all-keys.

    Textual 8.2.8's XTermParser mis-parses the ``u;<codepoint>`` suffix
    (see the canary test below), so associated-text must be off before
    the driver starts its input thread — otherwise every key typed in
    Kitty turns into ``key + ';<digits>'`` garbage in the composer.

    Report-all-keys is also off: without associated text it delivers
    Shift+digit as ``shift+7`` with ``character=None``, so Spanish ``/``
    (Shift+7) never inserts. Disambiguate stays on so Ctrl combos work.
    """
    from textual.drivers import linux_driver

    from phoson_cli.__main__ import _workaround_kitty_associated_text

    saved_associated = getattr(linux_driver, "KITTY_REPORT_ASSOCIATED_TEXT", 0)
    saved_all_keys = getattr(linux_driver, "KITTY_REPORT_ALL_KEYS", 0)
    try:
        linux_driver.KITTY_REPORT_ASSOCIATED_TEXT = 0b00010000
        linux_driver.KITTY_REPORT_ALL_KEYS = 0b00001000
        _workaround_kitty_associated_text()
        assert linux_driver.KITTY_REPORT_ASSOCIATED_TEXT == 0
        assert linux_driver.KITTY_REPORT_ALL_KEYS == 0
        # Ctrl combos keep working via disambiguate
        assert linux_driver.KITTY_DISAMBIGUATE_ESCAPE_CODES == 0b00000001
    finally:
        linux_driver.KITTY_REPORT_ASSOCIATED_TEXT = saved_associated
        linux_driver.KITTY_REPORT_ALL_KEYS = saved_all_keys


def test_canary_kitty_associated_text_parser_bug() -> None:
    """Canary: the Textual 8.2.8 parser still mis-parses ``u;<codepoint>``.

    If a future Textual release parses the associated-text suffix
    correctly (one Key event, no leftover ``;97``), this test fails —
    remove the canary and the _workaround_kitty_associated_text call,
    since the flag will no longer need to be disabled.
    """
    from textual._xterm_parser import XTermParser

    events = list(XTermParser().feed("\x1b[97u;97"))  # Kitty: key 'a'
    assert events and events[0].character == "a"
    # BUG: the leftover ';97' surfaces as extra keys (';' '9' '7')
    assert len(events) > 1


# ── resume, parallel tools, composer, pickers ──────────────────────────────


async def test_print_history_replays_tail_not_head(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        messages = [
            Message(role="user", content=f"u{i}")
            if i % 2 == 0
            else Message(role="assistant", content=f"a{i}")
            for i in range(10)
        ]
        app._sink.print_history(messages, tail=4)
        for _ in range(20):
            await pilot.pause()
        rows = _rows(app)
        texts = []
        for row in rows:
            if isinstance(row, HistoryRule):
                texts.append("rule")
            elif isinstance(row, UserTurn):
                texts.append(str(row.render()))
            elif isinstance(row, AssistantTurn):
                texts.append(row._text)
        assert texts[0] == "rule"
        joined = "\n".join(texts)
        assert "u0" not in joined and "a1" not in joined
        assert "u8" in joined and "a9" in joined
        app.shutdown()


async def test_load_session_keeps_replayed_history(tmp_path: Path) -> None:
    async def fake_stream(self, messages, config):
        yield AgentTokenEvent(content="hello-session")
        yield AgentDoneEvent(result=_done_result(messages, "hello-session"))

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._controller.engine.stream = fake_stream.__get__(app._controller.engine)
        with _patch_models():
            await _submit(app, pilot, "remember this")
            await _wait_idle(app, pilot)
            session_id = app._controller.tree.session_id
            await _submit(app, pilot, "/new")
            for _ in range(20):
                await pilot.pause()
            assert not any(isinstance(r, UserTurn) for r in _rows(app))
            facade = app._command_handler.repl
            ok = await facade.load_session(session_id)
            assert ok is True
            for _ in range(20):
                await pilot.pause()
        rows = _rows(app)
        assert any(isinstance(r, UserTurn) for r in rows)
        assert any(isinstance(r, AssistantTurn) for r in rows)
        app.shutdown()


async def test_parallel_tool_cards_do_not_clobber(tmp_path: Path) -> None:
    async def fake_stream(self, messages, config):
        yield AgentToolStartEvent(
            tool_name="bash", args={"command": "one"}, tool_call_id="c1"
        )
        yield AgentToolStartEvent(
            tool_name="bash", args={"command": "two"}, tool_call_id="c2"
        )
        yield AgentToolDoneEvent(
            tool_name="bash", result="ok", duration_ms=5, tool_call_id="c1"
        )
        yield AgentToolDoneEvent(
            tool_name="bash",
            result="ok",
            duration_ms=9,
            error="boom",
            tool_call_id="c2",
        )
        yield AgentDoneEvent(result=_done_result(messages, "done"))

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._controller.engine.stream = fake_stream.__get__(app._controller.engine)
        with _patch_models():
            await _submit(app, pilot, "hi")
            await _wait_idle(app, pilot)
        turn = next(r for r in _rows(app) if isinstance(r, StreamingTurn))
        cards = [c for c in turn.children if isinstance(c, ToolCard)]
        assert len(cards) == 2
        rendered = " ".join(str(c.render()) for c in cards)
        assert "✓" in rendered and "✗" in rendered
        app.shutdown()


async def test_composer_enter_sends_shift_enter_is_newline(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one("#composer")
        composer.focus()
        composer.text = "line1"
        composer.cursor_location = (0, len("line1"))
        await composer._on_key(textual.events.Key("shift+enter", "\n"))
        assert composer.text.startswith("line1\n")
        composer.text = "/bogus"
        await composer._on_key(textual.events.Key("enter", "\n"))
        for _ in range(20):
            await pilot.pause()
        assert composer.text == ""
        assert "Unknown command" in _notify_text(app)
        app.shutdown()


async def test_model_picker_screen_selects(tmp_path: Path) -> None:
    from phoson_cli.models import ModelOption

    app = _make_app(tmp_path)
    models = [
        ModelOption(id="aaa", label="A", provider="ollama"),
        ModelOption(id="bbb", label="B", provider="ollama"),
    ]
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        task = asyncio.ensure_future(
            app._command_handler.host.pick_model(models, "aaa")
        )
        for _ in range(20):
            await pilot.pause()
            if not task.done():
                break
        await pilot.press("down")
        await pilot.press("enter")
        for _ in range(30):
            await pilot.pause()
            if task.done():
                break
        assert task.done()
        assert task.result().model_id == "bbb"
        app.shutdown()


async def test_user_markup_is_escaped(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._sink.on_user_message("[red]boom[/]", Message(role="user", content="x"))
        for _ in range(10):
            await pilot.pause()
        row = next(r for r in _rows(app) if isinstance(r, UserTurn))
        assert r"\[red]boom" in str(row.render()) or "[red]boom" in str(row.render())
        app.shutdown()
