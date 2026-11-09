"""Composer editing keys reach the draft, fed as raw terminal bytes.

Every test here drives the real ``PhosonApp`` through prompt_toolkit's pipe
input: the bytes go through the VT100 parser, the key processor and the
app's merged key bindings — the same path a terminal's key press takes.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from phoson_cli.config import PhosonConfig
from phoson_cli.fullscreen.keys import DEFAULT_KEY_BINDINGS, listing_for_config

DRAFT = "alpha beta gamma"


def _app_for(tmp_path, **config_kwargs):
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            **config_kwargs,
        )
        from phoson_cli.fullscreen.app import PhosonApp

        return PhosonApp(config)


async def _feed(app, data: str, *, draft: str = DRAFT, cursor: int | None = None):
    """Run the app, put ``draft`` in the composer, send ``data``, then quit.

    Returns the composer buffer as it was right after ``data`` was handled.
    """
    seen: dict[str, object] = {}
    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()

        async def drive() -> None:
            await asyncio.sleep(0.02)
            buffer = app._prompt_input.buffer
            buffer.text = draft
            buffer.cursor_position = len(draft) if cursor is None else cursor
            pipe.send_text(data)
            await asyncio.sleep(0.08)
            seen["text"] = buffer.text
            seen["cursor"] = buffer.cursor_position
            app.app.exit()

        task = asyncio.create_task(drive())
        await app.app.run_async()
        await task
    return seen


# ── Word left / right ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "\x1bb",  # Option+Left, macOS Terminal / iTerm2 natural text editing
        "\x1b[1;3D",  # Alt+Left (xterm CSI modifiers)
        "\x1b[1;9D",  # Option+Left, iTerm2 (Meta)
        "\x1b\x1b[D",  # Option+Left, "Option sends Esc+" in front of the arrow
        "\x1b[1;5D",  # Ctrl+Left
        "\x1b[98;3u",  # Alt+B under the kitty keyboard protocol
    ],
)
async def test_word_left_moves_the_cursor(tmp_path, data) -> None:
    app = _app_for(tmp_path)
    seen = await _feed(app, data)
    assert seen == {"text": DRAFT, "cursor": DRAFT.index("gamma")}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    ["\x1bf", "\x1b[1;3C", "\x1b[1;9C", "\x1b\x1b[C", "\x1b[1;5C"],
)
async def test_word_right_moves_the_cursor(tmp_path, data) -> None:
    app = _app_for(tmp_path)
    seen = await _feed(app, data, cursor=0)
    assert seen["text"] == DRAFT
    # readline's forward-word stops after the word; ``escape right`` at the
    # start of the next one. Both leave the first word.
    assert seen["cursor"] in (len("alpha"), len("alpha "))


# ── Start / end of line ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "\x1b[H",  # Home
        "\x1bOH",  # Home (application mode) / a common Cmd+Left mapping
        "\x1b[1~",  # Home (linux console, tmux)
        "\x01",  # Ctrl+A — also Cmd+Left in iTerm2 natural text editing
    ],
)
async def test_start_of_line_moves_the_cursor(tmp_path, data) -> None:
    app = _app_for(tmp_path)
    app._chat_pane._auto_scroll = True
    seen = await _feed(app, data)
    assert seen == {"text": DRAFT, "cursor": 0}
    assert app._chat_pane._auto_scroll is True  # the chat did not scroll


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "\x1b[F",  # End
        "\x1bOF",  # End (application mode) / a common Cmd+Right mapping
        "\x1b[4~",  # End (linux console, tmux)
        "\x05",  # Ctrl+E — also Cmd+Right in iTerm2 natural text editing
    ],
)
async def test_end_of_line_moves_the_cursor(tmp_path, data) -> None:
    app = _app_for(tmp_path)
    app._chat_pane._auto_scroll = False
    with patch("phoson_cli.fullscreen.state_cycles.save_config") as save:
        seen = await _feed(app, data, cursor=0)
    assert seen == {"text": DRAFT, "cursor": len(DRAFT)}
    assert app._chat_pane._auto_scroll is False  # the chat did not scroll
    save.assert_not_called()  # Ctrl+E no longer changes the reasoning effort


@pytest.mark.asyncio
async def test_home_and_end_scroll_the_chat_when_the_draft_is_empty(tmp_path) -> None:
    app = _app_for(tmp_path)
    app._chat_pane._auto_scroll = True
    await _feed(app, "\x1b[H", draft="")
    assert app._chat_pane._auto_scroll is False
    assert app._chat_pane._chat_scroll_top == 0

    app = _app_for(tmp_path)
    app._chat_pane._auto_scroll = False
    await _feed(app, "\x1b[F", draft="")
    assert app._chat_pane._auto_scroll is True


@pytest.mark.asyncio
async def test_remapped_scroll_home_fires_with_a_draft(tmp_path) -> None:
    """A user remap is explicit: it is not held back by a non-empty draft."""
    app = _app_for(tmp_path, key_bindings={"scroll_home": ["c-a"]})
    app._chat_pane._auto_scroll = True
    seen = await _feed(app, "\x01")
    assert app._chat_pane._auto_scroll is False
    assert seen["cursor"] == len(DRAFT)


# ── Deleting ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "\x1b\x7f",  # Option+Backspace
        "\x1b\x08",  # Alt+Backspace where Backspace is ^H
        "\x17",  # Ctrl+W
        "\x1b[127;3u",  # Alt+Backspace under the kitty keyboard protocol
    ],
)
async def test_delete_word_backwards(tmp_path, data) -> None:
    app = _app_for(tmp_path)
    seen = await _feed(app, data)
    assert seen == {"text": "alpha beta ", "cursor": len("alpha beta ")}


@pytest.mark.asyncio
async def test_alt_d_deletes_the_next_word(tmp_path) -> None:
    app = _app_for(tmp_path)
    seen = await _feed(app, "\x1bd", cursor=0)
    assert seen == {"text": " beta gamma", "cursor": 0}


@pytest.mark.asyncio
async def test_ctrl_k_and_ctrl_u_cut_the_line(tmp_path) -> None:
    app = _app_for(tmp_path)
    seen = await _feed(app, "\x0b", cursor=len("alpha"))
    assert seen["text"] == "alpha"

    app = _app_for(tmp_path)
    seen = await _feed(app, "\x15", cursor=len("alpha "))
    assert seen["text"] == "beta gamma"


# ── Alt chords never act as Esc ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["\x1bb", "\x1bq", "\x1b\x1b[A", "\x1b[1;3A"])
async def test_alt_chord_types_nothing_and_does_not_arm_the_rewind(
    tmp_path, data
) -> None:
    """``ESC`` + key is one chord: no stray letter, no Esc side effect —
    including chords no editing binding claims (Alt+Q, Alt+Up)."""
    app = _app_for(tmp_path)
    with patch.object(app, "handle_rewind") as rewind:
        seen = await _feed(app, data + data)
    assert seen["text"] == DRAFT
    assert app._last_escape_at == 0.0
    rewind.assert_not_called()


@pytest.mark.asyncio
async def test_alt_chord_mid_run_does_not_cancel(tmp_path) -> None:
    app = _app_for(tmp_path)
    with (
        patch.object(app, "_is_run_in_flight", return_value=True),
        patch.object(app, "_cancel_operation", return_value="cancelled") as cancel,
    ):
        await _feed(app, "\x1bb\x1bq\x1b[1;3D")
    cancel.assert_not_called()


@pytest.mark.asyncio
async def test_lone_esc_mid_run_still_cancels(tmp_path) -> None:
    app = _app_for(tmp_path)
    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()
        with (
            patch.object(app, "_is_run_in_flight", return_value=True),
            patch.object(app, "_cancel_operation", return_value="cancelled") as cancel,
        ):

            async def drive() -> None:
                await asyncio.sleep(0.02)
                pipe.send_text("\x1b")
                await asyncio.sleep(0.7)  # past ptk's lone-Esc disambiguation
                app.app.exit()

            task = asyncio.create_task(drive())
            await app.app.run_async()
            await task
    cancel.assert_called_once()


@pytest.mark.asyncio
async def test_paste_read_together_with_esc_is_not_swallowed(tmp_path) -> None:
    app = _app_for(tmp_path)
    seen = await _feed(app, "\x1b\x1b[200~pasted\x1b[201~", draft="")
    assert seen["text"] == "pasted"


# ── Reasoning effort moved off Ctrl+E ─────────────────────────────────────────


def test_reasoning_effort_default_is_ctrl_r_and_unique() -> None:
    assert DEFAULT_KEY_BINDINGS["cycle_reasoning_effort"] == ["c-r"]
    owners = [a for a, seqs in DEFAULT_KEY_BINDINGS.items() if "c-r" in seqs]
    assert owners == ["cycle_reasoning_effort"]
    assert dict(listing_for_config(None))["cycle_reasoning_effort"] == "Ctrl+R"


@pytest.mark.asyncio
async def test_ctrl_r_cycles_the_reasoning_effort(tmp_path) -> None:
    app = _app_for(tmp_path)
    app.session.config.reasoning_effort = None
    with patch("phoson_cli.fullscreen.state_cycles.save_config") as save:
        seen = await _feed(app, "\x12")
    assert app.session.config.reasoning_effort == "low"
    save.assert_called_once()
    assert seen == {"text": DRAFT, "cursor": len(DRAFT)}


@pytest.mark.asyncio
async def test_explicit_ctrl_e_remap_still_cycles_the_effort(tmp_path) -> None:
    app = _app_for(tmp_path, key_bindings={"cycle_reasoning_effort": ["c-e"]})
    app.session.config.reasoning_effort = None
    with patch("phoson_cli.fullscreen.state_cycles.save_config"):
        seen = await _feed(app, "\x05", cursor=0)
    assert app.session.config.reasoning_effort == "low"
    assert seen["cursor"] == 0  # the user's remap owns the key
