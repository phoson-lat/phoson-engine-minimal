"""Pure renderable formatters (UI-toolkit agnostic).

Formatters that turn *data* into Rich renderables without any
console/spinner/Live state live here so every front end can reuse
them — the classic Renderer prints them to the terminal; a full-screen
front end can print the same objects into a buffered console.

Keep this module dependency-free of console I/O: no ``Console``, no
``print``, no ``Live``, no threads.
"""

import re
import json
import difflib
import logging
from typing import TYPE_CHECKING, Any, Final
from dataclasses import dataclass
from collections.abc import Sequence, Collection

from rich import box
from rich.rule import Rule
from rich.text import Text
from rich.panel import Panel
from rich.console import Group, RenderableType
from rich.markdown import Markdown

from phoson_agent import (
    Plugin,
    AgentDoneEvent,
    ToolRenderSpec,
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
    """Build the expanded reasoning panel (Ctrl+T post-turn).

    Kept for the classic REPL, which *prints* the full scratchpad. The
    full-screen TUI does not use this box (T-3) — it shows a single
    collapsed line instead; see :func:`render_reasoning_collapsed`.
    """
    return Panel(
        Text(reasoning, style=theme.reasoning),
        title="reasoning",
        title_align="left",
        border_style=theme.muted_deep,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def render_reasoning_collapsed(elapsed_s: float | None, theme: Theme) -> Text:
    """T-3: one muted line for a turn's thinking — the collapsed default.

    A finished turn's reasoning is metadata, not content: instead of a
    large rounded ``Panel`` (see :func:`render_reasoning_panel`, kept for
    the classic REPL) it renders as a single dim line, ``thought 8s``.
    Ctrl+T expands it *in place* (:func:`render_reasoning_expanded`), with
    no box.
    """
    if elapsed_s is None:
        return Text("  ▸ thought", style=theme.muted_deep)
    seconds = max(0, int(round(elapsed_s)))
    return Text(f"  ▸ thought {seconds}s", style=theme.muted_deep)


def render_reasoning_expanded(reasoning: str, theme: Theme) -> Text:
    """T-3: the full reasoning text, shown in place (no panel / no box).

    The in-place expansion of a collapsed ``thought Ns`` line. Indented
    like the collapsed line and rendered in the theme's muted reasoning
    color so it stays visibly secondary to the answer.
    """
    return Text(f"  {reasoning}", style=theme.reasoning)


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
    """Build the assistant response block: markdown content, no label.

    T-2: there is no per-turn "Phoson" signature — the answer renders as
    bare Markdown (plus a dim reasoning line when shown), matching how a
    human chat transcript reads rather than stamping a product name before
    every reply.

    With ``stream_plain=True`` the content renders as un-parsed text:
    the cheap path used while tokens are still arriving (perf — see the
    comment on the fast path below). Frozen/finalized turns always get
    the full Markdown render.
    """
    renderables: list[RenderableType] = []

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
    """Session/model line shown at the start of a run (T-2).

    The filled `` assistant `` badge chip is gone — the header already
    carries the model, so this is a plain ``model  session · msgs`` line.
    """
    session = (session_id or "")[:8] or "—"
    return Text.assemble(
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
    """Run summary line (cost + step count), or None when there's nothing to show.

    When the run was cut off at the model's token budget (``result.truncated``,
    F-13) a leading ``⚠ truncated`` badge is added in the warning tone so the
    user knows the answer is incomplete rather than a clean completion.
    """
    r = event.result
    parts: list[str] = []
    if r.total_cost_usd > 0:
        parts.append(f"${r.total_cost_usd:.5f}")
    steps = len(r.steps)
    parts.append(f"{steps} step{'s' if steps != 1 else ''}")
    if not parts and not r.truncated:
        return None
    line = Text()
    if r.truncated:
        line.append("  ⚠ truncated", style=theme.warn)
        if parts:
            line.append(f"  {chr(183)} ", style=theme.muted)
    line.append(f"  {chr(183)} ".join(["", *parts]), style=theme.muted)
    return line


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
    """Render a user message as a ``›`` gutter + plain text (T-2).

    The filled `` user `` badge chip is gone — a thin accent gutter reads
    as a chat speaker marker without the IM-style chip.
    """
    return Group(Text("›  ", style=theme.accent_soft), Text(text, style=theme.text))


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
    "grep": "searching files",
    "glob": "globbing files",
    "view_image": "viewing image",
    "bash": "running command",
    "skill": "loading skill",
    "web_search": "searching the web",
    "web_fetch": "fetching page",
}


@dataclass(frozen=True)
class ToolRenderRegistry:
    """Immutable per-session mapping of tool names to presentation specs."""

    specs: dict[str, ToolRenderSpec]

    def get(self, tool_name: str) -> ToolRenderSpec | None:
        return self.specs.get(tool_name)


def build_tool_render_registry(
    plugins: Sequence[Plugin], tool_names: Collection[str]
) -> ToolRenderRegistry:
    """Validate visual specs contributed by the currently loaded plugins.

    Built-in tools keep the visual treatment defined in this module. A plugin
    may only style a tool it owns, which prevents dependencies from silently
    changing the CLI's rendering of core tools.
    """
    native_names = set(_TOOL_VERBS) | {"agent", "agents"}
    available = set(tool_names)
    specs: dict[str, ToolRenderSpec] = {}
    for plugin in plugins:
        try:
            plugin_specs = plugin.get_tool_render_specs()
        except Exception as exc:
            raise ValueError(
                f"Plugin {plugin.name!r} failed while listing tool render specs: {exc}"
            ) from exc
        for spec in plugin_specs:
            if not isinstance(spec, ToolRenderSpec):
                raise ValueError(
                    f"Plugin {plugin.name!r} returned {type(spec).__name__} "
                    "instead of ToolRenderSpec"
                )
            if not spec.tool_name or spec.tool_name not in available:
                raise ValueError(
                    f"Plugin {plugin.name!r} render spec references unknown tool "
                    f"{spec.tool_name!r}"
                )
            if spec.tool_name in native_names:
                raise ValueError(
                    f"Plugin {plugin.name!r} cannot override built-in tool "
                    f"{spec.tool_name!r}"
                )
            if not spec.verb.strip() or not spec.icon.strip():
                raise ValueError(
                    f"Plugin {plugin.name!r} render spec for {spec.tool_name!r} "
                    "requires non-empty verb and icon"
                )
            if spec.tool_name in specs:
                raise ValueError(
                    f"Multiple plugins define a render spec for {spec.tool_name!r}"
                )
            specs[spec.tool_name] = spec
    return ToolRenderRegistry(specs)


#: Max rendered diff lines before truncation with an explicit notice.
_DIFF_MAX_LINES: Final[int] = 20
#: Max lines of bash output echoed in the done card.
_BASH_PREVIEW_LINES: Final[int] = 6
_INDENT: Final[str] = "      "

#: Tools whose done card renders a specialized, collapsible body (T-7).
#: A card for one of these shows the ``/details`` hint; a plain tool
#: (read_file, view_image, ...) has no body to collapse.
_BODY_TOOLS: Final[frozenset[str]] = frozenset({"patch_file", "write_file", "bash"})


def tool_verb(tool_name: str, registry: ToolRenderRegistry | None = None) -> str:
    """Human action phrase for a tool, consulting an optional session registry."""
    spec = registry.get(tool_name) if registry is not None else None
    return (
        spec.verb
        if spec is not None
        else _TOOL_VERBS.get(tool_name, tool_name.replace("_", " "))
    )


#: Per-family glyphs (T-7): one `⚙` for everything read as one action.
#: Families: read/inspect · write/edit · shell · web · skill.
_TOOL_ICONS: Final[dict[str, str]] = {
    "read_file": "📖",
    "list_dir": "📂",
    "grep": "🔍",
    "glob": "🗂",
    "view_image": "🖼",
    "write_file": "✍",
    "patch_file": "🪄",
    "bash": "⌘",
    "web_search": "🔎",
    "web_fetch": "🔗",
    "skill": "📜",
}


def tool_icon(tool_name: str, registry: ToolRenderRegistry | None = None) -> str:
    """Tool headline glyph, defaulting to the built-in gear."""
    spec = registry.get(tool_name) if registry is not None else None
    if spec is not None:
        return spec.icon
    return _TOOL_ICONS.get(tool_name, "⚙")


def tool_detail(tool_name: str, args: dict[str, Any]) -> str:
    """One-line detail of *what* is being acted on (path / command / query).

    Falls back to :func:`tool_args_preview` for tools without a dedicated
    detail extractor.
    """
    if tool_name == "bash":
        cmd = str(args.get("command") or "")
        one_line = " ".join(cmd.split())
        return one_line[:72] + ("…" if len(one_line) > 72 else "")
    if tool_name in {"grep", "glob"}:
        # The pattern is the useful detail (like bash's command), not the
        # search root — "searching files · def build_" beats "· .".
        pattern = args.get("pattern")
        if isinstance(pattern, str) and pattern:
            return pattern[:72] + ("…" if len(pattern) > 72 else "")
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


def _card_header(
    tool_name: str,
    args: dict[str, Any],
    theme: Theme,
    registry: ToolRenderRegistry | None = None,
    *,
    expandable: bool = False,
) -> Text:
    """The ``│ ⚙ <verb> · <detail>`` headline shared by start/done cards.

    File paths double as OSC 8 ``file://`` links (T-7): Rich emits the
    real hyperlink escape (``link <uri>`` in its style grammar), which
    the full-screen bridge carries through via ``osc8_passthrough`` and
    classic terminals receive natively. ``expandable`` marks cards whose
    done body was collapsed (``/details`` re-renders them).
    """
    header = Text()
    header.append("  │ ", style=theme.accent_soft)
    header.append(f"{tool_icon(tool_name, registry)} ", style=theme.accent_soft)
    header.append(tool_verb(tool_name, registry), style=f"bold {theme.accent}")
    detail = tool_detail(tool_name, args)
    if detail:
        header.append("  ·  ", style=theme.muted)
        detail_style = theme.muted
        if tool_name in {"read_file", "write_file", "patch_file"} and isinstance(
            args.get("path"), str
        ):
            from .hyperlinks import file_uri  # local: formatting stays import-light

            detail_style = f"{theme.muted} link {file_uri(args['path'])}"
        header.append(detail, style=detail_style)
    if expandable:
        header.append("  ·  /details", style=theme.muted_deep)
    return header


def render_tool_start_line(
    event: AgentToolStartEvent,
    theme: Theme,
    registry: ToolRenderRegistry | None = None,
) -> Text:
    """Compact "running tool" line for a regular (non-subagent) tool call."""
    return _card_header(event.tool_name, event.args, theme, registry)


def render_tool_done_line(
    event: AgentToolDoneEvent,
    theme: Theme,
    args: dict[str, Any] | None = None,
    registry: ToolRenderRegistry | None = None,
    *,
    collapsed: bool = False,
) -> RenderableType:
    """Result card for a finished regular (non-subagent) tool call.

    ``args`` should be the call's arguments as captured from the matching
    :class:`AgentToolStartEvent` (done events don't carry them); front ends
    remember them keyed by ``tool_call_id``. Without them the card still
    renders — just without path/command detail or specialized bodies.

    Specialized bodies: colored unified diff for ``patch_file`` (built
    purely from the ``old_content``/``new_content`` args), a created/updated
    summary for ``write_file``, first stdout lines for ``bash``.

    ``collapsed`` (T-7) suppresses those bodies — header + ✓/✗ · duration
    only — the way a transcript card reads once the work is done; the
    front end re-renders the same call uncollapsed on ``/details``.
    """
    call_args: dict[str, Any] = args or {}
    has_body = event.tool_name in _BODY_TOOLS and not event.error
    parts: list[RenderableType] = [
        _card_header(
            event.tool_name,
            call_args,
            theme,
            registry,
            expandable=has_body and not collapsed,
        )
    ]

    # Subagent tools without parseable metrics keep their dedicated
    # "✓ spawned" outcome line instead of a generic check.
    if not event.error and event.tool_name in {"agent", "agents"}:
        label = tool_label(event)
        spawned = Text("  │ ", style=theme.accent_soft)
        spawned.append("✓ ", style=theme.ok)
        spawned.append(f"spawned {label}", style=theme.ok)
        spawned.append(f"  ·  {event.duration_ms}ms", style=theme.muted)
        return Group(*parts, spawned)

    if not event.error and not collapsed:
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
        return _write_summary_body(args, result, theme)
    if tool_name == "bash":
        return _bash_output_body(result, theme)
    return []


def _diff_body(args: dict[str, Any], theme: Theme) -> list[RenderableType]:
    """Colored unified-diff body for a finished patch_file call.

    T-7: ``+``/``-`` lines carry a subtle full-line background
    (``diff_add_bg``/``diff_del_bg``) in addition to the prefix color —
    the genre's diff look. Tiers without a background token (ansi,
    system, no-color) fall back to prefix color only.
    """
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
        bg = ""
        if diff_line.startswith("+"):
            bg = theme.diff_add_bg
        elif diff_line.startswith("-"):
            bg = theme.diff_del_bg
        if diff_line.startswith("-"):
            color = theme.err if bg else theme.muted
        elif diff_line.startswith("+"):
            color = theme.ok if bg else theme.muted
        elif diff_line.startswith("@@"):
            color = theme.accent_soft
        else:
            color = theme.muted
        rendered.append(
            Text(f"{_INDENT}{diff_line}", style=color + (f" {bg}" if bg else ""))
        )
    return rendered


def _write_summary_body(
    args: dict[str, Any], result: str, theme: Theme
) -> list[RenderableType]:
    """``created src/x.py · 42 lines · 1.8 KB`` summary for write_file.

    T-7: the tool result itself now says *Created* or *Updated*
    (tools/files.py checks existence before writing) — the card mirrors
    it instead of always claiming ``created`` on an overwrite.
    """
    path = str(args.get("path") or "")
    content = args.get("content")
    if not isinstance(content, str):
        return []
    lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    size = len(content.encode("utf-8"))
    size_text = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"
    verb = "updated" if result.lstrip().lower().startswith("updated") else "created"
    summary = Text(_INDENT)
    summary.append(f"{verb} ", style=theme.ok)
    summary.append(path, style=f"bold {theme.text}")
    summary.append(f"  ·  {lines} lines  ·  {size_text}", style=theme.muted)
    return [summary]


#: Full OSC (operating-system command) sequences in raw command output —
#: window titles (``\\x1b]0;title\\x07``), XTerm colors, etc. Bash stdout is
#: the one place such bytes reach the transcript: ``Text.from_ansi`` only
#: understands SGR, so an OSC would be kept as literal text and rendered
#: through ``prompt_toolkit``'s ``ANSI()`` as visible garbage (F-42).
#: Terminated by BEL (``\\x07``) or ST (``\\x1b\\\\``); the payload must not
#: cross a newline or the next ESC.
_OSC_RE = re.compile(r"\x1b\][^\x1b\x07\n]*(?:\x07|\x1b\\)")


def _bash_output_body(result: str, theme: Theme) -> list[RenderableType]:
    """First stdout/stderr lines for a finished bash call (timeouts included).

    Bash output is terminal output: commands like ``ls --color``, ``git``,
    or scripts that set the window title emit raw SGR and OSC escapes. The
    card's ``Text`` must not carry the OSC ones — ``prompt_toolkit``'s
    ``ANSI()`` parser would render ``\\x1b]0;title\\x07`` as literal text and
    leave stray control bytes in the frozen transcript (F-42). The OSC
    sequences are stripped and the remainder is parsed with
    ``Text.from_ansi``, which keeps real colors as Rich styles; anything it
    does not understand (e.g. a truncated CSI from a torn stream) stays
    literal, which is safe.
    """
    stripped = result.strip()
    if not stripped:
        return []
    out_lines = stripped.splitlines()
    shown = out_lines[:_BASH_PREVIEW_LINES]
    rendered: list[RenderableType] = []
    for line in shown:
        clean = _OSC_RE.sub("", line[:100])
        line_text = Text.from_ansi(clean, style=theme.text)
        rendered.append(Text(_INDENT, style=theme.text) + line_text)
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

    items.append(Text(" session history ", style=theme.muted))

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
            # T-2: same › gutter as the live turn (render_user_turn), no
            # filled badge — history replay reuses the live primitives.
            items.append(Text("›  ", style=theme.accent_soft))
            if isinstance(content, str):
                items.append(Text(content, style=theme.text))
            else:
                for block in content:
                    if isinstance(block, TextBlock):
                        items.append(Text(block.text, style=theme.text))

        elif role == "assistant":
            # T-2: bare Markdown, no " assistant " badge or Rule separator
            # (matches render_streaming_panel, which no longer labels).
            if isinstance(content, str) and content.strip():
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
    "render_reasoning_collapsed",
    "render_reasoning_expanded",
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
    "tool_icon",
    "tool_detail",
    "unified_diff",
    "error_hint",
    "format_token_indicator",
]
