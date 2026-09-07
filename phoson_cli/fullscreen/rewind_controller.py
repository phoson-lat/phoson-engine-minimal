"""Rewind / undo-jump (double-Esc) for the full-screen front end (#187).

Extracted from ``app.py`` to keep ``PhosonApp`` focused on layout, scroll and
lifecycle.  The rewind/undo logic is unchanged — this is a move, not a
rewrite.

State (``_rewind_stack``) stays on the owning ``PhosonApp``: the test suite
asserts on ``app._rewind_stack`` directly, and a fresh/reset session clears it.
The controller reads and writes that state through ``app``.

``PhosonApp.handle_rewind`` / ``undo_jump`` / ``_apply_rewind`` /
``_reset_transcript`` remain thin delegates so the ``keys.py`` name lookups and
the test suite are untouched.
"""

from ..controller import MAX_RESUME_REPLAY_MESSAGES


def one_line(text: str) -> str:
    """Collapse whitespace to a single line (rewind notices/previews)."""
    return " ".join(text.split())


class RewindController:
    """Owns the double-Esc rewind and Ctrl+Z undo-jump.

    References the owning ``PhosonApp`` (``app``) for ``repl`` / ``sink`` and
    the ``_rewind_stack`` state.  See the module docstring for why the state
    lives on the app rather than here.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def handle_rewind(self) -> None:
        """Double-Esc (idle): pick an earlier user message and rewind (G1).

        The picker lists the user turns of the active path; selecting one
        lands the cursor on the node *before* it (Claude Code's UX), the
        previous cursor is pushed on the undo stack (``undo_jump``,
        default Ctrl+Z, restores it), and the chat pane is redrawn from
        the tree up to the new cursor. The discarded messages are not
        deleted — they remain as an abandoned branch (visible via
        ``/tree``), and the composer is pre-filled with the selected
        turn's text so editing and resending is one Enter away.
        """
        app = self.app
        if app._active_float is not None or app._is_run_in_flight():
            return
        candidates = app.repl.jump_candidates()
        if not candidates:
            app.sink.notify("info", "Nothing to rewind to in this session.")
            return

        from ..rewind_picker import build_rewind_picker

        picker = build_rewind_picker(
            candidates, theme=app.theme, invalidate=app.app.invalidate
        )
        result = await app.run_float_picker(picker)
        if result.cancelled or result.node_id is None:
            return
        await self.apply_rewind(result.node_id)

    async def apply_rewind(self, user_node_id: str) -> None:
        """Rewind to just before ``user_node_id`` and redraw the pane.

        Each rewind pushes the previous cursor onto ``_rewind_stack``
        (``undo_jump`` pops them back in reverse order), so consecutive
        rewinds are individually restorable with repeated Ctrl+Z.
        """
        app = self.app
        previous_cursor = app.repl.current_node_id
        ok, info = app.repl.jump_to_user_turn(user_node_id)
        if not ok:
            app.sink.notify("error", str(info))
            return

        # The pre-rewind cursor is restorable (undo_jump) as long as the
        # session doesn't change underneath (a new/load/compact replaces
        # the tree, after which a stale node id is rejected by
        # ``jump_to_node`` and the stack entry is dropped).
        if previous_cursor is not None:
            app._rewind_stack.append(previous_cursor)
        try:
            path = app.repl.tree.get_path(app.repl.current_node_id)
        except (ValueError, AttributeError, TypeError):
            app.sink.notify("error", "Could not redraw after rewind — cursor lost.")
            return

        self.reset_transcript()
        if len(path) > MAX_RESUME_REPLAY_MESSAGES:
            app.sink.print_history(path, tail=MAX_RESUME_REPLAY_MESSAGES)
        else:
            app.sink.print_history(path)
        app.repl._context_tokens = app.repl._controller.estimate_active_path()
        app._auto_scroll = True
        app._chat_scroll_top = 0
        app.app.invalidate()

        # Re-populate the composer with the turn being rewound — editing
        # it and resending replaces the abandoned branch in one Enter.
        turn_text = app.repl.message_text(user_node_id).strip()
        if turn_text:
            app._prompt_input.text = turn_text

        app.sink.notify(
            "info",
            f"Rewound to just before “{one_line(turn_text)[:40]}” "
            f"(cursor → {info[:8]}) — Ctrl+Z to restore, "
            "edit and Enter to re-send.",
        )

    def undo_jump(self) -> None:
        """Ctrl+Z: undo the last rewind jump (G1) and redraw to that point.

        Pops the pre-rewind cursor pushed by ``apply_rewind`` (the
        session's continuation point) and redraws the transcript up to
        it. A plain Esc is *not* used: it would race the single-Esc run
        cancel (#68) and the picker overlays' own Esc. The binding is
        remappable (``undo_jump`` in ``[keys]``); the composer's buffer
        ignores Ctrl+Z, so the key is free.
        """
        app = self.app
        if app._active_float is not None or app._is_run_in_flight():
            return
        if not app._rewind_stack:
            app.sink.notify("info", "No rewind to undo.")
            return
        cursor = app._rewind_stack.pop()
        ok, info = app.repl.jump_to_node(cursor)
        if not ok:
            # The tree changed underneath (new/resume/compact replaced
            # the session): every remaining stack entry is stale too, so
            # drop the whole stack instead of warning per entry.
            app._rewind_stack = []
            app.sink.notify("warn", str(info))
            return
        try:
            path = app.repl.tree.get_path(cursor)
        except (ValueError, AttributeError, TypeError):
            app.sink.notify("error", "Could not redraw — the node no longer exists.")
            app._rewind_stack = []
            return
        self.reset_transcript()
        if len(path) > MAX_RESUME_REPLAY_MESSAGES:
            app.sink.print_history(path, tail=MAX_RESUME_REPLAY_MESSAGES)
        else:
            app.sink.print_history(path)
        app.repl._context_tokens = app.repl._controller.estimate_active_path()
        app._auto_scroll = True
        app._chat_scroll_top = 0
        app.app.invalidate()
        app.sink.notify(
            "info",
            f"Back to the previous point (cursor → {info[:8]})."
            + (
                f" Ctrl+Z again — {len(app._rewind_stack)} more rewind(s) on the stack."
                if app._rewind_stack
                else ""
            ),
        )

    def reset_transcript(self) -> None:
        """Drop the transcript and its ANSI cache.

        Rewind/undo-redraws (G1) rebuild the pane from the tree; the
        immutable-block cache must be dropped too, otherwise old
        (discarded) block ids could shadow freshly rendered ones.
        """
        app = self.app
        app.sink.blocks.clear()
        app.sink.drop_error_notice()
        app._block_ansi_cache.clear(0)
        app._block_ft_cache.clear(0)
        app._banner_block = None
        app.sink.dirty = True
        app.app.invalidate()
