"""Turn submission + dispatch for the full-screen front end (#187).

Extracted from ``app.py``: Enter-key submission, the command/agent-turn
dispatch, and the transient activity-indicator ticker that animates the
in-chat spinner while a turn is running. Kept as thin delegates on
``PhosonApp`` so the ``keys.py`` name lookups and the test suite keep
working.
"""

import os
import time
import asyncio
import logging
from typing import Any
from pathlib import Path

from ..commands import Command, parse_command, is_run_safe_command

_PERF_LOGGER = logging.getLogger("phoson_cli.fullscreen.perf")

# How often the subagent panel animation frame advances while active.
# Kept at 0.12 s (I-84): 0.2 s made the braille spinner visibly lag
# (2 s/rotation vs 1.2 s). The streaming freeze in
# `tick_activity_frame()` — not the tick rate — is what cuts CPU.
SUBAGENT_TICK_SECONDS = 0.12


def submit(app: Any) -> None:
    """Handle Enter on the input line: dispatch a command or an agent turn.

    While a turn is already in flight the composer stays usable:
    - a **run-safe** slash command (local UI/config or read-only info, see
      :func:`phoson_cli.commands.is_run_safe_command`) runs immediately, side
      by side with the turn, through the concurrent-operation path;
    - any other slash command and ``!`` bash lines are refused with a notice
      and the draft is *kept* (they compete with the running turn);
    - a **plain message is queued** and sent automatically when the current
      turn settles, so the user can line up several messages without waiting.

    The idle path is unchanged. The custom submit path bypasses the buffer's
    ``accept_handler`` (which normally persists history), so history is
    written explicitly (IMPROVEMENTS.md A2).
    """
    text = app._prompt_input.text
    if not text.strip():
        return
    if app._is_run_in_flight():
        _submit_while_running(app, text)
        return
    app._prompt_input.buffer.append_to_history()
    app._prompt_input.text = ""
    app._auto_scroll = True
    # T-12: a leading "!" (with the rest non-blank) is a shell command,
    # not an agent turn or a slash command.
    if text.startswith("!") and text[1:].strip():
        app._start_operation(app._run_bash_line(text[1:].strip()), "bash")
        return
    app._start_operation(app._dispatch(text), "input")


def _submit_while_running(app: Any, text: str) -> None:
    """Enter during an in-flight turn: run-safe command, refuse, or queue."""
    cmd = parse_command(text)
    if cmd is not None:
        if is_run_safe_command(cmd.name):
            app._prompt_input.buffer.append_to_history()
            app._prompt_input.text = ""
            app._auto_scroll = True
            app._start_concurrent_operation(app._run_command(cmd), "command")
            return
        app.sink.notify(
            "warn",
            f"{cmd.name} is not available while a turn is running — "
            "press Esc to cancel it first. Your text is kept.",
        )
        return
    if text.startswith("!") and text[1:].strip():
        app.sink.notify(
            "warn",
            "Shell commands cannot run while a turn is running — "
            "press Esc to cancel it first. Your text is kept.",
        )
        return
    app._prompt_input.buffer.append_to_history()
    app._prompt_input.text = ""
    app._auto_scroll = True
    enqueue_turn(app, text)


def enqueue_turn(app: Any, text: str) -> None:
    """Queue a message to send when the current turn settles."""
    app._pending_turns.append(text)
    app.sink.notify(
        "info",
        f"Queued message #{len(app._pending_turns)} — it will send when the "
        "current turn finishes.",
    )
    app.app.invalidate()


def is_run_in_flight(app: Any) -> bool:
    """True from the moment Enter is pressed until the turn fully settles.

    Guards against overlapping Enter, palette, and autonomous-wake work,
    including the brief window after a visible answer is rendered while
    ``run_turn`` is still persisting it. ``request_exit`` uses the same
    authoritative task and defers exit when persistence is required.
    """
    return app._run_task is not None and not app._run_task.done()


async def dispatch(app: Any, text: str) -> None:
    cmd = parse_command(text)
    if cmd is not None:
        await app._run_command(cmd)
    else:
        await app._run_turn(text)


async def run_command(app: Any, cmd: Command) -> None:
    should_continue = await app._commands.handle(cmd)
    app.app.invalidate()
    if cmd.name in {"/model", "/subagent-model", "/provider"}:
        # The available (or current-marked) model set may have just
        # changed — refresh in the background so autocomplete stays
        # accurate without blocking on another network round trip.
        app.app.create_background_task(app.model_cache.refresh(app.repl.config))
    if cmd.name in {"/sessions", "/new", "/delete"}:
        # Session list may have changed (load/new/delete) — refresh the
        # /sessions autocomplete cache in the background as well.
        app.app.create_background_task(
            app.session_cache.refresh(app.repl.storage, cwd=str(Path.cwd()))
        )
    if not should_continue:
        app.app.exit()


async def run_turn(app: Any, text: str) -> None:
    from .chat_pane import enable_perf_counter

    # Start feedback before the controller/provider can emit its first
    # AgentStartEvent. This removes the otherwise silent post-Enter gap.
    app.sink.begin_activity()
    ticker = app.app.create_background_task(app._tick_activity_indicators())
    count_renders = (
        enable_perf_counter(app.app) if os.environ.get("PHOSON_PERF") else None
    )
    turn_start = time.monotonic()
    renders_before = count_renders() if count_renders else 0
    try:
        await app.repl._run_agent(text)
    except asyncio.CancelledError:
        pass
    finally:
        ticker.cancel()
        app.sink.end_pending_activity()
        app.app.invalidate()
        if count_renders is not None:
            elapsed = time.monotonic() - turn_start
            renders = count_renders() - renders_before
            _PERF_LOGGER.info(
                "perf: turn=%.1fs renders=%d avg_fps=%.1f",
                elapsed,
                renders,
                renders / elapsed if elapsed > 0 else 0.0,
            )


async def tick_activity_indicators(app: Any) -> None:
    """Animate the transient in-chat activity and subagent indicators."""
    while True:
        await asyncio.sleep(SUBAGENT_TICK_SECONDS)
        activity_active = app.sink.tick_activity_frame()
        subagents_active = app.sink.tick_subagent_frame()
        if activity_active or subagents_active:
            app.sink.dirty = True
            app.app.invalidate()


__all__ = [
    "SUBAGENT_TICK_SECONDS",
    "submit",
    "is_run_in_flight",
    "dispatch",
    "run_command",
    "run_turn",
    "tick_activity_indicators",
]
