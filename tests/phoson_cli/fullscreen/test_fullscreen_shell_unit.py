"""Tests for the full-screen app shell (layout, scroll, key bindings).

These exercise ``PhosonApp`` without spawning a real ``Application``
event loop — we drive the bound key handlers and scroll math directly,
same pattern as ``tests/phoson_cli/test_picker_base.py``. Submitting a
message schedules a background asyncio task (``run_turn`` runs on the
same loop as the ``Application``), so those tests are ``async def`` and
patch ``PhosonRepl._run_agent`` to avoid a real network call.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.fullscreen.app import PhosonApp


def _trigger(app: PhosonApp, key: str) -> None:
    """Look up the handler registered for ``key`` on ``app.app`` and invoke it.

    Calls the *first* matching handler regardless of its filter — fine
    for tests that don't care about Float gating (there's only ever one
    real candidate). For gating tests, use ``_trigger_if_enabled``.
    """
    aliases = {"enter": "c-m", "return": "c-m"}
    target = aliases.get(key.lower(), key.lower())

    bindings = app.app.key_bindings.bindings
    for binding in bindings:
        for k in binding.keys:
            value = getattr(k, "value", str(k))
            if str(value).lower() == target:
                binding.handler(MagicMock())
                return
    raise KeyError(f"No binding for {key!r}")


def _trigger_if_enabled(app: PhosonApp, key: str) -> bool:
    """Like ``_trigger``, but only invokes bindings whose filter is True.

    Needed to test Float gating (``ConditionalKeyBindings``): a naive
    "call the first match" helper would invoke a base-app binding whose
    filter has gone False (e.g. Ctrl+Q while a picker Float is open),
    which real prompt_toolkit dispatch would have skipped. Returns
    whether an enabled binding was found and invoked.
    """
    aliases = {"enter": "c-m", "return": "c-m"}
    target = aliases.get(key.lower(), key.lower())

    for binding in app.app.key_bindings.bindings:
        for k in binding.keys:
            value = getattr(k, "value", str(k))
            if str(value).lower() == target and binding.filter():
                binding.handler(MagicMock())
                return True
    return False


@pytest.fixture
def app(tmp_path) -> PhosonApp:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            # Keep input-history writes out of the developer's real file.
            history_file=tmp_path / "history.txt",
        )
        return PhosonApp(config)


def test_shell_builds_full_screen_application(app: PhosonApp) -> None:
    assert app.app.layout is not None
    assert app.app.style is not None


def test_shell_creates_nested_history_directory(tmp_path) -> None:
    """A2: FileHistory can use a configured path whose parent is absent."""
    history_file = tmp_path / "nested" / "history" / "input.txt"

    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        PhosonApp(
            PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                history_file=history_file,
            )
        )

    assert history_file.parent.is_dir()


async def test_submit_schedules_a_run_and_clears_input(app: PhosonApp) -> None:
    with patch.object(app.repl, "_run_agent", new=AsyncMock(return_value=None)) as run:
        app._prompt_input.text = "hello world"
        _trigger(app, "enter")
        assert app._prompt_input.text == ""
        await asyncio.sleep(0)  # let the background task run

    run.assert_awaited_once_with("hello world")


async def test_submit_ignores_blank_input(app: PhosonApp) -> None:
    with patch.object(app.repl, "_run_agent", new=AsyncMock(return_value=None)) as run:
        app._prompt_input.text = "   "
        _trigger(app, "enter")
        await asyncio.sleep(0)

    run.assert_not_awaited()


async def test_submit_ignores_input_while_a_run_is_in_flight(app: PhosonApp) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run_agent(text: str) -> None:
        started.set()
        await release.wait()

    with patch.object(app.repl, "_run_agent", new=slow_run_agent):
        app._prompt_input.text = "first"
        _trigger(app, "enter")
        await started.wait()

        # A second Enter while the first turn is still in flight must be a no-op.
        app._prompt_input.text = "second"
        _trigger(app, "enter")
        await asyncio.sleep(0)
        assert app._prompt_input.text == "second"  # not cleared — submit was a no-op

        release.set()
        await app._run_task


async def test_submit_while_run_in_flight_keeps_text_and_warns(app: PhosonApp) -> None:
    """A4: Enter during a run must not be silent — keep the text and warn.

    The user's draft is preserved (not cleared) and a warn notice explains
    that a turn is already running, so a no-op Enter no longer looks like a
    frozen app.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run_agent(text: str) -> None:
        started.set()
        await release.wait()

    with patch.object(app.repl, "_run_agent", new=slow_run_agent):
        app._prompt_input.text = "first"
        _trigger(app, "enter")
        await started.wait()

        blocks_before = len(app.sink.blocks)
        app._prompt_input.text = "my draft"
        _trigger(app, "enter")
        await asyncio.sleep(0)

        # The draft survives the rejected submit.
        assert app._prompt_input.text == "my draft"
        # A warn notice was appended to the transcript.
        assert len(app.sink.blocks) == blocks_before + 1
        assert "already running" in app._render_chat().value

        release.set()
        await app._run_task


async def test_ctrl_j_inserts_newline_and_enter_still_submits(app: PhosonApp) -> None:
    """A2: Ctrl+J is the newline key; Enter keeps submitting.

    Shift+Enter/Ctrl+Enter are not portable (prompt_toolkit's VT100 parser
    does not map the CSI-u sequences modern terminals emit), so Ctrl+J — a
    single universal byte — carries the newline role. The app-level ``enter``
    binding must keep winning over the buffer's built-in multiline newline.
    """
    app._prompt_input.text = "line one"
    app._prompt_input.buffer.cursor_position = len("line one")

    _trigger(app, "c-j")
    assert app._prompt_input.text == "line one\n"

    app._prompt_input.buffer.insert_text("line two")
    assert app._prompt_input.text == "line one\nline two"

    # Enter submits the whole multiline text and clears the input.
    with patch.object(app.repl, "_run_agent", new=AsyncMock(return_value=None)):
        _trigger(app, "enter")
        await asyncio.sleep(0)

    assert app._run_task is not None
    await app._run_task


def test_input_is_multiline_with_dynamic_height(app: PhosonApp) -> None:
    """The TextArea grows with content up to a cap, then scrolls internally."""
    from prompt_toolkit.layout.dimension import Dimension

    assert app._prompt_input.buffer.multiline() is True

    window = app._prompt_input.window
    height = window.height
    assert isinstance(height, Dimension)
    assert height.max == 5  # _INPUT_MAX_LINES
    assert height.min == 1


async def test_submit_persists_input_to_shared_history_file(app: PhosonApp) -> None:
    """A2: submitted inputs survive restarts via ~/.phoson/history.txt.

    The custom submit path bypasses the buffer's accept handler, so it must
    append to the history explicitly. The file format is prompt_toolkit's
    FileHistory one (``+``-prefixed lines), shared with the classic REPL.
    """
    history_path = app.repl.config.history_file

    with patch.object(app.repl, "_run_agent", new=AsyncMock(return_value=None)):
        app._prompt_input.text = "remembered message"
        _trigger(app, "enter")
        await asyncio.sleep(0)
        await app._run_task

    content = history_path.read_text(encoding="utf-8")
    assert "+remembered message" in content


async def test_history_survives_an_app_restart(app: PhosonApp, tmp_path) -> None:
    """A2 criterio de listo: ↑ after restarting the TUI recalls the last
    message from the previous session (verified at the storage layer: a new
    PhosonApp over the same history file loads the previous entries)."""
    history_path = tmp_path / "history.txt"

    def build_app() -> PhosonApp:
        with patch("phoson_cli.controller.build_chat") as mock_build:
            mock_build.return_value = MagicMock()
            config = PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                history_file=history_path,
            )
            return PhosonApp(config)

    first = build_app()
    with patch.object(first.repl, "_run_agent", new=AsyncMock(return_value=None)):
        first._prompt_input.text = "first session message"
        _trigger(first, "enter")
        await asyncio.sleep(0)
        await first._run_task

    # "Restart": a brand-new PhosonApp instance over the same file.
    second = build_app()
    strings = second._prompt_input.buffer.history.load_history_strings()
    assert "first session message" in list(strings)
    app.sink.blocks = ["one", "two"]
    app.sink.dirty = False
    app._chat_scroll_top = 5
    app._auto_scroll = False

    _trigger(app, "c-l")

    assert app.sink.blocks == []
    assert app.sink.dirty is True
    assert app._auto_scroll is True
    assert app._chat_scroll_top == 0


def test_ctrl_q_and_ctrl_c_request_exit_when_idle(app: PhosonApp) -> None:
    with patch.object(app.app, "exit") as mock_exit:
        _trigger(app, "c-q")
    mock_exit.assert_called_once()

    with patch.object(app.app, "exit") as mock_exit:
        _trigger(app, "c-c")
    mock_exit.assert_called_once()


async def test_ctrl_c_exits_directly_before_any_content_is_visible(
    app: PhosonApp,
) -> None:
    """Whenever ``sink.current_turn`` is None — before AgentStartEvent

    fires, or after AgentDoneEvent/AgentErrorEvent/flush_line already
    cleared it — nothing is visibly happening (no spinner, no status
    change), whether that's because the turn hasn't started yet or
    because only invisible trailing bookkeeping (persisting reasoning,
    saving the session) remains. Either way Ctrl+C/Ctrl+Q should just
    quit rather than silently "cancel" something the user can't see —
    the still-pending run task gets cancelled for free by the
    Application shutting down (prompt_toolkit's own background-task
    contract), instead of forcing a confusing second keypress.
    """
    started = asyncio.Event()

    async def slow_run_agent(text: str) -> None:
        started.set()
        await asyncio.sleep(10)  # never touches the sink — nothing visible yet

    with patch.object(app.repl, "_run_agent", new=slow_run_agent):
        app._prompt_input.text = "hello"
        _trigger(app, "enter")
        await started.wait()

        assert app.sink.current_turn is None

        with patch.object(app.app, "exit") as mock_exit:
            _trigger(app, "c-c")

        mock_exit.assert_called_once()
        app._run_task.cancel()  # tear down the still-pending fake turn


async def test_ctrl_c_cancels_instead_of_exiting_while_content_is_visible(
    app: PhosonApp,
) -> None:
    from phoson_cli.fullscreen.sink import CurrentTurn

    started = asyncio.Event()

    async def slow_run_agent(text: str) -> None:
        app.sink.current_turn = CurrentTurn(model="m", max_steps=10)
        started.set()
        await asyncio.sleep(10)

    with patch.object(app.repl, "_run_agent", new=slow_run_agent):
        app._prompt_input.text = "hello"
        _trigger(app, "enter")
        await started.wait()

        with patch.object(app.repl, "cancel_current") as mock_cancel:
            with patch.object(app.app, "exit") as mock_exit:
                _trigger(app, "c-c")

        mock_cancel.assert_called_once()
        mock_exit.assert_not_called()

        app._run_task.cancel()


def test_scroll_page_up_disables_auto_scroll(app: PhosonApp) -> None:
    app._total_chat_lines = 100
    app._auto_scroll = True

    _trigger(app, "pageup")

    assert app._auto_scroll is False
    assert app._chat_scroll_top >= 0


def test_scroll_end_reenables_auto_scroll(app: PhosonApp) -> None:
    app._auto_scroll = False

    _trigger(app, "end")

    assert app._auto_scroll is True


def test_scroll_home_jumps_to_top(app: PhosonApp) -> None:
    app._auto_scroll = True

    _trigger(app, "home")

    assert app._auto_scroll is False
    assert app._chat_scroll_top == 0


def test_render_chat_shows_the_banner_on_startup(app: PhosonApp) -> None:
    """The chat pane is seeded with the welcome banner, not an empty

    placeholder (the "Type a message..." hint in ``render_chat`` is
    unreachable in the real app for this reason — it's still exercised
    directly against a bare ``FullScreenSink`` in ``test_sink_unit.py``).
    The provider/model/session/command-hint lines are NOT part of it —
    that info lives in the header instead (see
    ``test_header_shows_provider_model_and_session``), not duplicated
    in the scrollback.
    """
    text = app._render_chat().value
    assert "phoson" in text
    assert "provider" not in text
    assert "/help for commands" not in text


def test_render_chat_shows_transcript_blocks(app: PhosonApp) -> None:
    app.sink.notify("info", "hello there")
    text = app._render_chat().value
    assert "hello there" in text
    assert app._total_chat_lines >= 1


# ── Float overlays (pickers, confirmations) ───────────────────────────────────


async def test_run_float_picker_opens_and_closes_the_float(app: PhosonApp) -> None:
    from phoson_cli.pickers import BasePicker

    picker: BasePicker[str] = BasePicker(render=lambda: [("class:row", "hi\n")])
    picker.bind("enter", lambda: picker.done("chosen"))

    task = asyncio.ensure_future(app.run_float_picker(picker))
    await asyncio.sleep(0)  # let it open

    assert app._active_float is not None
    assert app._float_kb is picker._kb
    assert app._active_float in app._root_container.floats

    picker.done("chosen")
    result = await task

    assert result == "chosen"
    assert app._active_float is None
    assert app._float_kb is None


async def test_base_bindings_are_disabled_while_a_float_is_open(app: PhosonApp) -> None:
    """Regression: a Float has no independent key-binding stack — without

    gating, a picker's Enter would also trigger the chat's submit handler
    (or Ctrl+L would clear the transcript, or Ctrl+Q would exit the app)
    underneath it.
    """
    from phoson_cli.pickers import BasePicker

    app.sink.notify("info", "existing content")
    app.sink.dirty = False

    picker: BasePicker[str] = BasePicker(render=lambda: [("class:row", "hi\n")])
    task = asyncio.ensure_future(app.run_float_picker(picker))
    await asyncio.sleep(0)

    with patch.object(app.app, "exit") as mock_exit:
        fired = _trigger_if_enabled(app, "c-q")
    assert fired is False
    mock_exit.assert_not_called()

    fired = _trigger_if_enabled(app, "c-l")
    assert fired is False
    assert app.sink.blocks  # NOT cleared — the base c-l binding never fired

    picker.done(None)
    await task

    # Now that the float is closed, the base bindings work again.
    with patch.object(app.app, "exit") as mock_exit:
        fired = _trigger_if_enabled(app, "c-q")
    assert fired is True
    mock_exit.assert_called_once()


async def test_run_float_confirm_resolves_yes_and_no(app: PhosonApp) -> None:
    task = asyncio.ensure_future(app.run_float_confirm("Really?"))
    await asyncio.sleep(0)
    assert app._active_float is not None

    _trigger(app, "y")
    assert await task is True
    assert app._active_float is None

    task = asyncio.ensure_future(app.run_float_confirm("Really?"))
    await asyncio.sleep(0)
    _trigger(app, "n")
    assert await task is False


async def test_run_float_confirm_resolves_no_on_ctrl_c(app: PhosonApp) -> None:
    """Ctrl+C on an open confirmation must resolve False, not hang —

    otherwise cancelling a run mid-tool-confirmation would leave the
    awaiting tool call (and the Float) stuck forever.
    """
    task = asyncio.ensure_future(app.run_float_confirm("Run this?"))
    await asyncio.sleep(0)

    fired = _trigger_if_enabled(app, "c-c")

    assert fired is True  # the confirm Float's own c-c binding, not the base exit
    assert await task is False


# ── Ctrl+D, header indicators, banner, completer ──────────────────────────────


def test_ctrl_d_deletes_forward_on_non_empty_line(app: PhosonApp) -> None:
    buf = app._prompt_input.buffer
    buf.text = "hello"
    buf.cursor_position = 0

    _trigger(app, "c-d")

    assert buf.text == "ello"


def test_ctrl_d_on_empty_line_requests_exit(app: PhosonApp) -> None:
    app._prompt_input.text = ""

    with patch.object(app.app, "exit") as mock_exit:
        _trigger(app, "c-d")

    mock_exit.assert_called_once()


async def test_ctrl_v_attaches_an_image_from_the_clipboard(
    app: PhosonApp, tmp_path
) -> None:
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    with patch(
        "phoson_cli.fullscreen.app.read_clipboard_image",
        new=AsyncMock(return_value=(fake_png, "image/png")),
    ):
        _trigger(app, "c-v")
        await asyncio.sleep(0)

    assert len(app.repl.attachments) == 1
    assert app._prompt_input.text == "[image #1] "


async def test_ctrl_v_placeholder_inserts_at_cursor_and_numbers_multiple_pastes(
    app: PhosonApp,
) -> None:
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    with patch(
        "phoson_cli.fullscreen.app.read_clipboard_image",
        new=AsyncMock(return_value=(fake_png, "image/png")),
    ):
        app._prompt_input.text = "look at this and that"
        app._prompt_input.buffer.cursor_position = len("look at this")
        _trigger(app, "c-v")
        await asyncio.sleep(0)

        _trigger(app, "c-v")
        await asyncio.sleep(0)

    assert app._prompt_input.text == ("look at this[image #1] [image #2]  and that")
    assert len(app.repl.attachments) == 2


async def test_ctrl_v_notifies_when_clipboard_has_no_image(app: PhosonApp) -> None:
    with patch(
        "phoson_cli.fullscreen.app.read_clipboard_image",
        new=AsyncMock(return_value=None),
    ):
        _trigger(app, "c-v")
        await asyncio.sleep(0)

    assert len(app.repl.attachments) == 0
    assert "No image on the clipboard" in app._render_chat().value


def test_header_shows_token_indicator_and_attachment_count(app: PhosonApp) -> None:
    app.repl._context_window = 128_000
    app.repl._context_tokens = 12_400

    text = app._get_header_text().value

    assert "12.4k/128.0k" in text or "12.4k/128k" in text


def test_header_hides_attachment_count_when_none_pending(app: PhosonApp) -> None:
    assert "📎" not in app._get_header_text().value


def test_header_shows_provider_model_and_session(app: PhosonApp) -> None:
    """provider/model/session live in the header only — not duplicated

    in the banner (see ``test_render_chat_shows_the_banner_on_startup``).
    """
    text = app._get_header_text().value

    assert app.repl.config.provider in text
    assert app.repl.current_model in text
    assert app.repl.tree.session_id[:8] in text


def test_banner_seeds_the_transcript_on_init(app: PhosonApp) -> None:
    assert len(app.sink.blocks) == 1
    text = app._render_chat().value
    assert "phoson" in text


def test_slash_completer_only_completes_the_command_word() -> None:
    from prompt_toolkit.document import Document

    from phoson_cli.fullscreen.completer import SlashCompleter

    completer = SlashCompleter()

    completions = list(completer.get_completions(Document("/mod", 4), None))
    assert any(c.text == "/model" for c in completions)

    # Not a slash command — no completions.
    assert list(completer.get_completions(Document("hello", 5), None)) == []

    # Args already typed — the command word itself is no longer completed.
    assert list(completer.get_completions(Document("/model gpt", 10), None)) == []


async def test_escape_cancels_a_run_in_flight(app: PhosonApp) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run_agent(text: str) -> None:
        started.set()
        await release.wait()

    # Make the controller report an in-flight task so Esc takes the
    # cancel branch (in production this task is the stream consumer).
    fake_task = MagicMock()
    fake_task.done.return_value = False

    with (
        patch.object(app.repl, "_run_agent", new=slow_run_agent),
        patch.object(app.repl, "cancel_current", return_value=True) as mock_cancel,
        patch.object(
            type(app.repl),
            "current_task",
            new_callable=PropertyMock,
            return_value=fake_task,
        ),
    ):
        app._prompt_input.text = "go"
        _trigger(app, "enter")
        await started.wait()

        _trigger(app, "escape")
        mock_cancel.assert_called_once()

    release.set()
    await app._run_task


def test_escape_is_a_noop_when_idle(app: PhosonApp) -> None:
    # No run in flight: Esc must not touch the controller.
    with patch.object(app.repl, "cancel_current") as mock_cancel:
        _trigger(app, "escape")
        mock_cancel.assert_not_called()
