import json
import threading
from time import sleep

from rich import box
from rich.live import Live
from rich.rule import Rule
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
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
)
from phoson_cli.tools.subagent_panel import (
    render_subagent_panel,
    parse_subagent_metrics,
    render_subagent_summary,
    render_subagent_panel_frame,
)

# ── Palette ────────────────────────────────────────────────────────────────────
_ACCENT = "medium_purple1"
_ACCENT2 = "plum3"
_MUTED = "grey50"
_MUTED2 = "grey35"
_TEXT = "white"
_PANEL_BG = "on #120d1d"
_TOOL_OK = "medium_spring_green"
_TOOL_ERR = "indian_red1"
_REASONING = "grey42"
_WARN = "gold3"
_ERR_BORDER = "indian_red1"


class Renderer:
    """Handles terminal rendering of agent events and user output.

    Manages streaming display, tool execution visualization, and
    session information rendering using Rich library.
    """

    def __init__(self, console: Console | None = None) -> None:
        """Initialize the renderer.

        Args:
            console: Optional Rich Console instance. Creates default if None.
        """
        self.console = console or Console(highlight=False)
        self.session_id: str | None = None
        self._streaming = False
        self._token_buf: list[str] = []
        self._reasoning_active = False
        self._stream_had_tokens = False
        self._waiting_thread: threading.Thread | None = None
        self._waiting_stop: threading.Event | None = None
        self._waiting_message = ""
        self._waiting_visible = False
        self._subagent_live: Live | None = None
        self._subagent_stop: threading.Event | None = None
        self._subagent_thread: threading.Thread | None = None

    def set_session(self, session_id: str) -> None:
        """Set the current session ID for display.

        Args:
            session_id: The session identifier.
        """
        self.session_id = session_id

    def flush_line(self) -> None:
        """Ensure we're on a fresh line after any raw-streamed tokens."""
        self.stop_waiting()
        self.stop_subagent_waiting()
        if self._streaming:
            self.console.print()
            self._streaming = False
        self._reasoning_active = False

    def finish_turn(self) -> None:
        """Re-render buffered tokens as Markdown, then clear the buffer."""
        self.flush_line()
        content = "".join(self._token_buf).strip()
        self._token_buf.clear()
        if content and not self._stream_had_tokens:
            body = Markdown(content, code_theme="monokai")
            self.console.print(
                Panel(
                    body,
                    box=box.SQUARE,
                    border_style=_MUTED2,
                    padding=(0, 1),
                    style=_PANEL_BG,
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
                self._on_start(event)
                self.start_waiting("thinking")

            case AgentTokenEvent():
                self.stop_waiting()
                self._token_buf.append(event.content)
                self.console.print(event.content, end="", soft_wrap=True)
                self._streaming = True
                self._stream_had_tokens = True

            case AgentReasoningEvent():
                self.stop_waiting()
                if not self._reasoning_active:
                    self.console.print(Text("  thinking", style=f"italic {_REASONING}"))
                    self._reasoning_active = True
                self.console.print(
                    Text(event.content, style=_REASONING),
                    end="",
                    soft_wrap=True,
                )
                self._streaming = True

            case AgentToolStartEvent():
                self.flush_line()
                self._on_tool_start(event)

            case AgentToolDoneEvent():
                self._on_tool_done(event)
                self.start_waiting("thinking")

            case AgentStepDoneEvent():
                return  # silent

            case AgentDoneEvent():
                self.finish_turn()
                self._on_done(event)
                self._stream_had_tokens = False

            case AgentErrorEvent():
                self.flush_line()
                self._on_error(event)
                self._stream_had_tokens = False

    def start_waiting(self, label: str = "thinking") -> None:
        """Start the waiting animation with a label.

        Args:
            label: Text to display next to the animation (e.g., "thinking").
        """
        if self._waiting_thread is not None and self._waiting_thread.is_alive():
            self._waiting_message = label
            return
        self._waiting_message = label
        self._waiting_stop = threading.Event()
        self._waiting_thread = threading.Thread(
            target=self._run_waiting_animation,
            daemon=True,
        )
        self._waiting_thread.start()

    def stop_waiting(self) -> None:
        """Stop the waiting animation."""
        if self._waiting_stop is None:
            return
        self._waiting_stop.set()
        if self._waiting_thread is not None and self._waiting_thread.is_alive():
            self._waiting_thread.join(timeout=0.2)
        self._clear_waiting_line()
        self._waiting_thread = None
        self._waiting_stop = None
        self._waiting_message = ""
        self._waiting_visible = False

    def _run_waiting_animation(self) -> None:
        """Internal animation loop for waiting indicator."""
        stop = self._waiting_stop
        if stop is None:
            return

        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        idx = 0
        while not stop.is_set():
            frame = frames[idx % len(frames)]
            self.console.file.write(f"\r\x1b[2K  {frame} {self._waiting_message}")
            self.console.file.flush()
            self._waiting_visible = True
            idx += 1
            sleep(0.08)

    def _clear_waiting_line(self) -> None:
        """Clear the waiting animation line from terminal."""
        if not self._waiting_visible:
            return
        self.console.file.write("\r\x1b[2K")
        self.console.file.flush()

    # ── Sub-renderers ─────────────────────────────────────────────────────────

    def _on_start(self, event: AgentStartEvent) -> None:
        """Handle AgentStartEvent by displaying session info."""
        session = (self.session_id or "")[:8] or "—"
        badge = Text(" assistant ", style=f"bold {_TEXT} on #3a255e")
        meta = Text.assemble(
            badge,
            Text("  ", style=_MUTED),
            Text(event.model, style=f"bold {_ACCENT}"),
            Text(f"  session {session}  ·  {event.message_count} msgs", style=_MUTED),
        )
        self.console.print(meta)

    def _on_tool_start(self, event: AgentToolStartEvent) -> None:
        """Handle AgentToolStartEvent by displaying tool invocation."""
        args_preview = _args_preview(event.tool_name, event.args)
        label = _tool_label(event)
        line = Text()
        line.append("  │ ", style=_ACCENT2)
        if event.tool_name in {"agent", "agents"}:
            line.append("◌ ", style=_ACCENT2)
            line.append(f"spawning {label}", style=f"bold {_ACCENT}")
        else:
            line.append("⚙ ", style=_ACCENT2)
            line.append(label, style=f"bold {_ACCENT}")
        if args_preview:
            line.append(f"  {args_preview}", style=_MUTED)
        self.console.print(line)

        if event.tool_name in {"agent", "agents"}:
            tasks = _subagent_tasks_from_args(event.tool_name, event.args)
            if tasks:
                self.start_subagent_waiting(tasks)

    def _on_tool_done(self, event: AgentToolDoneEvent) -> None:
        """Handle AgentToolDoneEvent by displaying tool result."""
        if event.tool_name in {"agent", "agents"}:
            self.stop_subagent_waiting()
            metrics = parse_subagent_metrics(event.result)
            if metrics:
                self.console.print(render_subagent_summary(metrics))
                return

        label = _tool_label(event)
        line = Text()
        if event.error:
            line.append("  │ ", style=_ACCENT2)
            line.append("✗ ", style=_TOOL_ERR)
            line.append(label, style=f"bold {_TOOL_ERR}")
            line.append(f"  {event.duration_ms}ms", style=_MUTED)
            err_short = event.error.splitlines()[0][:72]
            line.append(f"  {err_short}", style=_TOOL_ERR)
        else:
            line.append("  │ ", style=_ACCENT2)
            if event.tool_name in {"agent", "agents"}:
                line.append("◍ ", style=_TOOL_OK)
                line.append(f"spawned {label}", style=_TOOL_OK)
            else:
                line.append("✓ ", style=_TOOL_OK)
                line.append(label, style=_TOOL_OK)
            line.append(f"  {event.duration_ms}ms", style=_MUTED)
        self.console.print(line)

    def start_subagent_waiting(self, tasks: list[str]) -> None:
        """Start displaying the subagent waiting panel.

        Args:
            tasks: List of subagent task descriptions to display.
        """
        self.stop_subagent_waiting()
        self._subagent_stop = threading.Event()
        self._subagent_live = Live(
            render_subagent_panel(tasks),
            console=self.console,
            refresh_per_second=12,
            transient=True,
        )
        self._subagent_live.start()
        self._subagent_thread = threading.Thread(
            target=self._run_subagent_animation,
            args=(tasks,),
            daemon=True,
        )
        self._subagent_thread.start()

    def stop_subagent_waiting(self) -> None:
        """Stop the subagent waiting panel animation."""
        if self._subagent_stop is not None:
            self._subagent_stop.set()
        if self._subagent_thread is not None and self._subagent_thread.is_alive():
            self._subagent_thread.join(timeout=0.2)
        if self._subagent_live is not None:
            self._subagent_live.stop()
        self._subagent_stop = None
        self._subagent_thread = None
        self._subagent_live = None

    def _run_subagent_animation(self, tasks: list[str]) -> None:
        """Internal animation loop for subagent panel."""
        stop = self._subagent_stop
        live = self._subagent_live
        if stop is None or live is None:
            return

        frame = 0
        while not stop.is_set():
            live.update(render_subagent_panel_frame(tasks, frame), refresh=True)
            frame += 1
            sleep(0.08)

    def _on_done(self, event: AgentDoneEvent) -> None:
        """Handle AgentDoneEvent by displaying run summary."""
        r = event.result
        parts: list[str] = []
        if r.total_cost_usd > 0:
            parts.append(f"${r.total_cost_usd:.5f}")
        steps = len(r.steps)
        parts.append(f"{steps} step{'s' if steps != 1 else ''}")
        if parts:
            self.console.print(Text(f"  {chr(183)} ".join(["", *parts]), style=_MUTED))

    def _on_error(self, event: AgentErrorEvent) -> None:
        """Handle AgentErrorEvent by displaying error panel."""
        body = Text()
        body.append(event.message, style="bold")
        if event.code:
            body.append(f"\ncode={event.code}", style=_MUTED)
        if event.retryable:
            body.append("  retryable", style=_WARN)
        self.console.print(
            Panel(
                body,
                title="error",
                border_style=_ERR_BORDER,
                padding=(0, 1),
                box=box.SQUARE,
                style=_PANEL_BG,
            )
        )

    # ── Utility ───────────────────────────────────────────────────────────────
    def print_user_turn(self, text: str) -> None:
        badge = Text(" user ", style=f"bold {_TEXT} on #23192f")
        self.console.print(badge)
        self.console.print(
            Panel(
                Text(text, style=_TEXT),
                border_style=_MUTED2,
                box=box.SQUARE,
                padding=(0, 1),
                style="on #0f0c14",
            )
        )

    def print_info(self, message: str) -> None:
        """Print an informational message.

        Args:
            message: The message to display.
        """
        self.console.print(Text(f"  {message}", style=_MUTED))

    def print_warn(self, message: str) -> None:
        """Print a warning message.

        Args:
            message: The warning text.
        """
        self.console.print(Text(f"  ⚠ {message}", style=_WARN))

    def print_error(self, message: str) -> None:
        """Print an error message.

        Args:
            message: The error text.
        """
        self.console.print(Text(f"  ✗ {message}", style=_TOOL_ERR))

    def print_history(self, messages: list) -> None:
        """Re-render a list of Message objects as a conversation replay.

        Args:
            messages: List of conversation messages to display.
        """
        from phoson_llm.schemas import TextBlock, ToolUseBlock, ToolResultBlock

        self.console.print(Text(" session history ", style=f"bold {_TEXT} on #2e2047"))
        for msg in messages:
            role = getattr(msg, "role", "?")
            content = getattr(msg, "content", "")

            if role == "system":
                continue

            if role == "user":
                # Skip pure tool-result messages (internal plumbing)
                if not isinstance(content, str) and all(
                    isinstance(b, ToolResultBlock) for b in content
                ):
                    continue
                label = Text("user", style=_ACCENT2)
                self.console.print(label)
                if isinstance(content, str):
                    self.console.print(
                        Panel(
                            Text(content, style=_TEXT),
                            border_style=_MUTED2,
                            box=box.SQUARE,
                            padding=(0, 1),
                            style="on #0f0c14",
                        )
                    )
                else:
                    for block in content:
                        if isinstance(block, TextBlock):
                            self.console.print(Text(f"  {block.text}", style=_TEXT))

            elif role == "assistant":
                badge = Text(" assistant ", style=f"bold {_TEXT} on #3a255e")
                self.console.print(badge)
                if isinstance(content, str) and content.strip():
                    self.console.print(
                        Panel(
                            Markdown(content.strip(), code_theme="monokai"),
                            border_style=_MUTED2,
                            box=box.SQUARE,
                            padding=(0, 1),
                            style=_PANEL_BG,
                        )
                    )
                elif not isinstance(content, str):
                    text_parts = [b.text for b in content if isinstance(b, TextBlock)]
                    tool_uses = [b for b in content if isinstance(b, ToolUseBlock)]
                    combined = "\n".join(text_parts).strip()
                    if combined:
                        self.console.print(
                            Panel(
                                Markdown(combined, code_theme="monokai"),
                                border_style=_MUTED2,
                                box=box.SQUARE,
                                padding=(0, 1),
                                style=_PANEL_BG,
                            )
                        )
                    for b in tool_uses:
                        t = Text()
                        t.append("  │ ", style=_ACCENT2)
                        t.append("⚙ ", style=_ACCENT2)
                        t.append(b.tool_name, style=f"bold {_ACCENT}")
                        self.console.print(t)

        self.console.print(Rule(style=_MUTED2))

    def print_sessions_table(self, sessions: list) -> None:
        """Print a table of sessions.

        Args:
            sessions: List of SessionMeta objects to display.
        """
        table = Table(
            show_header=True,
            header_style=f"bold {_ACCENT}",
            border_style=_MUTED2,
            box=box.SIMPLE_HEAVY,
            padding=(0, 1),
        )
        table.add_column("#", style=_MUTED, width=3, justify="right")
        table.add_column("Session ID", style="white", no_wrap=True)
        table.add_column("Messages", style=_MUTED, justify="right")
        table.add_column("Updated", style=_MUTED)
        table.add_column("State", style=_ACCENT2)
        for i, s in enumerate(sessions, start=1):
            updated = s.updated_at.strftime("%Y-%m-%d %H:%M")
            state = (
                "active"
                if str(s.id).startswith((self.session_id or "")[:4])
                else "saved"
            )
            table.add_row(str(i), s.id, str(s.message_count), updated, state)
        self.console.print(table)

    def print_help(self, entries: list[tuple[str, str]]) -> None:
        """Render the ``/help`` table.

        Args:
            entries: ``(name, description)`` pairs as returned by
                :func:`phoson_cli.commands.get_command_help`. Caller owns
                ordering so that aliases display together.
        """
        table = Table(
            show_header=False,
            border_style=_MUTED2,
            box=box.SIMPLE_HEAVY,
            padding=(0, 1),
        )
        table.add_column("cmd", style=f"bold {_ACCENT}", no_wrap=True)
        table.add_column("desc", style=_MUTED)
        for name, description in entries:
            table.add_row(name, description)
        self.console.print(table)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _args_preview(tool_name: str, args: dict) -> str:
    """Return a compact one-line preview of tool args."""
    if not args:
        return ""
    # For known single-key tools show the value directly
    if len(args) == 1:
        val = next(iter(args.values()))
        s = str(val)
        if len(s) > 72:
            s = s[:69] + "…"
        return f"`{s}`"
    # For multi-key, show key=value pairs inline
    parts = []
    total = 0
    for k, v in args.items():
        s = f"{k}={json.dumps(v)}"
        total += len(s)
        if total > 80:
            parts.append("…")
            break
        parts.append(s)
    return "  ".join(parts)


def _tool_label(event: AgentToolStartEvent | AgentToolDoneEvent) -> str:
    if event.label:
        return event.label
    if event.tool_name == "agent":
        return "subagent"
    if event.tool_name == "agents":
        return "subagents"
    return event.tool_name


def _subagent_tasks_from_args(tool_name: str, args: dict) -> list[str]:
    if tool_name == "agent":
        task = args.get("task")
        return [task] if isinstance(task, str) and task else []

    tasks = args.get("tasks")
    if isinstance(tasks, list):
        return [task for task in tasks if isinstance(task, str) and task]

    return []
