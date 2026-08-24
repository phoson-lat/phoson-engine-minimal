"""Pure renderable formatters (UI-toolkit agnostic).

Formatters that turn *data* into Rich renderables without any
console/spinner/Live state live here so every front end can reuse
them — the classic Renderer prints them to the terminal; a full-screen
front end can print the same objects into a buffered console.

Keep this module dependency-free of console I/O: no ``Console``, no
``print``, no ``Live``, no threads.
"""

import json
from typing import TYPE_CHECKING

from rich import box
from rich.rule import Rule
from rich.text import Text
from rich.panel import Panel
from rich.console import Group, RenderableType
from rich.markdown import Markdown

from phoson_agent import (
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)

from .theme import Theme

if TYPE_CHECKING:
    from phoson_llm.schemas import Message


def render_reasoning_panel(reasoning: str, theme: Theme) -> Panel:
    """Build the expanded reasoning panel (Ctrl+T post-turn)."""
    return Panel(
        Text(reasoning, style=theme.reasoning),
        title="reasoning",
        title_align="left",
        border_style=theme.muted_deep,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def render_assistant_label(theme: Theme) -> Text:
    """The short colored label preceding assistant output ("Phoson")."""
    return Text("Phoson", style=f"bold {theme.accent}")


def render_streaming_panel(
    content: str, reasoning: str, show_reasoning: bool, theme: Theme
) -> Group:
    """Build the assistant response block: a label, then plain content.

    No border/box — a colored "Phoson" label line directly above the
    rendered Markdown, matching a plain chat-transcript look rather than
    a bordered panel.
    """
    renderables: list[RenderableType] = [render_assistant_label(theme)]

    if reasoning and show_reasoning:
        thinking_text = reasoning.strip() or "thinking..."
        renderables.append(Text(thinking_text, style=theme.reasoning))

    if content:
        try:
            # "none" (Markdown's default) emits no ANSI color at all for
            # plain paragraph text, so inside the full-screen app it falls
            # through to prompt_toolkit's own default foreground (a muted
            # tone, not the terminal's white) — pass the theme color
            # explicitly so the answer always renders in it.
            #
            # hyperlinks=False: Rich's default OSC 8 hyperlink escapes
            # (``\x1b]8;;URL\x1b\\``) aren't understood by prompt_toolkit's
            # ANSI() parser — it only recognizes CSI/SGR (``\x1b[...m``)
            # codes, so an OSC 8 sequence gets torn apart and its raw
            # bytes ("8;id=...;https://...") show up as literal text
            # around the link. Rendering links as "text (url)" with plain
            # SGR color codes avoids that entirely.
            answer_render = Markdown(
                content, code_theme=theme.code_theme, style=theme.text, hyperlinks=False
            )
        except Exception:
            answer_render = Text(content, style=theme.text)
        renderables.append(answer_render)
    elif not (reasoning and show_reasoning):
        renderables.append(Text("thinking...", style=theme.muted))

    return Group(*renderables)


def render_start_line(
    event: AgentStartEvent, session_id: str | None, theme: Theme
) -> Text:
    """Session/model badge shown at the start of a run."""
    session = (session_id or "")[:8] or "—"
    badge = Text(" assistant ", style=theme.badge_assistant)
    return Text.assemble(
        badge,
        Text("  ", style=theme.muted),
        Text(event.model, style=f"bold {theme.accent}"),
        Text(
            f"  session {session}  ·  {event.message_count} msgs",
            style=theme.muted,
        ),
    )


def render_subagent_start_line(event: AgentToolStartEvent, theme: Theme) -> Text:
    """Line shown when an agent/agents tool starts: "spawning subagent(s)"."""
    label = tool_label(event)
    line = Text()
    line.append("  │ ", style=theme.accent_soft)
    line.append("◌ ", style=theme.accent_soft)
    line.append(f"spawning {label}", style=f"bold {theme.accent}")
    return line


def render_tool_start_line(event: AgentToolStartEvent, theme: Theme) -> Text:
    """Compact "running tool" line for a regular (non-subagent) tool call."""
    args_preview = tool_args_preview(event.tool_name, event.args)
    label = tool_label(event)
    line = Text()
    line.append("  │ ", style=theme.accent_soft)
    line.append("⚙ ", style=theme.accent_soft)
    line.append(label, style=f"bold {theme.accent}")
    if args_preview:
        preview = args_preview[:50] + ("…" if len(args_preview) > 50 else "")
        line.append(f"  ·  {preview}", style=theme.muted)
    return line


def render_tool_done_line(event: AgentToolDoneEvent, theme: Theme) -> Text:
    """Compact result line for a finished tool call (success or error)."""
    label = tool_label(event)
    line = Text()
    if event.error:
        line.append("  │ ", style=theme.accent_soft)
        line.append("✗ ", style=theme.err)
        line.append(label, style=f"bold {theme.err}")
        line.append(f"  ·  {event.duration_ms}ms", style=theme.muted)
        err_short = event.error.splitlines()[0][:72]
        line.append(f"  ·  {err_short}", style=theme.err)
    else:
        line.append("  │ ", style=theme.accent_soft)
        if event.tool_name in {"agent", "agents"}:
            line.append("◍ ", style=theme.ok)
            line.append(f"spawned {label}", style=theme.ok)
        else:
            line.append("✓ ", style=theme.ok)
            line.append(label, style=theme.ok)
        line.append(f"  ·  {event.duration_ms}ms", style=theme.muted)
    return line


def render_done_line(event: AgentDoneEvent, theme: Theme) -> Text | None:
    """Run summary line (cost + step count), or None when there's nothing to show."""
    r = event.result
    parts: list[str] = []
    if r.total_cost_usd > 0:
        parts.append(f"${r.total_cost_usd:.5f}")
    steps = len(r.steps)
    parts.append(f"{steps} step{'s' if steps != 1 else ''}")
    if not parts:
        return None
    return Text(f"  {chr(183)} ".join(["", *parts]), style=theme.muted)


def render_error_panel(event: AgentErrorEvent, theme: Theme) -> Panel:
    """Build the error panel shown on ``AgentErrorEvent``."""
    body = Text()
    body.append(event.message, style="bold")
    if event.code:
        body.append(f"\ncode={event.code}", style=theme.muted)
    if event.retryable:
        body.append("  retryable", style=theme.warn)
    return Panel(
        body,
        title="error",
        border_style=theme.err,
        padding=(0, 1),
        box=box.SQUARE,
        style=theme.panel_bg,
    )


def render_user_turn(text: str, theme: Theme) -> Group:
    """Render a user message with a lightweight badge + plain text."""
    badge = Text(" user ", style=theme.badge_user)
    return Group(badge, Text(f"  {text}", style=theme.text))


def render_notice(kind: str, message: str, theme: Theme) -> Text:
    """Render an info/warn/error status line (see ``AgentEventSink.notify``)."""
    if kind == "warn":
        return Text(f"  ⚠ {message}", style=theme.warn)
    if kind == "error":
        return Text(f"  ✗ {message}", style=theme.err)
    return Text(f"  {message}", style=theme.muted)


def tool_args_preview(tool_name: str, args: dict) -> str:
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


def tool_label(event: AgentToolStartEvent | AgentToolDoneEvent) -> str:
    """Human-readable label for a tool call (explicit label, or a fallback)."""
    if event.label:
        return event.label
    if event.tool_name == "agent":
        return "subagent"
    if event.tool_name == "agents":
        return "subagents"
    return event.tool_name


def render_history(
    messages: "list[Message]", theme: Theme, tail: int | None = None
) -> Group:
    """Re-render a list of Message objects as a conversation replay."""
    from phoson_llm.schemas import TextBlock, ToolUseBlock, ToolResultBlock

    items: list[RenderableType] = []

    if tail is not None and len(messages) > tail:
        above = len(messages) - tail
        items.append(Rule(f"{above} messages above", style=theme.muted_deep))
        messages = messages[-tail:]

    items.append(Text(" session history ", style=theme.badge_history))

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
            items.append(Text(" user ", style=theme.badge_user))
            if isinstance(content, str):
                items.append(Text(f"  {content}", style=theme.text))
            else:
                for block in content:
                    if isinstance(block, TextBlock):
                        items.append(Text(f"  {block.text}", style=theme.text))

        elif role == "assistant":
            items.append(Text(" assistant ", style=theme.badge_assistant))
            if isinstance(content, str) and content.strip():
                items.append(Rule(style=theme.muted_deep))
                items.append(
                    Markdown(
                        content.strip(),
                        code_theme=theme.code_theme,
                        style=theme.text,
                        hyperlinks=False,
                    )
                )
            elif not isinstance(content, str):
                text_parts = [b.text for b in content if isinstance(b, TextBlock)]
                tool_uses = [b for b in content if isinstance(b, ToolUseBlock)]
                combined = "\n".join(text_parts).strip()
                if combined:
                    items.append(Rule(style=theme.muted_deep))
                    items.append(
                        Markdown(
                            combined,
                            code_theme=theme.code_theme,
                            style=theme.text,
                            hyperlinks=False,
                        )
                    )
                for b in tool_uses:
                    t = Text()
                    t.append("  │ ", style=theme.accent_soft)
                    t.append("⚙ ", style=theme.accent_soft)
                    t.append(b.tool_name, style=f"bold {theme.accent}")
                    items.append(t)

    items.append(Rule(style=theme.muted_deep))
    return Group(*items)


def subagent_tasks_from_args(tool_name: str, args: dict) -> list[str]:
    """Extract the pending task description(s) from an agent/agents tool call."""
    if tool_name == "agent":
        task = args.get("task")
        return [task] if isinstance(task, str) and task else []
    tasks = args.get("tasks")
    if isinstance(tasks, list):
        return [task for task in tasks if isinstance(task, str) and task]
    return []


__all__ = [
    "render_reasoning_panel",
    "render_assistant_label",
    "render_streaming_panel",
    "render_start_line",
    "render_subagent_start_line",
    "render_tool_start_line",
    "render_tool_done_line",
    "render_done_line",
    "render_error_panel",
    "render_user_turn",
    "render_notice",
    "render_history",
    "tool_args_preview",
    "tool_label",
    "subagent_tasks_from_args",
]
