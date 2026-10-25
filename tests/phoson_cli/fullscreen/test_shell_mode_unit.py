"""Shell mode (``!``) and the hanging-indent composer in the full-screen TUI."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.fullscreen.app import PhosonApp
from phoson_cli.fullscreen.header_model import _FOOTER_HINT_SHELL

pytestmark = pytest.mark.usefixtures("isolated_cli_home")


@pytest.fixture
def app(tmp_path) -> PhosonApp:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        return PhosonApp(config)


def _press(app: PhosonApp, key: str) -> bool:
    """Invoke the first *enabled* binding for ``key``; False when none is."""
    target = {"enter": "c-m"}.get(key, key)
    for binding in app.app.key_bindings.bindings:
        for k in binding.keys:
            if str(getattr(k, "value", k)).lower() == target and binding.filter():
                binding.handler(MagicMock())
                return True
    return False


def test_bang_on_empty_composer_enters_shell_mode(app: PhosonApp) -> None:
    assert _press(app, "!")
    assert app._shell_mode
    assert app._prompt_input.text == ""
    assert app._composer_line_prefix(0, 0) == [("class:composer.shell", "! ")]
    assert "shell" in app._composer_placeholder()
    assert _FOOTER_HINT_SHELL in app._get_footer_text().value


def test_bang_mid_text_is_a_plain_character(app: PhosonApp) -> None:
    app._prompt_input.text = "hello"
    assert not _press(app, "!")  # no shell binding: ptk inserts the char
    assert not app._shell_mode


def test_backspace_on_empty_composer_leaves_shell_mode(app: PhosonApp) -> None:
    app.enter_shell_mode()
    app._prompt_input.text = "ls"
    # With text, Backspace edits (the shell binding is disabled).
    for binding in app.app.key_bindings.bindings:
        if binding.handler.__name__ == "_leave_shell":
            assert not binding.filter()
    app._prompt_input.text = ""
    assert _press(app, "c-h")
    assert not app._shell_mode


def test_escape_leaves_shell_mode_and_keeps_text(app: PhosonApp) -> None:
    app.enter_shell_mode()
    app._prompt_input.text = "ls -la"
    app.handle_escape()
    assert not app._shell_mode
    assert app._prompt_input.text == "ls -la"


async def test_enter_in_shell_mode_runs_command_and_returns_to_chat(
    app: PhosonApp,
) -> None:
    app.enter_shell_mode()
    app._prompt_input.text = "git status"
    with patch.object(app, "_run_bash_line", new=AsyncMock()) as run:
        _press(app, "enter")
        await app._run_task
    run.assert_awaited_once_with("git status")
    assert not app._shell_mode
    assert app._prompt_input.text == ""
    # History keeps the ``!`` so a recalled entry still runs as shell.
    assert list(app._prompt_input.buffer.history.get_strings())[-1] == "!git status"


async def test_recalled_bang_entry_in_shell_mode_drops_the_prefix(
    app: PhosonApp,
) -> None:
    app.enter_shell_mode()
    app._prompt_input.text = "!ls"
    with patch.object(app, "_run_bash_line", new=AsyncMock()) as run:
        _press(app, "enter")
        await app._run_task
    run.assert_awaited_once_with("ls")


async def test_bash_line_output_is_queued_for_the_agent(app: PhosonApp) -> None:
    from phoson_cli.permissions_store import LEVEL_ALLOW

    policy = MagicMock()
    policy.check.return_value = LEVEL_ALLOW
    with (
        patch("phoson_cli.permissions_store.load_policy", return_value=policy),
        patch("phoson_cli.tools.bash._run_bash", new=AsyncMock(return_value="ok\n")),
    ):
        await app._run_bash_line("echo ok")

    controller = app.session.controller
    assert controller.pending_shell_count == 1
    assert "1 shell output → next message" in app._get_footer_text().value


def test_continuation_lines_hang_under_the_prompt_glyph(app: PhosonApp) -> None:
    assert app._composer_line_prefix(0, 0) == [("class:text-area.prompt", "❯ ")]
    # Wrapped continuation of line 0 and every Ctrl+J line are indented by
    # the glyph's width, so the text never runs under the ``❯``.
    assert app._composer_line_prefix(0, 1) == [("", "  ")]
    assert app._composer_line_prefix(2, 0) == [("", "  ")]


def test_completions_are_off_in_shell_mode(app: PhosonApp) -> None:
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent

    completer = app._prompt_input.completer
    doc = Document("/he", 3)
    assert list(completer.get_completions(doc, CompleteEvent()))
    app.enter_shell_mode()
    assert not list(completer.get_completions(doc, CompleteEvent()))


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b[13;2u",
        "\x1b[13;5u",
        "\x1b[27;2;13~",
        "\x1b[27;5;13~",
        "\x1b\r",
    ],
)
def test_modified_enter_decodes_as_newline(app: PhosonApp, sequence: str) -> None:
    """Shift/Ctrl/Alt+Enter reach the ``newline`` action (c-j), not submit."""
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.input.vt100_parser import Vt100Parser

    keys: list = []
    parser = Vt100Parser(lambda kp: keys.append(kp.key))
    parser.feed(sequence)
    parser.flush()
    assert keys == [Keys.ControlJ]


def test_plain_enter_and_lone_escape_are_unchanged(app: PhosonApp) -> None:
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.input.vt100_parser import Vt100Parser

    keys: list = []
    parser = Vt100Parser(lambda kp: keys.append(kp.key))
    parser.feed("\r")
    parser.feed("\x1b")
    parser.flush()
    assert keys == [Keys.ControlM, Keys.Escape]


def _decode(*chunks: str) -> list:
    from prompt_toolkit.input.vt100_parser import Vt100Parser

    presses: list = []
    parser = Vt100Parser(presses.append)
    for chunk in chunks:
        parser.feed(chunk)
    parser.flush()
    return presses


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("\x1b[27u", ["escape"]),  # Esc
        ("\x1b[99;5u", ["c-c"]),  # Ctrl+C
        ("\x1b[106;5u", ["c-j"]),  # Ctrl+J
        ("\x1b[112;5u", ["c-p"]),  # Ctrl+P
        ("\x1b[116;6u", ["c-t"]),  # Ctrl+Shift+T
        ("\x1b[32;5u", ["c-@"]),  # Ctrl+Space
        ("\x1b[9;2u", ["s-tab"]),  # Shift+Tab
        ("\x1b[98;3u", ["escape", "b"]),  # Alt+B
        ("\x1b[127;3u", ["escape", "c-h"]),  # Alt+Backspace
        ("\x1b[13;3u", ["c-j"]),  # Alt+Enter
        ("\x1b[49;5u", ["<ignore>"]),  # Ctrl+1: no legacy meaning
    ],
)
def test_kitty_keyboard_protocol_decodes_to_legacy_keys(
    app: PhosonApp, sequence: str, expected: list
) -> None:
    """With kitty's flag 1 on, every binding still sees its legacy key."""
    assert [str(getattr(p.key, "value", p.key)) for p in _decode(sequence)] == expected


def test_kitty_alt_key_escape_is_not_a_real_escape(app: PhosonApp) -> None:
    """Alt+B under the kitty protocol must not cancel a run (#108 guard)."""
    from types import SimpleNamespace

    from phoson_cli.fullscreen.escape_controller import is_prefixed_escape

    _escape, follower = _decode("\x1b[98;3u")
    assert follower.data == ""
    app.app = SimpleNamespace(key_processor=SimpleNamespace(input_queue=[follower]))
    assert is_prefixed_escape(app)


def test_kitty_keyboard_is_tied_to_the_alternate_screen() -> None:
    from phoson_cli.fullscreen.app import _tie_kitty_keyboard_to_alternate_screen
    from phoson_cli.fullscreen.keys import KITTY_KEYBOARD_POP, KITTY_KEYBOARD_PUSH

    written: list[str] = []

    class _Output:
        def write_raw(self, data: str) -> None:
            written.append(data)

        def enter_alternate_screen(self) -> None:
            written.append("<enter>")

        def quit_alternate_screen(self) -> None:
            written.append("<quit>")

    output = _Output()
    _tie_kitty_keyboard_to_alternate_screen(output)
    output.enter_alternate_screen()
    output.quit_alternate_screen()
    assert written == ["<enter>", KITTY_KEYBOARD_PUSH, KITTY_KEYBOARD_POP, "<quit>"]


def test_truecolor_terminals_get_24_bit_color(monkeypatch) -> None:
    from prompt_toolkit.output import ColorDepth

    from phoson_cli.fullscreen.app import _detect_color_depth

    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert _detect_color_depth() is ColorDepth.DEPTH_24_BIT
    monkeypatch.setenv("COLORTERM", "")
    assert _detect_color_depth() is None
    monkeypatch.setenv("COLORTERM", "24bit")
    monkeypatch.setenv("PROMPT_TOOLKIT_COLOR_DEPTH", "DEPTH_8_BIT")
    assert _detect_color_depth() is None  # explicit override wins (ptk reads it)


def test_welcome_screen_stays_anchored_at_the_top(app: PhosonApp) -> None:
    """On a pane shorter than the welcome screen the banner stays in view."""
    pane = app._chat_pane
    app._render_chat()
    pane._total_chat_lines = 500
    assert pane.get_effective_scroll() == 0
    app.sink.blocks.append("a block")
    assert pane.get_effective_scroll() > 0
