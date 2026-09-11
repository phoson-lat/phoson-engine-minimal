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

from ..commands import Command, parse_command

_PERF_LOGGER = logging.getLogger("phoson_cli.fullscreen.perf")

# How often the subagent panel animation frame advances while active.
# Kept at 0.12 s (I-84): 0.2 s made the braille spinner visibly lag
# (2 s/rotation vs 1.2 s). The streaming freeze in
# `tick_activity_frame()` — not the tick rate — is what cuts CPU.
SUBAGENT_TICK_SECONDS = 0.12


def submit(app: Any) -> None:
    """Handle Enter on the input line: dispatch a command or an agent turn.

    While a turn is already in flight the input is *kept* (not cleared)
    and the user is told why nothing happened — otherwise pressing Enter
    looks like the app froze (IMPROVEMENTS.md A4). The header already
    shows the live status ("Streaming" / "Running tool") so the user can
    see the turn is still going.
    """
    text = app._prompt_input.text
    if not text.strip():
        return
    if app._is_run_in_flight():
        app.sink.notify(
            "warn",
            "A turn is already running — press Esc to cancel it first. "
            "Your text is kept.",
        )
        return
    # Persist to the input history. The custom submit path bypasses the
    # buffer's ``accept_handler`` (which normally does this), so it must
    # be spelled out (IMPROVEMENTS.md A2).
    app._prompt_input.buffer.append_to_history()
    app._prompt_input.text = ""
    app._auto_scroll = True
    # T-12: a leading "!" (with the rest non-blank) is a shell command,
    # not an agent turn or a slash command.
    if text.startswith("!") and text[1:].strip():
        app._run_task = app.app.create_background_task(
            app._run_bash_line(text[1:].strip())
        )
        return
    app._run_task = app.app.create_background_task(app._dispatch(text))


def is_run_in_flight(app: Any) -> bool:
    """True from the moment Enter is pressed until the turn fully settles.

    Guards against a second submission overlapping the first (which
    would race two mutations of the same tree/session state) —
    including the brief window after the visible answer is already
    rendered but ``run_turn`` is still persisting it. For "should
    Ctrl+C/Ctrl+Q interrupt something visible" use
    ``sink.current_turn is not None`` instead (see ``request_exit``)
    — that invisible trailing save is not cancel-worthy.
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
