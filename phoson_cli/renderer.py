"""Terminal renderer for the Phoson REPL.

Translates ``AgentEvent`` objects into Rich-formatted terminal output.
The two animation lifecycles (waiting spinner and subagent panel) are
isolated in :class:`WaitingSpinner` and :class:`SubagentSpinner` so that
:class:`Renderer` only holds rendering state.
"""

import threading
from time import sleep
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from phoson_llm.schemas import Message
    from phoson_agent.sessions.models import SessionMeta

from rich import box
from rich.live import Live
from rich.rule import Rule
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich.console import Group, Console
from rich.markdown import Markdown

from phoson_agent import (
    AgentEvent,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
    AgentToolComposingEvent,
)
from phoson_cli.theme import Theme, load_theme
from phoson_cli.animations import SPINNER_FRAMES
from phoson_cli.formatting import (
    ToolRenderRegistry,
    render_notice,
    render_history,
    render_done_line,
    render_user_turn,
    render_start_line,
    render_error_notice,
    render_tool_done_line,
    render_reasoning_panel,
    render_streaming_panel,
    render_subagent_start_line,
)
from phoson_cli.formatting import (
    tool_icon as _tool_icon,
)
from phoson_cli.formatting import (
    tool_verb as _tool_verb,
)
from phoson_cli.formatting import (
    tool_label as _tool_label,
)
from phoson_cli.formatting import (
    tool_args_preview as _args_preview,
)
from phoson_cli.formatting import (
    subagent_tasks_from_args as _subagent_tasks_from_args,
)
from phoson_cli.command_host import HelpEntry, HelpEntries, is_grouped_help
from phoson_cli.tools.subagent_panel import (
    render_subagent_panel,
    parse_subagent_metrics,
    render_subagent_summary,
    render_subagent_panel_frame,
)

# Palette lives in phoson_cli.theme (see Renderer.theme).


# ── Animation helpers ──────────────────────────────────────────────────────────


class WaitingSpinner:
    """Braille-spinner animation that writes directly to the console file.

    Thread-safe: ``_label`` updates are serialised via ``_lock`` so the
    animation thread always sees a consistent string.
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._label: str = ""
        self._visible: bool = False

    def start(self, label: str) -> None:
        """Start the spinner, or update the label if already running."""
        with self._lock:
            self._label = label
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self, label: str) -> None:
        """Replace the displayed label without restarting the thread."""
        with self._lock:
            self._label = label

    def stop(self) -> None:
        """Stop animation and clear the spinner line."""
        if self._stop is None:
            return
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.2)
        self._clear()
        self._thread = None
        self._stop = None
        with self._lock:
            self._label = ""
        self._visible = False

    def _run(self) -> None:
        stop = self._stop
        if stop is None:
            return
        idx = 0
        while not stop.is_set():
            frame = SPINNER_FRAMES[idx % len(SPINNER_FRAMES)]
            with self._lock:
                label = self._label
            self._console.file.write(f"\r\x1b[2K  {frame}  {label}")
            self._console.file.flush()
            self._visible = True
            idx += 1
            sleep(0.08)

    def _clear(self) -> None:
        if not self._visible:
            return
        self._console.file.write("\r\x1b[2K")
        self._console.file.flush()


class SubagentSpinner:
    """Rich Live panel animation for parallel subagent execution."""

    def __init__(self, console: Console, theme: Theme | None = None) -> None:
        self._console = console
        self._theme = theme or load_theme()
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._tasks: list[str] = []
        # Live per-task metrics (E2); None → the table shows "waiting".
        self._progress: object | None = None

    def start(self, tasks: list[str]) -> None:
        """Start the subagent panel animation."""
        self.stop()
        with self._lock:
            self._tasks = tasks
        self._stop = threading.Event()
        self._live = Live(
            render_subagent_panel(tasks, theme=self._theme, progress=self._progress),
            console=self._console,
            refresh_per_second=12,
            transient=True,
        )
        self._live.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop animation and close the Live context."""
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.2)
        if self._live is not None:
            self._live.stop()
        self._stop = None
        self._thread = None
        self._live = None
        self._progress = None

    def set_progress(self, progress: object | None) -> None:
        """Attach/detach the live-metrics source (E2).

        The animation thread re-reads it on every frame, so values keep
        updating while the panel is on screen; ``None`` falls back to
        the static "waiting" cells.
        """
        with self._lock:
            self._progress = progress

    def _run(self) -> None:
        stop = self._stop
        live = self._live
        if stop is None or live is None:
            return
        frame = 0
        while not stop.is_set():
            with self._lock:
                tasks = self._tasks
                progress = self._progress
            live.update(
                render_subagent_panel_frame(
                    tasks, frame, theme=self._theme, progress=progress
                ),
                refresh=True,
            )
            frame += 1
            sleep(0.08)


# ── Renderer ───────────────────────────────────────────────────────────────────


class Renderer:
    """Handles terminal rendering of agent events and user output.

    Composes :class:`WaitingSpinner` and :class:`SubagentSpinner` for
    animations, and exposes event handlers for the full ``AgentEvent``
    hierarchy.
    """

    def __init__(
        self, console: Console | None = None, theme: Theme | None = None
    ) -> None:
        """Initialize the renderer.

        Args:
            console: Optional Rich Console instance. Creates default if None.
            theme: Optional :class:`Theme`. Resolved via ``load_theme()``
                (env/config) when None.
        """
        self.console = console or Console(highlight=False)
        self.theme = theme or load_theme()
        self.session_id: str | None = None

        # ── Streaming state ───────────────────────────────────────────
        self._streaming = False
        self._token_buf: list[str] = []
        self._reasoning_buf: list[str] = []
        self._reasoning_active = False
        self._stream_had_tokens = False

        # ── Reasoning (Ctrl+T) ─────────────────────────────────────────
        # Full reasoning text captured during the last finished run, until
        # the REPL persists it to the tree node (see take_last_reasoning).
        self._last_reasoning: str = ""
        # Live toggle: show/hide the "thinking" section while streaming.
        self._live_show_reasoning: bool = True

        # ── Live panel for streaming ─────────────────────────────────
        self._live: Live | None = None
        self._live_content: str = ""
        self._live_reasoning: str = ""

        # Args of in-flight regular tool calls, keyed by tool_call_id (C1):
        # done events don't carry args, so the start event stashes them and
        # the done card pops them to render path/command detail + diffs.
        self._pending_tool_args: dict[str, dict] = {}
        self._tool_render_registry = ToolRenderRegistry({})

        # ── Run-time context (reset on AgentStartEvent) ───────────────
        self._current_step: int = 0
        self._max_steps: int = 0
        self._run_cost_usd: float = 0.0

        # ── Animations ────────────────────────────────────────────────
        self._spinner = WaitingSpinner(self.console)
        self._subagent_spinner = SubagentSpinner(self.console, theme=self.theme)

    def set_session(self, session_id: str) -> None:
        """Set the current session ID for display."""
        self.session_id = session_id

    def set_tool_render_registry(self, registry: ToolRenderRegistry) -> None:
        """Apply the active controller's isolated plugin visual specs."""
        self._tool_render_registry = registry

    # ── Public animation delegates ────────────────────────────────────────────

    def start_waiting(self, label: str = "thinking") -> None:
        """Start (or relabel) the waiting spinner."""
        self._spinner.start(label)

    def update_waiting(self, label: str) -> None:
        """Update the spinner label without restarting it."""
        self._spinner.update(label)

    def stop_waiting(self) -> None:
        """Stop and clear the waiting spinner."""
        self._spinner.stop()

    def start_subagent_waiting(self, tasks: list[str]) -> None:
        """Start the subagent panel animation."""
        self._subagent_spinner.start(tasks)

    def stop_subagent_waiting(self) -> None:
        """Stop the subagent panel animation."""
        self._subagent_spinner.stop()

    def flush_line(self) -> None:
        """Ensure we're on a fresh line after any raw-streamed tokens."""
        self.stop_waiting()
        self.stop_subagent_waiting()
        self._stop_live_streaming()
        if self._streaming:
            self.console.print()
            self._streaming = False
        self._reasoning_active = False

    # ── Reasoning (Ctrl+T) ────────────────────────────────────────────

    def take_last_reasoning(self) -> str:
        """Pop the reasoning text captured during the last finished run.

        The REPL persists it on the last assistant node so it survives
        resume and can be expanded later with Ctrl+T.
        """
        text, self._last_reasoning = self._last_reasoning, ""
        return text

    def capture_partial_reasoning(self) -> None:
        """Snapshot reasoning collected so far when a run ends without a
        normal finish (cancellation or agent error)."""
        if self._reasoning_buf:
            self._last_reasoning = "".join(self._reasoning_buf)
            self._reasoning_buf.clear()

    def toggle_live_reasoning(self) -> bool:
        """Toggle the live "thinking" section while a run is streaming.

        Returns:
            True when the thinking section is now visible.
        """
        self._live_show_reasoning = not self._live_show_reasoning
        if self._live is not None:
            self._update_live_streaming()
        return self._live_show_reasoning

    def render_reasoning_panel(self, reasoning: str) -> Panel:
        """Build the expanded reasoning panel (Ctrl+T post-turn).

        Thin delegate over the pure formatter in :mod:`.formatting` (a
        future front end can reuse the same renderable).
        """
        return render_reasoning_panel(reasoning, self.theme)

    # ── Live streaming panel ────────────────────────────────────────────────

    def _start_live_streaming(self) -> None:
        """Start a Rich Live panel for streaming Markdown."""
        if self._live is not None:
            return
        self._live = Live(
            self._render_live_panel(),
            console=self.console,
            refresh_per_second=8,
            # Keep the final streamed Markdown visible after Live.stop().
            # Rich removes the live renderable on exit when transient=True.
            transient=False,
        )
        self._live.start()

    def _update_live_streaming(self) -> None:
        """Update the Live panel with current buffer content."""
        if self._live is None:
            return
        self._live.update(self._render_live_panel(), refresh=True)

    def _stop_live_streaming(self) -> None:
        """Stop the Live panel and render final Markdown."""
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    def _render_live_panel(self) -> Group:
        """Render the current stream: a label, then thinking/answer blocks."""
        return render_streaming_panel(
            self._live_content,
            self._live_reasoning,
            self._live_show_reasoning,
            self.theme,
        )

    def finish_turn(self) -> None:
        """Re-render buffered tokens as Markdown, then clear the buffer.

        If reasoning chunks were collected, prints a summary line instead
        of replaying the raw text.
        """
        self._stop_live_streaming()

        # Reasoning summary — the full text is kept for Ctrl+T expansion
        # (the REPL persists it to the node's metadata).
        if self._reasoning_buf:
            reasoning = "".join(self._reasoning_buf)
            self._last_reasoning = reasoning
            approx_tokens = max(1, len(reasoning) // 4)
            self.console.print(
                Text(
                    f"  ◦  reasoning  (~{approx_tokens} tok)  —  Ctrl+T to expand",
                    style=self.theme.muted,
                )
            )
            self._reasoning_buf.clear()
        else:
            self._last_reasoning = ""

        # Assistant response - only if not streamed (fallback)
        content = "".join(self._token_buf).strip()
        self._token_buf.clear()
        if content and not self._stream_had_tokens:
            self.console.print(Rule(style=self.theme.muted_deep))
            self.console.print(
                Markdown(
                    content,
                    code_theme=self.theme.code_theme,
                    style=self.theme.text,
                    # hyperlinks=True (IMPROVEMENTS.md G4, #58): this
                    # console prints straight to the real terminal (no
                    # ANSI() re-parse in between, unlike the full-screen
                    # bridge), so Rich's OSC 8 escapes reach it intact —
                    # clickable links in terminals that support OSC 8.
                    hyperlinks=True,
                )
            )

    # ── Event dispatch ────────────────────────────────────────────────────────

    def on_event(self, event: AgentEvent) -> None:
        """Dispatch an agent event to the appropriate handler.

        Args:
            event: The agent event to render.
        """
        match event:
            case AgentStartEvent():
                self._current_step = 0
                self._max_steps = event.max_iterations
                self._run_cost_usd = 0.0
                self._token_buf.clear()
                self._reasoning_buf.clear()
                self._live_content = ""
                self._live_reasoning = ""
                self._stream_had_tokens = False
                self._on_start(event)
                self.start_waiting(f"thinking  ·  step 0 / {event.max_iterations}")

            case AgentTokenEvent():
                self.stop_waiting()
                self._token_buf.append(event.content)
                self._live_content += event.content
                self._streaming = True
                self._stream_had_tokens = True

                # Start or update Live panel
                if self._live is None:
                    self._start_live_streaming()
                else:
                    self._update_live_streaming()

            case AgentReasoningEvent():
                self.stop_waiting()
                self._reasoning_buf.append(event.content)
                self._live_reasoning += event.content
                self._streaming = True
                self._reasoning_active = True

                # Update Live panel with reasoning
                if self._live is None:
                    self._start_live_streaming()
                else:
                    self._update_live_streaming()

            case AgentToolComposingEvent():
                self._on_tool_composing(event)

            case AgentToolStartEvent():
                self._stop_live_streaming()
                self.flush_line()
                self._on_tool_start(event)

            case AgentToolDoneEvent():
                self._on_tool_done(event)
                self.start_waiting(
                    f"thinking  ·  step {self._current_step} / {self._max_steps}"
                    + (f"  ·  ${self._run_cost_usd:.4f}" if self._run_cost_usd else "")
                )

            case AgentStepDoneEvent():
                self._current_step += 1
                self._run_cost_usd += event.step.cost_usd
                self.update_waiting(
                    f"thinking  ·  step {self._current_step} / {self._max_steps}"
                    f"  ·  ${self._run_cost_usd:.4f}"
                )

            case AgentDoneEvent():
                self.finish_turn()
                self._on_done(event)
                self._stream_had_tokens = False

            case AgentErrorEvent():
                self.flush_line()
                self.capture_partial_reasoning()
                self._on_error(event)
                self._stream_had_tokens = False

    # ── Sub-renderers ─────────────────────────────────────────────────────────

    def _on_start(self, event: AgentStartEvent) -> None:
        """Render session/model badge at the start of a run."""
        self.console.print(render_start_line(event, self.session_id, self.theme))

    def _on_tool_composing(self, event: AgentToolComposingEvent) -> None:
        """Handle tool composing: relabel the waiting spinner (I-128).

        The classic front end has no persistent pane, so the feedback is
        the spinner label: ``⚙  running command…`` while the model still
        generates the call, upgraded to the full ``⚙  tool · args`` line
        by :meth:`_on_tool_start` once the call lands. Skipped while the
        Live streaming panel is open (the growing text is the feedback
        there) and for deltas whose name is not known yet.
        """
        if not event.tool_name or self._live is not None:
            return
        self.start_waiting(
            f"{_tool_icon(event.tool_name, self._tool_render_registry)}  "
            f"{_tool_verb(event.tool_name, self._tool_render_registry)}…"
        )

    def _on_tool_start(self, event: AgentToolStartEvent) -> None:
        """Handle tool start: update spinner for regular tools, start subagent panel."""
        if event.tool_name in {"agent", "agents"}:
            self.console.print(render_subagent_start_line(event, self.theme))
            tasks = _subagent_tasks_from_args(event.tool_name, event.args)
            if tasks:
                self.start_subagent_waiting(tasks)
        else:
            # Remember the call's args so the done card can render its
            # detail line and specialized body (done events don't carry
            # args) — C1.
            if event.tool_call_id:
                self._pending_tool_args[event.tool_call_id] = dict(event.args)
            args_preview = _args_preview(event.tool_name, event.args)
            label = _tool_label(event)
            spinner_text = f"⚙  {label}"
            if args_preview:
                spinner_text += (
                    f"  ·  {args_preview[:50]}{'…' if len(args_preview) > 50 else ''}"
                )
            self.start_waiting(spinner_text)

    def _on_tool_done(self, event: AgentToolDoneEvent) -> None:
        """Handle tool done: print compact result line."""
        if event.tool_name in {"agent", "agents"}:
            self.stop_subagent_waiting()
            metrics = parse_subagent_metrics(event.result)
            if metrics:
                self.console.print(render_subagent_summary(metrics, theme=self.theme))
                return

        start_args = self._pending_tool_args.pop(event.tool_call_id or "", None)
        self.console.print(
            render_tool_done_line(
                event,
                self.theme,
                args=start_args,
                registry=self._tool_render_registry,
            )
        )

    def _on_done(self, event: AgentDoneEvent) -> None:
        """Render run summary line."""
        line = render_done_line(event, self.theme)
        if line is not None:
            self.console.print(line)

    def _on_error(self, event: AgentErrorEvent) -> None:
        """Render the single-line error notice (I-83).

        The classic REPL prints to a real terminal (no mutable block
        list), so repeated retries still add one line each — but a line,
        not a ~6-line panel. The raw body is logged at debug level.
        """
        self.console.print(render_error_notice(event, self.theme))

    # ── Utility ───────────────────────────────────────────────────────────────

    def print_user_turn(self, text: str) -> None:
        """Render a user message with a lightweight badge + plain text."""
        self.console.print(render_user_turn(text, self.theme))

    def print_info(self, message: str) -> None:
        """Print an informational message."""
        self.console.print(render_notice("info", message, self.theme))

    def print_warn(self, message: str) -> None:
        """Print a warning message."""
        self.console.print(render_notice("warn", message, self.theme))

    def print_error(self, message: str) -> None:
        """Print an error message."""
        self.console.print(render_notice("error", message, self.theme))

    def publish_plugin_block(self, block_id: str, block: object) -> None:  # noqa: ARG002
        """Classic transcripts are append-only; publish the neutral plugin card."""
        self.console.print(block)

    def replace_plugin_block(self, block_id: str, block: object) -> None:  # noqa: ARG002
        self.console.print(block)

    def remove_plugin_block(self, block_id: str) -> None:  # noqa: ARG002
        pass

    def print_history(self, messages: "list[Message]", tail: int | None = None) -> None:
        """Re-render a list of Message objects as a conversation replay.

        Args:
            messages: List of conversation messages to display.
            tail: If set, only render the last ``tail`` messages and print
                  a ``"N messages above"`` rule when the list is longer.
        """
        self.console.print(render_history(messages, self.theme, tail=tail))

    def print_sessions_table(self, sessions: "list[SessionMeta]") -> None:
        """Print a table of sessions."""
        table = Table(
            show_header=True,
            header_style=f"bold {self.theme.accent}",
            border_style=self.theme.muted_deep,
            box=box.SIMPLE_HEAVY,
            padding=(0, 1),
        )
        table.add_column("#", style=self.theme.muted, width=3, justify="right")
        table.add_column("Session ID", style="white", no_wrap=True)
        table.add_column("Messages", style=self.theme.muted, justify="right")
        table.add_column("Updated", style=self.theme.muted)
        table.add_column("State", style=self.theme.accent_soft)
        for i, s in enumerate(sessions, start=1):
            updated = s.updated_at.strftime("%Y-%m-%d %H:%M")
            state = (
                "active"
                if str(s.id).startswith((self.session_id or "")[:4])
                else "saved"
            )
            table.add_row(str(i), s.id, str(s.message_count), updated, state)
        self.console.print(table)

    def print_help(self, entries: HelpEntries) -> None:
        """Render the ``/help`` listing.

        Accepts the grouped form ``[(category, [(name, desc), ...]), ...]``
        (IMPROVEMENTS.md C4) or, for backward compatibility, a flat list of
        ``(name, description)`` pairs.
        """
        if is_grouped_help(entries):
            for category, commands in entries:  # type: ignore[union-attr]
                self.console.print(
                    Text(f" {category}", style=f"bold {self.theme.accent}")
                )
                table = Table(
                    show_header=False,
                    border_style=self.theme.muted_deep,
                    box=box.SIMPLE,
                    padding=(0, 1),
                )
                table.add_column("cmd", style=f"bold {self.theme.accent}", no_wrap=True)
                table.add_column("desc", style=self.theme.muted)
                for name, description in commands:
                    table.add_row(name, description)
                self.console.print(table)
            return

        table = Table(
            show_header=False,
            border_style=self.theme.muted_deep,
            box=box.SIMPLE_HEAVY,
            padding=(0, 1),
        )
        table.add_column("cmd", style=f"bold {self.theme.accent}", no_wrap=True)
        table.add_column("desc", style=self.theme.muted)
        flat_entries = cast("list[HelpEntry]", entries)
        table_rows: list[tuple[str, str]] = list(flat_entries)
        for row in table_rows:
            table.add_row(row[0], row[1])
        self.console.print(table)


# ── AgentEventSink adapter ────────────────────────────────────────────────────


class ClassicSink:
    """``AgentEventSink`` implementation over the classic Rich ``Renderer``.

    This is the presentation adapter used by the classic REPL (and by
    :class:`~phoson_cli.controller.SessionController` in tests). A
    future front end will provide its own sink; nothing in the
    controller depends on this class.
    """

    def __init__(self, renderer: "Renderer") -> None:
        self._renderer = renderer

    def on_user_message(self, text: str, message: "Message") -> None:
        self._renderer.print_user_turn(text)

    def on_attachments(self, sources: list[str]) -> None:
        for source in sources:
            self._renderer.print_info(f"  📎 {source}")

    def on_event(self, event: AgentEvent) -> None:
        self._renderer.on_event(event)

    def flush_line(self) -> None:
        self._renderer.flush_line()

    def capture_partial_reasoning(self) -> None:
        self._renderer.capture_partial_reasoning()

    def take_reasoning(self) -> str:
        return self._renderer.take_last_reasoning()

    def set_session(self, session_id: str) -> None:
        self._renderer.set_session(session_id)

    def set_tool_render_registry(self, registry: ToolRenderRegistry) -> None:
        """Forward a controller-scoped visual registry to the renderer."""
        self._renderer.set_tool_render_registry(registry)

    def publish_plugin_block(self, block_id: str, block: object) -> None:
        self._renderer.publish_plugin_block(block_id, block)

    def replace_plugin_block(self, block_id: str, block: object) -> None:
        self._renderer.replace_plugin_block(block_id, block)

    def remove_plugin_block(self, block_id: str) -> None:
        self._renderer.remove_plugin_block(block_id)

    def on_subagent_progress(self, progress: object | None) -> None:
        """Feed the live panel with per-task metrics (E2).

        The classic front end renders the panel in a transient Rich
        ``Live`` display owned by :class:`SubagentSpinner`, so this just
        attaches/detaches the tracker the animation reads each frame.
        """
        self._renderer._subagent_spinner.set_progress(progress)

    def print_history(self, path: list["Message"], tail: int | None = None) -> None:
        self._renderer.print_history(path, tail=tail)

    def notify(self, kind: str, message: str) -> None:
        method = getattr(self._renderer, f"print_{kind}", None)
        if method is not None:
            method(message)
