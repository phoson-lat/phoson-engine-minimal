"""Pure renderable formatters (UI-toolkit agnostic).

Formatters that turn *data* into Rich renderables without any
console/spinner/Live state live here so every front end can reuse
them — the classic Renderer prints them to the terminal; a full-screen
front end can print the same objects into a buffered console.

Keep this module dependency-free of console I/O: no ``Console``, no
``print``, no ``Live``, no threads.
"""

import json
import difflib
import logging
from typing import TYPE_CHECKING, Any, Final

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

#: Raw provider error bodies (often raw JSON) are logged here at debug
#: level so they remain available for troubleshooting without dominating
#: the transcript (I-83). No handler is attached — the process-level
#: logging config decides where it goes.
logger = logging.getLogger("phoson.cli.errors")


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


def render_activity_line(label: str, frame: str, theme: Theme) -> Text:
    """Build the transient in-chat activity indicator for an active turn.

    Unlike a transcript block this is rendered only while a turn is running:
    it gives immediate feedback after Enter, before the provider has emitted
    its first event, and remains animated while thinking, streaming, or
    running a tool.
    """
    return Text.assemble(
        Text(f"{frame} ", style=f"bold {theme.accent}"),
        Text(label, style=theme.muted),
    )


def render_streaming_panel(
    content: str,
    reasoning: str,
    show_reasoning: bool,
    theme: Theme,
    stream_plain: bool = False,
) -> Group:
    """Build the assistant response block: a label, then plain content.

    No border/box — a colored "Phoson" label line directly above the
    rendered Markdown, matching a plain chat-transcript look rather than
    a bordered panel.

    With ``stream_plain=True`` the content renders as un-parsed text:
    the cheap path used while tokens are still arriving (perf — see the
    comment on the fast path below). Frozen/finalized turns always get
    the full Markdown render.
    """
    renderables: list[RenderableType] = [render_assistant_label(theme)]

    if reasoning and show_reasoning:
        thinking_text = reasoning.strip() or "thinking..."
        renderables.append(Text(thinking_text, style=theme.reasoning))

    if content:
        if stream_plain:
            # Streaming fast path (perf/render-cache): Rich's Markdown
            # parser costs O(content length) per render (~6ms/K chars) and
            # during token streaming it would re-parse ever-growing text
            # on every frame. Plain Text is near-free; the full Markdown
            # render happens once when the turn's text is frozen into the
            # transcript (_freeze_current_text).
            answer_render = Text(content, style=theme.text)
        else:
            try:
                # "none" (Markdown's default) emits no ANSI color at all for
                # plain paragraph text, so inside the full-screen app it falls
                # through to prompt_toolkit's own default foreground (a muted
                # tone, not the terminal's white) — pass the theme color
                # explicitly so the answer always renders in it.
                #
                # hyperlinks=True (IMPROVEMENTS.md G4, #58): Rich's real OSC 8
                # hyperlink escapes (``\x1b]8;;URL\x1b\\``) make links clickable
                # in terminals that support it. prompt_toolkit's ANSI() parser
                # doesn't understand OSC 8 on its own and would tear the
                # sequence apart into literal text — the full-screen render
                # path (fullscreen/render.py) runs
                # ``phoson_cli.hyperlinks.osc8_passthrough`` on the ANSI string
                # before wrapping it in ANSI(), which is what actually carries
                # the sequence through intact. The classic REPL prints
                # straight to a real Console, so it needs no such fix.
                answer_render = Markdown(
                    content,
                    code_theme=theme.code_theme,
                    style=theme.text,
                    hyperlinks=True,
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


def _sanitize_error_message(message: str, limit: int = 80) -> str:
    """Reduce a raw provider error body to a short, readable fragment.

    Provider bodies are often raw JSON (``{"error": {"message": ...}}``
    or ``{"error": "..."}``); when parseable, the innermost
    human-readable message is extracted. Whitespace is collapsed and the
    result truncated to *limit* characters (I-83).
    """
    text = " ".join(message.split())
    if not text:
        return ""
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        candidates: list[object] = [payload.get("message")]
        error = payload.get("error")
        if isinstance(error, str):
            candidates.append(error)
        elif isinstance(error, dict):
            candidates.append(error.get("message"))
        for value in candidates:
            if isinstance(value, str) and value.strip():
                text = " ".join(value.split())
                break
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def render_error_notice(event: AgentErrorEvent, theme: Theme) -> Text:
    """Build the single-line error warning shown on ``AgentErrorEvent`` (I-83).

    Compact, overwritable replacement for :func:`render_error_panel` in
    the transcript flow::

        ⚠ server_error · retryable — provider-side failure — retry, or switch model
        ⚠ rate_limit · retryable — wait a moment, or switch model with /model

    The detail is the actionable hint for known codes, or a sanitized
    fragment of the raw message otherwise. The raw provider body (often
    raw JSON) is never displayed — it is logged at debug level for
    troubleshooting.
    """
    logger.debug("raw provider error: %s", event.message)
    line = Text()
    line.append("  ⚠ ", style=theme.warn)
    line.append(event.code or "error", style=f"bold {theme.err}")
    if event.retryable:
        line.append(" · retryable", style=theme.muted)
    detail = error_hint(event.code) or _sanitize_error_message(event.message)
    if detail:
        line.append(f" — {detail}", style=theme.warn)
    return line


def render_error_panel(event: AgentErrorEvent, theme: Theme) -> Panel:
    """Build the error panel shown on ``AgentErrorEvent``.

    Known error codes get a trailing "hint" line with the actionable next
    step (IMPROVEMENTS.md C4) — e.g. ``auth`` points at /setup.

    Kept for expandable/debug views; the transcript flow uses the
    single-line :func:`render_error_notice` instead (I-83).
    """
    body = Text()
    body.append(event.message, style="bold")
    if event.code:
        body.append(f"\ncode={event.code}", style=theme.muted)
    if event.retryable:
        body.append("  retryable", style=theme.warn)
    hint = error_hint(event.code)
    if hint:
        body.append(f"\nhint: {hint}", style=theme.warn)
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


# ─── Rich tool cards (IMPROVEMENTS.md C1) ────────────────────────────────────

#: Human verb per tool — the card headline ("writing file src/x.py").
#: Subagent tools keep their dedicated start/done lines, so they are absent.
_TOOL_VERBS: Final[dict[str, str]] = {
    "read_file": "reading file",
    "write_file": "writing file",
    "patch_file": "editing file",
    "list_dir": "listing directory",
    "view_image": "viewing image",
    "bash": "running command",
    "skill": "loading skill",
    "web_search": "searching the web",
    "web_fetch": "fetching page",
}

#: Max rendered diff lines before truncation with an explicit notice.
_DIFF_MAX_LINES: Final[int] = 20
#: Max lines of bash output echoed in the done card.
_BASH_PREVIEW_LINES: Final[int] = 6
_INDENT: Final[str] = "      "


def tool_verb(tool_name: str) -> str:
    """Human action phrase for a tool name ("write_file" → "writing file")."""
    return _TOOL_VERBS.get(tool_name, tool_name.replace("_", " "))


def tool_detail(tool_name: str, args: dict[str, Any]) -> str:
    """One-line detail of *what* is being acted on (path / command / query).

    Falls back to :func:`tool_args_preview` for tools without a dedicated
    detail extractor.
    """
    if tool_name == "bash":
        cmd = str(args.get("command") or "")
        one_line = " ".join(cmd.split())
        return one_line[:72] + ("…" if len(one_line) > 72 else "")
    for key in ("path", "query", "url", "pattern", "name"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return tool_args_preview(tool_name, args)


def unified_diff(
    old_content: str, new_content: str, path: str, context_lines: int = 3
) -> list[str]:
    """Unified diff lines between two file versions (no trailing newlines).

    Returns ``[]`` when the contents are identical.
    """
    diff = difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=path,
        tofile=path,
        n=context_lines,
    )
    return [line.rstrip("\n") for line in diff]


def _card_header(tool_name: str, args: dict[str, Any], theme: Theme) -> Text:
    """The ``│ ⚙ <verb> · <detail>`` headline shared by start/done cards."""
    header = Text()
    header.append("  │ ", style=theme.accent_soft)
    header.append("⚙ ", style=theme.accent_soft)
    header.append(tool_verb(tool_name), style=f"bold {theme.accent}")
    detail = tool_detail(tool_name, args)
    if detail:
        header.append(f"  ·  {detail}", style=theme.muted)
    return header


def render_tool_start_line(event: AgentToolStartEvent, theme: Theme) -> Text:
    """Compact "running tool" line for a regular (non-subagent) tool call."""
    return _card_header(event.tool_name, event.args, theme)


def render_tool_done_line(
    event: AgentToolDoneEvent,
    theme: Theme,
    args: dict[str, Any] | None = None,
) -> RenderableType:
    """Result card for a finished regular (non-subagent) tool call.

    ``args`` should be the call's arguments as captured from the matching
    :class:`AgentToolStartEvent` (done events don't carry them); front ends
    remember them keyed by ``tool_call_id``. Without them the card still
    renders — just without path/command detail or specialized bodies.

    Specialized bodies: colored unified diff for ``patch_file`` (built
    purely from the ``old_content``/``new_content`` args), a created/updated
    summary for ``write_file``, first stdout lines for ``bash``.
    """
    call_args: dict[str, Any] = args or {}
    parts: list[RenderableType] = [_card_header(event.tool_name, call_args, theme)]

    # Subagent tools without parseable metrics keep their dedicated
    # "✓ spawned" outcome line instead of a generic check.
    if not event.error and event.tool_name in {"agent", "agents"}:
        label = tool_label(event)
        spawned = Text("  │ ", style=theme.accent_soft)
        spawned.append("✓ ", style=theme.ok)
        spawned.append(f"spawned {label}", style=theme.ok)
        spawned.append(f"  ·  {event.duration_ms}ms", style=theme.muted)
        return Group(*parts, spawned)

    if not event.error:
        parts.extend(_outcome_body(event.tool_name, call_args, event.result, theme))

    footer_bits: list[tuple[str, str]] = []
    if event.error:
        first_line = event.error.splitlines()[0][:72]
        footer_bits.append((f"✗ {first_line}", theme.err))
    else:
        footer_bits.append(("✓", theme.ok))
    footer_bits.append((f"{event.duration_ms}ms", theme.muted))
    footer = Text("  │ ", style=theme.accent_soft)
    for i, (bit, style) in enumerate(footer_bits):
        if i:
            footer.append("  ·  ", style=theme.muted)
        footer.append(bit, style=style)
    parts.append(footer)

    return Group(*parts)


def _outcome_body(
    tool_name: str, args: dict[str, Any], result: str, theme: Theme
) -> list[RenderableType]:
    """Specialized outcome lines for the done card (empty for plain tools)."""
    if tool_name == "patch_file":
        return _diff_body(args, theme)
    if tool_name == "write_file":
        return _write_summary_body(args, theme)
    if tool_name == "bash":
        return _bash_output_body(result, theme)
    return []


def _diff_body(args: dict[str, Any], theme: Theme) -> list[RenderableType]:
    """Colored unified-diff body for a finished patch_file call."""
    path = str(args.get("path") or "(file)")
    old_content = args.get("old_content")
    new_content = args.get("new_content")
    if not isinstance(old_content, str) or not isinstance(new_content, str):
        return []

    lines = unified_diff(old_content, new_content, path)
    rendered: list[RenderableType] = []
    diff_line: str
    for i, diff_line in enumerate(lines):
        if i >= _DIFF_MAX_LINES:
            hidden = len(lines) - i
            rendered.append(
                Text(f"{_INDENT}… +{hidden} more diff lines", style=theme.muted)
            )
            break
        color = theme.ok
        if diff_line.startswith("-"):
            color = theme.err
        elif diff_line.startswith("@@"):
            color = theme.accent_soft
        rendered.append(Text(f"{_INDENT}{diff_line}", style=color))
    return rendered


def _write_summary_body(args: dict[str, Any], theme: Theme) -> list[RenderableType]:
    """``created src/x.py · 42 lines · 1.8 KB`` summary for write_file."""
    path = str(args.get("path") or "")
    content = args.get("content")
    if not isinstance(content, str):
        return []
    lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    size = len(content.encode("utf-8"))
    size_text = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"
    summary = Text(_INDENT)
    summary.append("created ", style=theme.ok)
    summary.append(path, style=f"bold {theme.text}")
    summary.append(f"  ·  {lines} lines  ·  {size_text}", style=theme.muted)
    return [summary]


def _bash_output_body(result: str, theme: Theme) -> list[RenderableType]:
    """First stdout/stderr lines for a finished bash call (timeouts included)."""
    stripped = result.strip()
    if not stripped:
        return []
    out_lines = stripped.splitlines()
    shown = out_lines[:_BASH_PREVIEW_LINES]
    rendered: list[RenderableType] = [
        Text(f"{_INDENT}{line[:100]}", style=theme.text) for line in shown
    ]
    if len(out_lines) > _BASH_PREVIEW_LINES:
        rendered.append(
            Text(
                f"{_INDENT}… +{len(out_lines) - _BASH_PREVIEW_LINES} more lines",
                style=theme.muted,
            )
        )
    return rendered


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
                        # hyperlinks=True: see render_streaming_panel above
                        # (IMPROVEMENTS.md G4, #58) — the full-screen render
                        # path carries the OSC 8 sequence through intact.
                        hyperlinks=True,
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
                            hyperlinks=True,
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


# ─── Error hints (IMPROVEMENTS.md C4) ────────────────────────────────────────

#: Actionable next step per known AgentErrorEvent code. Rendered as a
#: trailing line inside the error panel so common failures point at a fix
#: instead of a dead end.
_ERROR_HINTS: Final[dict[str, str]] = {
    "auth": "run /setup or set the provider API key env var",
    "permission": "check API key scopes and account access",
    "rate_limit": "wait a moment, or switch model with /model",
    "overloaded": "provider is busy — retry shortly or switch model",
    "server_error": "provider-side failure — retry, or switch model",
    "not_found": "model id may be wrong — pick one with /model",
    "max_iterations": "raise the budget: /config max_iterations <n>",
}


def error_hint(code: str | None) -> str | None:
    """Actionable hint for a known error code (None when unknown)."""
    if not code:
        return None
    return _ERROR_HINTS.get(code)


def format_token_indicator(used: int, window: int) -> str:
    """Short context-usage string like ``12.4k/128k`` (``?`` when unknown).

    Shared by the classic REPL's prompt line and the full-screen header —
    the two front ends must show the same number for the same state.
    """
    if window <= 0:
        return "?"

    def _fmt(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)

    return f"{_fmt(used)}/{_fmt(window)}"


__all__ = [
    "render_reasoning_panel",
    "render_assistant_label",
    "render_activity_line",
    "render_streaming_panel",
    "render_start_line",
    "render_subagent_start_line",
    "render_tool_start_line",
    "render_tool_done_line",
    "render_done_line",
    "render_error_panel",
    "render_error_notice",
    "render_user_turn",
    "render_notice",
    "render_history",
    "tool_args_preview",
    "tool_label",
    "subagent_tasks_from_args",
    "tool_verb",
    "tool_detail",
    "unified_diff",
    "error_hint",
    "format_token_indicator",
]
