"""Terminal renderer for the Phoson REPL.

Translates ``AgentEvent`` objects into Rich-formatted terminal output.
The two animation lifecycles (waiting spinner and subagent panel) are
isolated in :class:`WaitingSpinner` and :class:`SubagentSpinner` so that
:class:`Renderer` only holds rendering state.
"""

import json
import threading
from time import sleep
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from phoson_llm.schemas import Message
    from phoson_agent.sessions.models import SessionMeta

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

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


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
            frame = _SPINNER_FRAMES[idx % len(_SPINNER_FRAMES)]
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

    def __init__(self, console: Console) -> None:
        self._console = console
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._tasks: list[str] = []

    def start(self, tasks: list[str]) -> None:
        """Start the subagent panel animation."""
        self.stop()
        with self._lock:
            self._tasks = tasks
        self._stop = threading.Event()
        self._live = Live(
            render_subagent_panel(tasks),
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

    def _run(self) -> None:
        stop = self._stop
        live = self._live
        if stop is None or live is None:
            return
        frame = 0
        while not stop.is_set():
            with self._lock:
                tasks = self._tasks
            live.update(render_subagent_panel_frame(tasks, frame), refresh=True)
            frame += 1
            sleep(0.08)


# ── Renderer ───────────────────────────────────────────────────────────────────


class Renderer:
    """Handles terminal rendering of agent events and user output.

    Composes :class:`WaitingSpinner` and :class:`SubagentSpinner` for
    animations, and exposes event handlers for the full ``AgentEvent``
    hierarchy.
    """

    def __init__(self, console: Console | None = None) -> None:
        """Initialize the renderer.

        Args:
            console: Optional Rich Console instance. Creates default if None.
        """
        self.console = console or Console(highlight=False)
        self.session_id: str | None = None

        # ── Streaming state ───────────────────────────────────────────
        self._streaming = False
        self._token_buf: list[str] = []
        self._reasoning_buf: list[str] = []
        self._reasoning_active = False
        self._stream_had_tokens = False

        # ── Run-time context (reset on AgentStartEvent) ───────────────
        self._current_step: int = 0
        self._max_steps: int = 0
        self._run_cost_usd: float = 0.0

        # ── Animations ────────────────────────────────────────────────
        self._spinner = WaitingSpinner(self.console)
        self._subagent_spinner = SubagentSpinner(self.console)

    def set_session(self, session_id: str) -> None:
        """Set the current session ID for display."""
        self.session_id = session_id

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
        if self._streaming:
            self.console.print()
            self._streaming = False
        self._reasoning_active = False

    def finish_turn(self) -> None:
        """Re-render buffered tokens as Markdown, then clear the buffer.

        If reasoning chunks were collected, prints a summary line instead
        of replaying the raw text.
        """
        self.flush_line()

        # Reasoning summary
        if self._reasoning_buf:
            chars = len("".join(self._reasoning_buf))
            approx_tokens = max(1, chars // 4)
            self.console.print(
                Text(f"  ◦  reasoning  (~{approx_tokens} tok)", style=_MUTED)
            )
            self._reasoning_buf.clear()

        # Assistant response
        content = "".join(self._token_buf).strip()
        self._token_buf.clear()
        if content and not self._stream_had_tokens:
            self.console.print(Rule(style=_MUTED2))
            self.console.print(Markdown(content, code_theme="monokai"))

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
                self._on_start(event)
                self.start_waiting(f"thinking  ·  step 0 / {event.max_iterations}")

            case AgentTokenEvent():
                self.stop_waiting()
                self._token_buf.append(event.content)
                self.console.print(event.content, end="", soft_wrap=True)
                self._streaming = True
                self._stream_had_tokens = True

            case AgentReasoningEvent():
                self.stop_waiting()
                self._reasoning_buf.append(event.content)
                self._streaming = True

            case AgentToolStartEvent():
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
                self._on_error(event)
                self._stream_had_tokens = False

    # ── Sub-renderers ─────────────────────────────────────────────────────────

    def _on_start(self, event: AgentStartEvent) -> None:
        """Render session/model badge at the start of a run."""
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
        """Handle tool start: update spinner for regular tools, start subagent panel."""
        if event.tool_name in {"agent", "agents"}:
            label = _tool_label(event)
            line = Text()
            line.append("  │ ", style=_ACCENT2)
            line.append("◌ ", style=_ACCENT2)
            line.append(f"spawning {label}", style=f"bold {_ACCENT}")
            self.console.print(line)
            tasks = _subagent_tasks_from_args(event.tool_name, event.args)
            if tasks:
                self.start_subagent_waiting(tasks)
        else:
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
                self.console.print(render_subagent_summary(metrics))
                return

        label = _tool_label(event)
        line = Text()
        if event.error:
            line.append("  │ ", style=_ACCENT2)
            line.append("✗ ", style=_TOOL_ERR)
            line.append(label, style=f"bold {_TOOL_ERR}")
            line.append(f"  ·  {event.duration_ms}ms", style=_MUTED)
            err_short = event.error.splitlines()[0][:72]
            line.append(f"  ·  {err_short}", style=_TOOL_ERR)
        else:
            line.append("  │ ", style=_ACCENT2)
            if event.tool_name in {"agent", "agents"}:
                line.append("◍ ", style=_TOOL_OK)
                line.append(f"spawned {label}", style=_TOOL_OK)
            else:
                line.append("✓ ", style=_TOOL_OK)
                line.append(label, style=_TOOL_OK)
            line.append(f"  ·  {event.duration_ms}ms", style=_MUTED)
        self.console.print(line)

    def _on_done(self, event: AgentDoneEvent) -> None:
        """Render run summary line."""
        r = event.result
        parts: list[str] = []
        if r.total_cost_usd > 0:
            parts.append(f"${r.total_cost_usd:.5f}")
        steps = len(r.steps)
        parts.append(f"{steps} step{'s' if steps != 1 else ''}")
        if parts:
            self.console.print(Text(f"  {chr(183)} ".join(["", *parts]), style=_MUTED))

    def _on_error(self, event: AgentErrorEvent) -> None:
        """Render error panel."""
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
        """Render a user message with a lightweight badge + plain text."""
        badge = Text(" user ", style=f"bold {_TEXT} on #23192f")
        self.console.print(badge)
        self.console.print(Text(f"  {text}", style=_TEXT))

    def print_info(self, message: str) -> None:
        """Print an informational message."""
        self.console.print(Text(f"  {message}", style=_MUTED))

    def print_warn(self, message: str) -> None:
        """Print a warning message."""
        self.console.print(Text(f"  ⚠ {message}", style=_WARN))

    def print_error(self, message: str) -> None:
        """Print an error message."""
        self.console.print(Text(f"  ✗ {message}", style=_TOOL_ERR))

    def print_history(self, messages: "list[Message]", tail: int | None = None) -> None:
        """Re-render a list of Message objects as a conversation replay.

        Args:
            messages: List of conversation messages to display.
            tail: If set, only render the last ``tail`` messages and print
                  a ``"N messages above"`` rule when the list is longer.
        """
        from phoson_llm.schemas import TextBlock, ToolUseBlock, ToolResultBlock

        if tail is not None and len(messages) > tail:
            above = len(messages) - tail
            self.console.print(Rule(f"{above} messages above", style=_MUTED2))
            messages = messages[-tail:]

        self.console.print(Text(" session history ", style=f"bold {_TEXT} on #2e2047"))

        for msg in messages:
            role = getattr(msg, "role", "?")
            content = getattr(msg, "content", "")

            if role == "system":
                continue

            if role == "user":
                if not isinstance(content, str) and all(
                    isinstance(b, ToolResultBlock) for b in content
                ):
                    continue
                badge = Text(" user ", style=f"bold {_TEXT} on #23192f")
                self.console.print(badge)
                if isinstance(content, str):
                    self.console.print(Text(f"  {content}", style=_TEXT))
                else:
                    for block in content:
                        if isinstance(block, TextBlock):
                            self.console.print(Text(f"  {block.text}", style=_TEXT))

            elif role == "assistant":
                badge = Text(" assistant ", style=f"bold {_TEXT} on #3a255e")
                self.console.print(badge)
                if isinstance(content, str) and content.strip():
                    self.console.print(Rule(style=_MUTED2))
                    self.console.print(Markdown(content.strip(), code_theme="monokai"))
                elif not isinstance(content, str):
                    text_parts = [b.text for b in content if isinstance(b, TextBlock)]
                    tool_uses = [b for b in content if isinstance(b, ToolUseBlock)]
                    combined = "\n".join(text_parts).strip()
                    if combined:
                        self.console.print(Rule(style=_MUTED2))
                        self.console.print(Markdown(combined, code_theme="monokai"))
                    for b in tool_uses:
                        t = Text()
                        t.append("  │ ", style=_ACCENT2)
                        t.append("⚙ ", style=_ACCENT2)
                        t.append(b.tool_name, style=f"bold {_ACCENT}")
                        self.console.print(t)

        self.console.print(Rule(style=_MUTED2))

    def print_sessions_table(self, sessions: "list[SessionMeta]") -> None:
        """Print a table of sessions."""
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
            entries: ``(name, description)`` pairs in display order.
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
    if len(args) == 1:
        val = next(iter(args.values()))
        s = str(val)
        if len(s) > 72:
            s = s[:69] + "…"
        return f"`{s}`"
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
