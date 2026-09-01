"""Unit tests for the double-Esc rewind feature (IMPROVEMENTS.md G1).

Covers:
1. The controller primitives (``jump_candidates`` / ``jump_to_user_turn`` /
   ``jump_to_node``) — the generalization of ``undo_last_turn``.
2. The key map wiring (new ``rewind``/``undo_jump`` actions, remap/unbind,
   the fixed double-Esc chord metadata).
3. ``PhosonApp`` state: the double-tap detection window, the rewind
   apply/redraw path, and ``undo_jump`` (Ctrl+Z).
4. Real keystroke routing through PipeInput (Esc Esc opens the picker,
   a lone idle Esc does not, a single Esc mid-run still cancels).
"""

import time
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from phoson_cli.config import (
    KNOWN_KEY_ACTIONS,
    PhosonConfig,
    PhosonKeyBindingsError,
    load_key_bindings,
)
from phoson_llm.schemas import Message
from phoson_cli.fullscreen.keys import (
    DEFAULT_KEY_BINDINGS,
    listing_for_config,
    resolve_key_bindings,
)


def _make_repl(tmp_path, *texts: str):
    """A PhosonRepl whose tree is a linear chain of the given user turns.

    Each turn appends a user node + one assistant reply node, so the
    path alternates user/assistant like a real conversation.
    """
    from phoson_cli.repl import PhosonRepl

    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        repl = PhosonRepl(config)

    for i, text in enumerate(texts):
        user_node, _ = repl._append_user_turn(Message(role="user", content=text))
        assistant_node = repl.tree.append(
            user_node, Message(role="assistant", content=f"reply {i}")
        )
        repl.current_node_id = assistant_node.id
    return repl


def _app_for(tmp_path, **config_kwargs):
    """A bare PhosonApp (mocked chat client) with optional config extras."""
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


def _user_node(repl, text: str):
    """The node id of the user message with exactly this content.

    Searches the whole tree (not just the active path) so it still works
    after a jump has moved the cursor and the original node became part
    of an abandoned branch.
    """
    return next(
        node.id
        for node in repl.tree.nodes.values()
        if node.message.role == "user" and node.message.content == text
    )


def _landing_before(repl, text: str):
    """The node the cursor lands on when rewinding to ``text`` (its parent)."""
    return repl.tree.nodes[_user_node(repl, text)].parent_id


# ── Controller primitives ────────────────────────────────────────────────────


def test_jump_candidates_lists_user_turns_newest_first(tmp_path) -> None:
    repl = _make_repl(tmp_path, "first", "second", "third")
    candidates = repl._controller.jump_candidates()
    # Newest first (issue #109): the most recent turn is the head of the
    # list, so the picker's initial cursor sits on the latest turn.
    assert [preview for _, preview in candidates] == ["third", "second"]
    # The root user turn is excluded (nothing to land on before it).
    for node_id, _ in candidates:
        node = repl.tree.nodes[node_id]
        assert node.message.role == "user"
        assert node.parent_id is not None


def test_jump_candidates_empty_when_nothing_to_rewind_to(tmp_path) -> None:
    repl = _make_repl(tmp_path, "first")
    assert repl._controller.jump_candidates() == []


def test_jump_candidates_excludes_tool_result_nodes(tmp_path) -> None:
    """Issue #109: tool results are stored with role "user" (content =
    [ToolResultBlock]); they must NOT show up as "(empty message)" rows.
    """
    from phoson_llm.schemas import ToolResultBlock

    repl = _make_repl(tmp_path, "first", "second")
    controller = repl._controller

    # Simulate what _tool_runner does: append a user-role node whose
    # content is only a ToolResultBlock (no TextBlock).
    tool_result_node = repl.tree.append(
        repl.current_node_id,
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_call_id="call_1", result="42"),
            ],
        ),
    )
    repl.current_node_id = tool_result_node.id
    # And one more genuine user turn after the tool round-trip.
    user3_id, _ = repl._append_user_turn(Message(role="user", content="third"))
    reply3 = repl.tree.append(user3_id, Message(role="assistant", content="reply 3"))
    repl.current_node_id = reply3.id

    candidates = controller.jump_candidates()
    previews = [preview for _, preview in candidates]
    # Newest first, and NO "(empty message)" row for the tool result.
    assert previews == ["third", "second"]
    assert "(empty message)" not in previews
    assert tool_result_node.id not in [node_id for node_id, _ in candidates]


def test_jump_candidates_keeps_user_turn_with_empty_text(tmp_path) -> None:
    """A genuine user turn with whitespace-only string content still
    qualifies (it is a real user turn — the preview may be empty, but
    the node is not a tool result)."""
    repl = _make_repl(tmp_path, "first")
    empty_user = repl.tree.append(
        repl.current_node_id, Message(role="user", content="   ")
    )
    assistant_node = repl.tree.append(
        empty_user.id, Message(role="assistant", content="ok")
    )
    repl.current_node_id = assistant_node.id

    candidates = repl._controller.jump_candidates()
    # "first" is the root (excluded); the whitespace turn is the only
    # non-root genuine user turn.
    assert [preview for _, preview in candidates] == ["(empty message)"]
    assert candidates[0][0] == empty_user.id


def test_jump_to_user_turn_lands_before_the_selected_turn(tmp_path) -> None:
    repl = _make_repl(tmp_path, "first", "second", "third")
    controller = repl._controller
    second_id = _user_node(repl, "second")

    cursor_before = repl.current_node_id
    ok, landed = controller.jump_to_user_turn(second_id)

    assert ok is True
    # Landing point: turn one's assistant reply — everything from
    # "second" onward is now an abandoned branch.
    assert landed != cursor_before
    path = repl.tree.get_path(landed)
    assert [m.content for m in path if m.role == "user"] == ["first"]
    # The abandoned branch is still in the tree.
    assert second_id in repl.tree.nodes


def test_jump_to_user_turn_rejects_off_path_and_non_user(tmp_path) -> None:
    repl = _make_repl(tmp_path, "first", "second")
    controller = repl._controller

    # Off-path node: a side branch off the "first" user node — it is a
    # child (so it has a parent) but not on the active path.
    first_id = _user_node(repl, "first")
    side = repl.tree.append(first_id, Message(role="user", content="side branch"))
    ok, msg = controller.jump_to_user_turn(side.id)
    assert ok is False
    assert "not on the active path" in msg

    # Assistant node: not rewound-to (user turns only).
    assistant_node = next(
        n for n in controller._node_path() if n.message.role == "assistant"
    )
    ok, msg = controller.jump_to_user_turn(assistant_node.id)
    assert ok is False
    assert "Only user turns" in msg

    # Root user node: nothing before it.
    root_user = next(
        n
        for n in controller._node_path()
        if n.message.role == "user" and n.parent_id is None
    )
    ok, msg = controller.jump_to_user_turn(root_user.id)
    assert ok is False
    assert "session starts with this turn" in msg


def test_jump_to_node_restores_a_forward_cursor(tmp_path) -> None:
    """``undo_jump`` uses jump_to_node to move *forward* again."""
    repl = _make_repl(tmp_path, "first", "second")
    controller = repl._controller
    leaf = repl.current_node_id
    ok, landed = controller.jump_to_user_turn(_user_node(repl, "second"))
    assert ok
    assert repl.current_node_id != leaf

    ok, landed = controller.jump_to_node(leaf)
    assert ok is True
    assert landed == leaf
    assert repl.current_node_id == leaf


def test_jump_to_node_rejects_unknown_id(tmp_path) -> None:
    repl = _make_repl(tmp_path, "first")
    ok, msg = repl._controller.jump_to_node("deadbeef")
    assert ok is False
    assert "Unknown node" in msg


def test_rewound_next_turn_branches_from_the_landing_point(tmp_path) -> None:
    """The user contract: re-sending after a rewind starts a new branch."""
    repl = _make_repl(tmp_path, "first", "second")
    second_node_id = _user_node(repl, "second")
    ok, landed = repl._controller.jump_to_user_turn(second_node_id)
    assert ok

    repl._append_user_turn(Message(role="user", content="second (retry)"))
    new_leaf = repl.current_node_id
    assert new_leaf != second_node_id
    # Both "second" and "second (retry)" hang off the same parent now.
    assert repl.tree.nodes[new_leaf].parent_id == landed
    assert repl.tree.nodes[second_node_id].parent_id == landed


# ── Key map wiring (E6-style table) ──────────────────────────────────────────


def test_default_key_map_has_undo_jump() -> None:
    assert DEFAULT_KEY_BINDINGS["undo_jump"] == ["c-z"]
    # The double-Esc rewind is NOT a table action: it rides on the
    # ``escape`` action (remapping ``escape`` moves single-Esc cancel and
    # double-Esc rewind together; unbinding it disables both).
    assert "rewind" not in DEFAULT_KEY_BINDINGS
    assert set(DEFAULT_KEY_BINDINGS) == set(KNOWN_KEY_ACTIONS)


def test_listing_shows_undo_jump() -> None:
    display = dict(listing_for_config(None))
    assert display["undo_jump"] == "Ctrl+Z"
    assert "rewind" not in display


def test_rewind_is_not_a_real_chord_binding(tmp_path) -> None:
    """The double-Esc is detected by the app, not registered as a chord."""
    app = _app_for(tmp_path)
    keys = [
        tuple(getattr(k, "value", str(k)) for k in b.keys)
        for b in app.app.key_bindings.bindings
    ]
    assert ("escape", "escape") not in keys  # no chord binding
    assert ("c-z",) in keys  # undo_jump is a real binding
    assert ("escape",) in keys  # single escape stays bound (eager)


def test_undo_jump_remap_works_like_any_action(tmp_path) -> None:
    resolved = resolve_key_bindings(overrides={"undo_jump": ["f12"]})
    assert resolved["undo_jump"] == ["f12"]
    display = dict(
        listing_for_config(PhosonConfig(key_bindings={"undo_jump": ["f12"]}))
    )
    assert display["undo_jump"] == "F12"


def test_rewind_is_not_a_config_action(monkeypatch, tmp_path) -> None:
    """``rewind`` is not a [keys] action (the double-tap rides on escape)."""
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    path = home / ".phoson" / "config.toml"
    path.write_text(
        '[defaults]\n\n[keys]\nrewind = "escape escape"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(PhosonKeyBindingsError, match="Unknown key action 'rewind'"):
        load_key_bindings(path)


# ── PhosonApp: double-tap detection ──────────────────────────────────────────


def test_lone_idle_esc_arms_but_does_not_rewind(tmp_path) -> None:
    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first", "second")
    blocks_before = len(app.sink.blocks)

    app.handle_escape()  # first press: arms the window
    assert len(app.sink.blocks) == blocks_before  # no picker, no noise
    assert app._last_escape_at > 0


async def test_double_idle_esc_opens_the_rewind_picker(tmp_path) -> None:
    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first", "second", "third")

    app._last_escape_at = time.monotonic()  # a press just now (<1.0 s)
    opened: dict[str, object] = {}

    async def fake_picker(picker):
        opened["picker"] = picker
        from phoson_cli.rewind_picker import RewindPickerResult

        return RewindPickerResult(cancelled=True)

    with patch.object(app, "run_float_picker", new=fake_picker):
        app.handle_escape()  # second press within the window
        await asyncio.sleep(0.05)

    assert "picker" in opened
    assert app._last_escape_at == 0.0  # window consumed
    # The picker was built with the real candidates (2 user turns).
    picker = opened["picker"]
    rendered = picker._render()
    assert any("second" in line for _, line in rendered)


async def test_rewind_outside_window_does_not_open_picker(tmp_path) -> None:
    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first", "second")
    app._last_escape_at = time.monotonic() - 10.0  # stale press

    with patch.object(app, "run_float_picker") as mock_pick:
        app.handle_escape()
        await asyncio.sleep(0.01)
    mock_pick.assert_not_called()
    # A stale press just re-arms the window.
    assert app._last_escape_at > 0


async def test_mid_run_esc_still_cancels_and_never_rewinds(tmp_path) -> None:
    """#68 precedence: in flight, Esc cancels immediately (no double-tap)."""
    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first")

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(text: str) -> None:
        started.set()
        await release.wait()

    with (
        patch.object(app.repl, "_run_agent", new=slow_run),
        patch.object(app.repl, "cancel_current", return_value=True) as mock_cancel,
    ):
        app._prompt_input.text = "go"
        app.submit()
        await started.wait()
        assert app._is_run_in_flight()

        # Even with an armed double-tap window, the in-flight Esc must
        # cancel the run, not open the picker.
        app._last_escape_at = time.monotonic()
        with patch.object(app, "run_float_picker") as mock_pick:
            app.handle_escape()
        mock_cancel.assert_called_once()
        mock_pick.assert_not_called()

        release.set()
        if app._run_task is not None:
            app._run_task.cancel()
            # _run_turn swallows the CancelledError (its contract), so the
            # background task settles normally.
            try:
                await app._run_task
            except asyncio.CancelledError:
                pass


async def test_rewind_ignored_while_a_float_is_open(tmp_path) -> None:
    """A second Esc must not rewind on top of another overlay."""
    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first", "second")
    app._active_float = object()  # simulate an open overlay

    with patch.object(app, "run_float_picker") as mock_pick:
        await app.handle_rewind()
    mock_pick.assert_not_called()


# ── PhosonApp: apply rewind / undo jump ──────────────────────────────────────


async def test_apply_rewind_redraws_and_prepopulates(tmp_path) -> None:
    app = _app_for(tmp_path)
    repl = _make_repl(tmp_path, "first", "second", "third")
    app.repl = repl
    previous_leaf = repl.current_node_id

    # Seed some transcript blocks (as a live turn would).
    app.sink.on_user_message("first", Message(role="user", content="first"))
    # The header uses the conservative request estimate (I-91): messages
    # + system prompt + tool schemas. Compute the expected values the same
    # way the app does so the assertion matches what it actually writes.
    full_path_tokens = repl._controller.estimate_active_path()

    await app._apply_rewind(_user_node(repl, "second"))

    # Cursor moved back; the previous point is on the undo stack.
    assert app._rewind_stack == [previous_leaf]
    # The pane was rebuilt: banner + one history replay (not the old blocks).
    rendered = app._render_chat().value
    assert "session history" in rendered
    assert "reply 0" in rendered  # first turn's reply is still shown
    assert "reply 1" not in rendered  # rewound turn's reply is gone
    assert "reply 2" not in rendered  # and everything after it
    # Composer pre-populated with the selected turn's text.
    assert app._prompt_input.text == "second"
    # Header token count reflects the shorter path (same estimator).
    shorter_path_tokens = repl._controller.estimate_active_path()
    assert shorter_path_tokens < full_path_tokens
    assert repl._context_tokens == shorter_path_tokens


async def test_apply_rewind_rejects_bad_node(tmp_path) -> None:
    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first")
    blocks_before = len(app.sink.blocks)
    await app._apply_rewind("deadbeef")
    # One error notice; no redraw, no undo stack entry.
    assert len(app.sink.blocks) == blocks_before + 1
    assert app._rewind_stack == []
    assert app._prompt_input.text == ""


async def test_undo_jump_restores_the_previous_point(tmp_path) -> None:
    app = _app_for(tmp_path)
    repl = _make_repl(tmp_path, "first", "second")
    app.repl = repl
    leaf = repl.current_node_id

    await app._apply_rewind(_user_node(repl, "second"))
    assert repl.current_node_id != leaf

    app.undo_jump()

    assert repl.current_node_id == leaf
    assert app._rewind_stack == []
    rendered = app._render_chat().value
    assert "second" in rendered  # the full path is back


async def test_consecutive_rewinds_are_individually_reversible(tmp_path) -> None:
    app = _app_for(tmp_path)
    repl = _make_repl(tmp_path, "first", "second", "third", "fourth")
    app.repl = repl
    leaf = repl.current_node_id

    # Rewind A: to just before "third" → lands on "second"'s reply.
    await app._apply_rewind(_user_node(repl, "third"))
    assert app._rewind_stack == [leaf]
    assert repl.current_node_id == _landing_before(repl, "third")

    # Rewind B: from the shorter path, to just before "second".
    await app._apply_rewind(_user_node(repl, "second"))
    assert app._rewind_stack == [leaf, _landing_before(repl, "third")]
    assert repl.current_node_id == _landing_before(repl, "second")

    # Ctrl+Z unwinds in reverse order.
    app.undo_jump()
    assert repl.current_node_id == _landing_before(repl, "third")
    app.undo_jump()
    assert repl.current_node_id == leaf
    assert app._rewind_stack == []


async def test_undo_jump_without_rewind_is_a_notice(tmp_path) -> None:
    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first")
    blocks_before = len(app.sink.blocks)
    app.undo_jump()
    assert len(app.sink.blocks) == blocks_before + 1  # one notice


def test_reset_transcript_clears_and_drops_cache(tmp_path) -> None:
    app = _app_for(tmp_path)
    app.sink.blocks.append("stale block")
    # Fill the block cache so the reset must drop it.
    app._block_ansi_cache.get_or_render("stale block", 80)

    app._reset_transcript()

    # T-1: the transcript is fully cleared and no banner is re-seeded.
    assert app.sink.blocks == []
    assert app._banner_block is None
    assert app._block_ansi_cache._entries == {}


# ── Real keystroke routing (PipeInput) ───────────────────────────────────────
# These tests send raw bytes through the real VT100 input layer, which holds
# each *lone* Esc for ``ttimeoutlen`` (0.5 s) to disambiguate it from the
# start of an escape sequence. Two consequences make the asserts robust
# without needing to wait ~0.5 s per key:
#   * The pipe is FIFO, so the later-sent Ctrl+C is always read (and its
#     ``exit()`` flag set) only after both Escs have been read and handled.
#   * ``exit()``'s flag is honored at the top of the input loop, so the run
#     ends only once the pending Esc deliveries are done — a second Esc that
#     still had disambiguation pending is always processed before the exit
#     takes effect.


@pytest.mark.asyncio
async def test_pipe_input_esc_esc_idle_opens_rewind_picker(tmp_path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from phoson_cli.rewind_picker import RewindPickerResult

    app = _app_for(tmp_path)
    repl = _make_repl(tmp_path, "first", "second", "third")
    app.repl = repl

    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()

        seen: list = []

        async def fake_picker(picker):
            seen.append(picker)
            return RewindPickerResult(cancelled=True)

        with patch.object(app, "run_float_picker", new=fake_picker):

            async def drive():
                await asyncio.sleep(0.02)
                pipe.send_text("\x1b")  # first Esc — arms the window
                await asyncio.sleep(0.05)
                pipe.send_text("\x1b")  # second Esc — rewind
                await asyncio.sleep(0.2)
                pipe.send_text("\x03")  # exit

            asyncio.create_task(drive())
            await app.app.run_async()
    assert len(seen) == 1  # the picker opened exactly once


@pytest.mark.asyncio
async def test_pipe_input_lone_esc_idle_does_nothing(tmp_path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first", "second")
    blocks_before = len(app.sink.blocks)

    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()

        with patch.object(app, "run_float_picker") as mock_pick:

            async def drive():
                await asyncio.sleep(0.02)
                pipe.send_text("\x1b")  # lone idle Esc
                await asyncio.sleep(0.3)
                pipe.send_text("\x03")  # exit

            asyncio.create_task(drive())
            await app.app.run_async()

        mock_pick.assert_not_called()
        assert len(app.sink.blocks) == blocks_before


@pytest.mark.asyncio
async def test_pipe_input_ctrl_z_without_rewind_notifies(tmp_path) -> None:
    """Ctrl+Z is inert (one notice) when nothing was rewound."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first")
    blocks_before = len(app.sink.blocks)

    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()

        async def drive():
            await asyncio.sleep(0.02)
            pipe.send_text("\x1a")  # Ctrl+Z
            await asyncio.sleep(0.1)
            pipe.send_text("\x03")  # exit

        asyncio.create_task(drive())
        await app.app.run_async()

    assert len(app.sink.blocks) == blocks_before + 1  # one notice


# ── Issue #108: Alt+<key> must not be read as Esc ─────────────────────────────
# Many terminals encode Alt+<key> as ESC + <key> (Meta convention). For
# Alt+Backspace the bytes are ``0x1b 0x7f``; the VT100 parser emits them as
# ``escape`` + ``c-h`` in the SAME input batch, and the eager ``escape``
# handler fires for the first key while ``c-h`` is still in the queue.
# ``PhosonApp._is_prefixed_escape`` uses exactly that signal to tell a
# deliberate Esc from a sequence prefix.


@pytest.mark.asyncio
async def test_pipe_input_alt_backspace_idle_does_not_rewind(tmp_path) -> None:
    """ESC+0x7f (Alt+Backspace) must NOT open the rewind picker (issue #108)."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first", "second")
    blocks_before = len(app.sink.blocks)

    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()

        with patch.object(app, "run_float_picker") as mock_pick:

            async def drive():
                await asyncio.sleep(0.02)
                pipe.send_text("\x1b\x7f")  # Alt+Backspace (ESC + DEL)
                await asyncio.sleep(0.05)
                pipe.send_text("\x1b\x7f")  # second Alt+Backspace
                await asyncio.sleep(0.3)
                pipe.send_text("\x03")  # exit

            asyncio.create_task(drive())
            await app.app.run_async()

        mock_pick.assert_not_called()
        # No notification noise either (the prefix Esc is fully ignored).
        assert len(app.sink.blocks) == blocks_before


@pytest.mark.asyncio
async def test_pipe_input_alt_backspace_mid_run_does_not_cancel(tmp_path) -> None:
    """ESC+0x7f while a run is in flight must NOT cancel it (issue #108,
    the regression that made the bug critical)."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first")

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(text: str) -> None:
        started.set()
        await release.wait()

    with (
        patch.object(app.repl, "_run_agent", new=slow_run),
        patch.object(app.repl, "cancel_current", return_value=True) as mock_cancel,
        create_pipe_input() as pipe,
    ):
        app.app.input = pipe
        app.app.output = DummyOutput()

        async def drive():
            await asyncio.sleep(0.02)
            app._prompt_input.text = "go"
            app.submit()
            await started.wait()
            pipe.send_text("\x1b\x7f")  # Alt+Backspace mid-run
            await asyncio.sleep(0.2)
            release.set()
            await asyncio.sleep(0.2)
            pipe.send_text("\x03")  # exit

        asyncio.create_task(drive())
        await app.app.run_async()

    mock_cancel.assert_not_called()  # the Alt+Backspace prefix did NOT cancel


@pytest.mark.asyncio
async def test_pipe_input_clean_esc_still_cancels_mid_run(tmp_path) -> None:
    """Regression guard (#68 intact): a *clean* Esc (nothing after it in
    the queue) still cancels an in-flight run immediately."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first")

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(text: str) -> None:
        started.set()
        await release.wait()

    with (
        patch.object(app.repl, "_run_agent", new=slow_run),
        patch.object(app.repl, "cancel_current", return_value=True) as mock_cancel,
        create_pipe_input() as pipe,
    ):
        app.app.input = pipe
        app.app.output = DummyOutput()

        async def drive():
            await asyncio.sleep(0.02)
            app._prompt_input.text = "go"
            app.submit()
            await started.wait()
            pipe.send_text("\x1b")  # clean Esc (alone in its batch)
            # A lone Esc via the pipe is held by the VT100 layer for
            # ttimeoutlen (0.5 s) before delivery — wait past that so
            # the cancel is observed while the run is still in flight.
            await asyncio.sleep(0.65)
            release.set()
            await asyncio.sleep(0.2)
            pipe.send_text("\x03")  # exit

        asyncio.create_task(drive())
        await app.app.run_async()

    mock_cancel.assert_called_once()


@pytest.mark.asyncio
async def test_pipe_input_alt_x_idle_does_not_rewind(tmp_path) -> None:
    """Any Alt-modified key (ESC + printable, here Alt+X) must behave the
    same as Alt+Backspace: no cancel, no rewind (issue #108 generalizes
    beyond Backspace)."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    app = _app_for(tmp_path)
    app.repl = _make_repl(tmp_path, "first", "second")

    with create_pipe_input() as pipe:
        app.app.input = pipe
        app.app.output = DummyOutput()

        with patch.object(app, "run_float_picker") as mock_pick:

            async def drive():
                await asyncio.sleep(0.02)
                pipe.send_text("\x1bx")  # Alt+X (ESC + x)
                await asyncio.sleep(0.05)
                pipe.send_text("\x1bx")  # second Alt+X
                await asyncio.sleep(0.3)
                pipe.send_text("\x03")  # exit

            asyncio.create_task(drive())
            await app.app.run_async()

        mock_pick.assert_not_called()


def test_is_prefixed_escape_uses_the_input_queue(tmp_path) -> None:
    """Unit-level check of the prefix signal: a queued Meta-encoded key
    (printable data) means the Esc was an Alt+<key> prefix; an empty
    queue or a queued control-char key means a clean/deliberate Esc."""
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.key_binding.key_processor import KeyPress

    app = _app_for(tmp_path)
    # Fresh app: no queue → not prefixed.
    assert app._is_prefixed_escape() is False

    # Simulate Alt+Backspace: c-h with data \x7f (0x7F, in printable range).
    app.app.key_processor.input_queue.append(KeyPress(Keys.ControlH, "\x7f"))
    assert app._is_prefixed_escape() is True

    # Simulate Alt+X: plain 'x' with data 'x' (0x78).
    app.app.key_processor.input_queue.clear()
    app.app.key_processor.input_queue.append(KeyPress("x", "x"))
    assert app._is_prefixed_escape() is True

    # Ctrl+C in queue (data \x03, below 0x20) → NOT prefixed.
    app.app.key_processor.input_queue.clear()
    app.app.key_processor.input_queue.append(KeyPress(Keys.ControlC, "\x03"))
    assert app._is_prefixed_escape() is False

    # Another Esc in queue (data \x1b, below 0x20) → NOT prefixed
    # (this is the normal double-Esc case).
    app.app.key_processor.input_queue.clear()
    app.app.key_processor.input_queue.append(KeyPress(Keys.Escape, "\x1b"))
    assert app._is_prefixed_escape() is False

    # The internal _Flush sentinel alone is NOT a real key → not prefixed.
    app.app.key_processor.input_queue.clear()
    from prompt_toolkit.key_binding.key_processor import _Flush

    app.app.key_processor.input_queue.append(_Flush)
    assert app._is_prefixed_escape() is False
