"""Full-screen prompt_toolkit application shell.

Layout, scrolling and rendering pattern ported from the reference
prototype (a single-window chat TUI built directly on prompt_toolkit,
with Rich rendered into a throwaway console and bridged in as ANSI
formatted text). Unlike the prototype's blocking OpenAI SDK call (run
in a daemon thread), :meth:`~phoson_cli.controller.SessionController.run_turn`
is already a native coroutine, so the agent turn runs as a background
task on the same asyncio loop as the ``Application`` — no thread, no
cross-thread marshaling, and ``Ctrl+C`` cancellation is a plain
``task.cancel()`` on that same loop.
"""

import time
import uuid
import shutil
import asyncio
import logging
import tempfile
import mimetypes
from typing import Any
from pathlib import Path

from prompt_toolkit import Application
from prompt_toolkit.styles import Style
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.widgets import Frame, TextArea
from prompt_toolkit.completion import merge_completers
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.containers import Float, HSplit, Window, FloatContainer
from prompt_toolkit.key_binding.key_bindings import (
    KeyBindings,
    DynamicKeyBindings,
    ConditionalKeyBindings,
    merge_key_bindings,
)

from .keys import build_key_bindings
from .sink import FullScreenSink
from ..repl import PhosonRepl
from ..theme import load_theme, build_prompt_style, build_picker_style_dict
from .render import BlockAnsiCache, render_chat
from .._views import render_banner
from ..config import PhosonConfig, save_config, enabled_providers_from_config
from ..pickers import BasePicker
from ..commands import Command, CommandHandler, parse_command
from .clipboard import (
    read_clipboard_text,
    read_clipboard_image,
    macos_image_tool_hint,
)
from .completer import (
    SlashCompleter,
    ModelArgCompleter,
    ResumeArgCompleter,
    StaticArgCompleter,
    SessionsArgCompleter,
)
from ..formatting import format_token_indicator
from .model_cache import ModelCache
from ..attachments import provider_compat_warning
from .command_host import FullScreenCommandHost
from .confirmation import FullScreenConfirmationService
from .session_cache import SessionListCache

_FOOTER_HINT = (
    '<style class="footer"> [Enter] Send  [Ctrl+J] New line  [PgUp/PgDn] Scroll'
    "  [Ctrl+T] Reasoning  [Ctrl+V] Paste image  [Ctrl+L] Clear"
    "  [Ctrl+C / Ctrl+Q] Exit</style>"
)

# How often the subagent panel animation frame advances while active.
_SUBAGENT_TICK_SECONDS = 0.12

# How often the header re-checks for AGENTS.md/CLAUDE.md memory files
# (IMPROVEMENTS.md A3) — the prompt itself re-reads them every turn; this
# cache only avoids stat-ing the filesystem on every rendered frame.
_AGENTS_MD_CACHE_SECONDS = 5.0

# Max height (in lines) the multiline input grows to before it scrolls
# internally (IMPROVEMENTS.md A2).
_INPUT_MAX_LINES = 5

# Default persistent input-history file — the *same* file the classic REPL
# writes (see ``PhosonRepl.run``), so the two front ends share one history.
# Overridable per-run via ``PhosonConfig.history_file`` (used by tests).
_DEFAULT_HISTORY_FILE = Path("~/.phoson/history.txt").expanduser()


class PhosonApp:
    """Full-screen front end over :class:`~phoson_cli.repl.PhosonRepl`."""

    def __init__(self, config: PhosonConfig) -> None:
        self.theme = load_theme(getattr(config, "theme", None))

        self._chat_scroll_top = 0
        self._auto_scroll = True
        self._total_chat_lines = 1
        self._cached_ansi = ANSI("")
        self._cache_dirty = True
        self._last_width = 80
        # Immutable transcript blocks render to ANSI once per width (#perf).
        self._block_ansi_cache = BlockAnsiCache()
        self._run_task: asyncio.Task | None = None
        # Header AGENTS.md indicator cache (see `_has_agents_md`).
        self._agents_md_cached: bool | None = None
        self._agents_md_checked_at: float = 0.0

        # Float overlay state (pickers, confirmations). While a Float is
        # open, the base key bindings are entirely disabled and only the
        # active Float's own bindings run — see `_build_application`.
        self._active_float: Float | None = None
        self._float_kb: KeyBindings | None = None

        # Backs inline /model autocomplete (see .completer.ModelArgCompleter)
        # — refreshed in the background, not fetched synchronously while
        # typing. Needed before `_build_layout` wires up the completer.
        self.model_cache = ModelCache()
        self.session_cache = SessionListCache()

        # Resolved input-history file for the multiline input
        # (IMPROVEMENTS.md A2) — read here so `_build_layout` (which runs
        # before the REPL exists) can wire the shared FileHistory.
        self._history_file = Path(
            getattr(config, "history_file", None) or _DEFAULT_HISTORY_FILE
        )
        self._history_file.parent.mkdir(parents=True, exist_ok=True)

        self._build_layout()
        self.app: Application = self._build_application()
        self._apply_style()

        self.sink = FullScreenSink(
            on_invalidate=self.app.invalidate,
            theme=self.theme,
            show_reasoning=getattr(config, "show_reasoning", True),
        )
        self.repl = PhosonRepl(
            config, sink=self.sink, confirmation=FullScreenConfirmationService(self)
        )
        self._commands = CommandHandler(self.repl, host=FullScreenCommandHost(self))

        self.sink.blocks.append(
            render_banner(
                provider=self.repl.config.provider,
                model=self.repl.current_model,
                session_id=self.repl.tree.session_id,
                theme=self.theme,
                show_meta=False,  # shown in the header instead — not twice
            )
        )

    # ── Layout ───────────────────────────────────────────────────────────

    def _build_layout(self) -> None:
        chat_control = FormattedTextControl(
            text=self._render_chat,
            get_cursor_position=self._get_chat_cursor_position,
            show_cursor=False,
        )
        self._chat_window = Window(
            content=chat_control,
            wrap_lines=False,
            always_hide_cursor=True,
            get_vertical_scroll=self._get_effective_scroll,
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )
        self._chat_window._mouse_handler = self._on_chat_mouse

        header_window = Window(
            content=FormattedTextControl(self._get_header_text), height=1
        )
        separator_line = Window(height=1, char="─", style="class:separator")

        self._prompt_input = TextArea(
            height=D(min=1, max=_INPUT_MAX_LINES),
            prompt="❯ ",
            multiline=True,
            # Long lines must wrap (not scroll horizontally off-screen) —
            # the default is True; an earlier port set it to False and
            # pasted/typed code disappeared past the right edge (A2).
            wrap_lines=True,
            # Take exactly the content height (capped at _INPUT_MAX_LINES)
            # and let the chat pane absorb the rest. Without this, HSplit's
            # "fill to max" pass inflates the empty composer to its max
            # height — a 5-line box around a single line (A2).
            dont_extend_height=True,
            # Shared with the classic REPL (same file) so input history
            # survives restarts and is consistent across front ends
            # (IMPROVEMENTS.md A2). Overridable via config (tests).
            history=FileHistory(str(self._history_file)),
            completer=merge_completers(
                [
                    SlashCompleter(),
                    ModelArgCompleter(self.model_cache),
                    StaticArgCompleter(
                        ("/reasoning-effort ", "/effort "),
                        ["low", "medium", "high", "off"],
                    ),
                    # /provider <name> — small static set, same inline
                    # autocomplete pattern as /reasoning-effort (#55).
                    StaticArgCompleter(
                        ("/provider ",),
                        lambda: enabled_providers_from_config(self.repl.config),
                    ),
                    SessionsArgCompleter(self.session_cache),
                    ResumeArgCompleter(self.session_cache),
                ]
            ),
            complete_while_typing=True,
            style="class:prompt_text",
        )

        bottom_margin = Window(height=1, char="—", style="class:separator")
        # The footer is intentionally keyboard hints only. Stable runtime
        # facts live in the compact header, avoiding duplicated UI chrome.
        footer_window = Window(
            content=FormattedTextControl(HTML(_FOOTER_HINT)), height=1
        )

        main_container = HSplit(
            [
                header_window,
                self._chat_window,
                separator_line,
                self._prompt_input,
                bottom_margin,
                footer_window,  # Now contains only keyboard hints
            ]
        )

        self._root_container = FloatContainer(
            content=main_container,
            floats=[
                Float(
                    xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8)
                ),
            ],
        )
        self._layout = Layout(self._root_container, focused_element=self._prompt_input)

    def _build_application(self) -> Application:
        # The base bindings (scroll, submit, exit, clear, reasoning) are
        # gated off entirely while a Float is open — a Float has no
        # independent key-binding stack, so without this a picker's Enter
        # would also trigger the chat's submit handler underneath it.
        # `DynamicKeyBindings` then layers in whichever Float is currently
        # active (`None` when idle, meaning "no extra bindings").
        base_kb = ConditionalKeyBindings(
            build_key_bindings(self), Condition(lambda: self._active_float is None)
        )
        float_kb = DynamicKeyBindings(lambda: self._float_kb)
        return Application(
            layout=self._layout,
            key_bindings=merge_key_bindings([base_kb, float_kb]),
            full_screen=True,
            mouse_support=True,
        )

    def _apply_style(self) -> None:
        self.app.style = Style.from_dict(
            {
                **build_prompt_style(self.theme),
                **build_picker_style_dict(self.theme),
                "header": f"{self.theme.pt_accent} bold",
                "header_dim": self.theme.pt_muted,
                "separator": self.theme.pt_muted_deep,
                "footer": self.theme.pt_muted_deep,
                "prompt_text": self.theme.prompt_input,
                "frame": f"bg:{self.theme.completion_bg}",
                "frame.border": self.theme.pt_accent,
                "frame.label": f"bold {self.theme.pt_accent}",
            }
        )

    # ── Scroll (ported from the reference prototype) ────────────────────

    def _get_visible_window_height(self) -> int:
        render_info = self._chat_window.render_info
        if render_info is not None:
            return max(1, render_info.window_height)
        term_lines = shutil.get_terminal_size((80, 24)).lines
        return max(1, term_lines - 5)

    def _get_effective_scroll(self, window: Window | None = None) -> int:
        visible_height = self._get_visible_window_height()
        max_scroll = max(0, self._total_chat_lines - visible_height)
        if self._auto_scroll:
            return max_scroll
        return max(0, min(self._chat_scroll_top, max_scroll))

    def _get_chat_cursor_position(self) -> Point:
        return Point(x=0, y=self._get_effective_scroll())

    def scroll_page_up(self) -> None:
        current = self._get_effective_scroll()
        self._auto_scroll = False
        step = max(5, self._get_visible_window_height() // 2)
        self._chat_scroll_top = max(0, current - step)
        self.app.invalidate()

    def scroll_page_down(self) -> None:
        current = self._get_effective_scroll()
        step = max(5, self._get_visible_window_height() // 2)
        max_scroll = max(0, self._total_chat_lines - self._get_visible_window_height())
        if current + step >= max_scroll:
            self._auto_scroll = True
            self._chat_scroll_top = max_scroll
        else:
            self._chat_scroll_top = current + step
        self.app.invalidate()

    def scroll_line_up(self) -> None:
        current = self._get_effective_scroll()
        self._auto_scroll = False
        self._chat_scroll_top = max(0, current - 2)
        self.app.invalidate()

    def scroll_line_down(self) -> None:
        current = self._get_effective_scroll()
        max_scroll = max(0, self._total_chat_lines - self._get_visible_window_height())
        if current + 2 >= max_scroll:
            self._auto_scroll = True
            self._chat_scroll_top = max_scroll
        else:
            self._chat_scroll_top = current + 2
        self.app.invalidate()

    def scroll_home(self) -> None:
        self._auto_scroll = False
        self._chat_scroll_top = 0
        self.app.invalidate()

    def scroll_end(self) -> None:
        self._auto_scroll = True
        self.app.invalidate()

    def _on_chat_mouse(self, mouse_event: MouseEvent) -> object:
        current = self._get_effective_scroll()
        max_scroll = max(0, self._total_chat_lines - self._get_visible_window_height())
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._auto_scroll = False
            self._chat_scroll_top = max(0, current - 3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            if current + 3 >= max_scroll:
                self._auto_scroll = True
                self._chat_scroll_top = max_scroll
            else:
                self._chat_scroll_top = current + 3
            return None
        return NotImplemented

    # ── Rendering ────────────────────────────────────────────────────────

    def _get_header_text(self) -> HTML:
        """Compact runtime header: brand · model (provider) · cwd · usage · status.

        The header is the single location for session facts in the
        full-screen UI. The lower line deliberately contains only keyboard
        hints, so no model/provider/cost/token/cwd value is repeated.
        """
        repl = self.repl
        cost = repl.session_metrics.total_cost_usd
        model_provider = f"{repl.current_model} ({repl.config.provider})"
        cwd = self._short_cwd(Path.cwd())
        token_cost = f"{self._token_indicator()} tok · ${cost:.4f}"

        attachments = len(repl.attachments)
        attach_part = f" · 📎{attachments}" if attachments else ""
        memory_part = " · 📄 agents.md" if self._has_agents_md() else ""
        status = self.sink.status_text()

        return HTML(
            '<style class="header"> phoson </style>'
            '<style class="header_dim"> | </style>'
            f'<style class="header_dim">{model_provider}</style>'
            '<style class="header_dim"> | </style>'
            f'<style class="header_dim">{cwd}</style>'
            '<style class="header_dim"> | </style>'
            f'<style class="header_dim">{token_cost}</style>'
            f'<style class="header_dim">{attach_part}{memory_part}</style>'
            '<style class="header_dim"> | </style>'
            f'<style class="header_dim">{status}</style>'
        )

    def _has_agents_md(self) -> bool:
        """Whether any AGENTS.md/CLAUDE.md memory file applies here.

        Cached for a short window so the header can render every frame
        without stat-ing the filesystem each time (IMPROVEMENTS.md A3).
        """
        now = time.monotonic()
        if (
            self._agents_md_cached is None
            or now - self._agents_md_checked_at > _AGENTS_MD_CACHE_SECONDS
        ):
            from ..agents_md import collect_agents_md_files

            self._agents_md_cached = bool(collect_agents_md_files())
            self._agents_md_checked_at = now
        return self._agents_md_cached

    def _token_indicator(self) -> str:
        """Short token usage string like '12.4k/128k' for the header."""
        return format_token_indicator(
            self.repl._context_tokens, self.repl._context_window
        )

    @staticmethod
    def _short_cwd(cwd: Path) -> str:
        """Compact display path for the fixed-width header."""
        parts = cwd.parts
        return str(Path(*parts[-2:])) if len(parts) > 2 else str(cwd)

    def _render_chat(self) -> ANSI:
        term_width = shutil.get_terminal_size((80, 24)).columns
        width = max(40, term_width - 4)

        if self.sink.dirty or width != self._last_width:
            text = render_chat(self.sink, width, self._block_ansi_cache)
            self._cached_ansi = ANSI(text)
            self._total_chat_lines = max(1, len(text.splitlines()))
            self.sink.dirty = False
            self._last_width = width

        return self._cached_ansi

    # ── Input handling ───────────────────────────────────────────────────

    def submit(self) -> None:
        """Handle Enter on the input line: dispatch a command or an agent turn.

        While a turn is already in flight the input is *kept* (not cleared)
        and the user is told why nothing happened — otherwise pressing Enter
        looks like the app froze (IMPROVEMENTS.md A4). The header already
        shows the live status ("Streaming" / "Running tool") so the user can
        see the turn is still going.
        """
        text = self._prompt_input.text
        if not text.strip():
            return
        if self._is_run_in_flight():
            self.sink.notify(
                "warn",
                "A turn is already running — press Esc to cancel it first. "
                "Your text is kept.",
            )
            return
        # Persist to the input history. The custom submit path bypasses the
        # buffer's ``accept_handler`` (which normally does this), so it must
        # be spelled out (IMPROVEMENTS.md A2).
        self._prompt_input.buffer.append_to_history()
        self._prompt_input.text = ""
        self._auto_scroll = True
        self._run_task = self.app.create_background_task(self._dispatch(text))

    def insert_newline(self) -> None:
        """Ctrl+J: insert a newline in the multiline input (IMPROVEMENTS.md A2).

        Shift+Enter is not portable (terminals emit CSI-u sequences that
        prompt_toolkit's VT100 parser does not map, so it arrives as literal
        garbage), so ``Ctrl+J`` — a single universal byte — is the newline
        key. It overrides prompt_toolkit's default ``c-j``→Enter remap.
        """
        self._prompt_input.buffer.newline(copy_margin=False)

    def _is_run_in_flight(self) -> bool:
        """True from the moment Enter is pressed until the turn fully settles.

        Guards against a second submission overlapping the first (which
        would race two mutations of the same tree/session state) —
        including the brief window after the visible answer is already
        rendered but ``run_turn`` is still persisting it. For "should
        Ctrl+C/Ctrl+Q interrupt something visible" use
        ``sink.current_turn is not None`` instead (see ``request_exit``)
        — that invisible trailing save is not cancel-worthy.
        """
        return self._run_task is not None and not self._run_task.done()

    async def _dispatch(self, text: str) -> None:
        cmd = parse_command(text)
        if cmd is not None:
            await self._run_command(cmd)
        else:
            await self._run_turn(text)

    async def _run_command(self, cmd: Command) -> None:
        should_continue = await self._commands.handle(cmd)
        self.app.invalidate()
        if cmd.name in {"/model", "/subagent-model", "/provider"}:
            # The available (or current-marked) model set may have just
            # changed — refresh in the background so autocomplete stays
            # accurate without blocking on another network round trip.
            self.app.create_background_task(self.model_cache.refresh(self.repl.config))
        if cmd.name in {"/sessions", "/new", "/delete"}:
            # Session list may have changed (load/new/delete) — refresh the
            # /sessions autocomplete cache in the background as well.
            self.app.create_background_task(
                self.session_cache.refresh(self.repl.storage)
            )
        if not should_continue:
            self.app.exit()

    async def _run_turn(self, text: str) -> None:
        # Start feedback before the controller/provider can emit its first
        # AgentStartEvent. This removes the otherwise silent post-Enter gap.
        self.sink.begin_activity()
        ticker = self.app.create_background_task(self._tick_activity_indicators())
        try:
            await self.repl._run_agent(text)
        except asyncio.CancelledError:
            pass
        finally:
            ticker.cancel()
            self.sink.end_pending_activity()
            self.app.invalidate()

    async def _tick_activity_indicators(self) -> None:
        """Animate the transient in-chat activity and subagent indicators."""
        while True:
            await asyncio.sleep(_SUBAGENT_TICK_SECONDS)
            activity_active = self.sink.tick_activity_frame()
            subagents_active = self.sink.tick_subagent_frame()
            if activity_active or subagents_active:
                self.sink.dirty = True
                self.app.invalidate()

    # ── Float overlays (pickers, confirmations) ─────────────────────────

    async def run_float_picker(self, picker: BasePicker) -> Any:
        """Show ``picker`` as a modal Float; return its result once resolved."""
        result_future: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_done(result: object) -> None:
            if not result_future.done():
                result_future.set_result(result)

        picker._on_done = on_done
        picker._invalidate = self.app.invalidate

        float_ = picker.as_float()
        self._open_float(float_, picker._kb, picker._window)
        try:
            return await result_future
        finally:
            self._close_float(float_)

    async def run_float_confirm(self, prompt: str) -> bool:
        """Show a yes/no Float; return the answer (False on cancel/Ctrl+C).

        Resolving "no" on Ctrl+C (rather than leaving it unhandled) matters
        once this is reused for the bash safe-mode confirmation: cancelling
        the run must not leave the awaiting tool call hanging on a Float
        nobody can answer anymore.
        """
        result_future: asyncio.Future = asyncio.get_running_loop().create_future()

        def resolve(answer: bool) -> None:
            if not result_future.done():
                result_future.set_result(answer)

        kb = KeyBindings()
        kb.add("y")(lambda event: resolve(True))  # noqa: ARG005
        kb.add("Y")(lambda event: resolve(True))  # noqa: ARG005
        kb.add("n")(lambda event: resolve(False))  # noqa: ARG005
        kb.add("N")(lambda event: resolve(False))  # noqa: ARG005
        kb.add("escape")(lambda event: resolve(False))  # noqa: ARG005
        kb.add("c-c")(lambda event: resolve(False))  # noqa: ARG005

        window = Window(
            content=FormattedTextControl(
                lambda: [
                    ("class:title", f"  {prompt}\n\n"),
                    ("class:footer", "  [y] Yes    [n] No / Esc\n"),
                ],
                focusable=True,
            ),
            always_hide_cursor=True,
        )
        float_ = Float(content=Frame(window), left=4, right=4, top=4, bottom=4)

        self._open_float(float_, kb, window)
        try:
            return await result_future
        finally:
            self._close_float(float_)

    def _open_float(self, float_: Float, kb: KeyBindings, focus_target: Window) -> None:
        self._root_container.floats.append(float_)
        self._float_kb = kb
        self._active_float = float_
        self.app.layout.focus(focus_target)
        self.app.invalidate()

    def _close_float(self, float_: Float) -> None:
        if float_ in self._root_container.floats:
            self._root_container.floats.remove(float_)
        self._float_kb = None
        self._active_float = None
        self.app.layout.focus(self._prompt_input)
        self.app.invalidate()

    def clear(self) -> None:
        self.sink.blocks.clear()
        self.sink.dirty = True
        self._auto_scroll = True
        self._chat_scroll_top = 0
        self.app.invalidate()

    def toggle_reasoning(self) -> None:
        """Ctrl+T: toggle the live thinking block, or expand a past node's.

        While streaming, toggles the in-progress reasoning panel. Once
        idle, expands the reasoning of the newest node on the current
        path that has any — a node's reasoning is shown at most once per
        session (the transcript is append-only).
        """
        if self.sink.current_turn is not None:
            new_state = self.sink.toggle_live_reasoning()
            # Persist the default for future turns/sessions (#50).
            if getattr(self.repl.config, "show_reasoning", True) != new_state:
                self.repl.config.show_reasoning = new_state
                save_config(self.repl.config, only_fields={"show_reasoning"})
                self.sink.show_reasoning_default = new_state
            return

        cursor: str | None = self.repl.current_node_id
        path_ids: list[str] = []
        while cursor is not None:
            path_ids.append(cursor)
            node = self.repl.tree.nodes.get(cursor)
            cursor = node.parent_id if node is not None else None
        path_ids.reverse()

        for node_id in path_ids:
            node = self.repl.tree.nodes.get(node_id)
            reasoning = node.metadata.get("reasoning") if node else None
            if not reasoning:
                continue
            if node_id in self.repl._expanded_reasoning:
                self.sink.notify(
                    "info",
                    "Reasoning already expanded (the transcript is append-only).",
                )
                return
            self.repl._expanded_reasoning.add(node_id)
            self.sink.expand_reasoning(str(reasoning))
            return

    def handle_escape(self) -> None:
        """Escape: cancel the in-flight run; do nothing when idle.

        Only fires while a run is actually streaming (``_is_run_in_flight``
        covers the whole dispatch, including the invisible trailing save —
        but cancelling during that window is a harmless no-op because the
        controller's cancel path is idempotent once the stream task is
        done). When idle, Esc keeps its overlay-cancel role inside Float
        pickers (which bind it separately and take precedence) and does
        nothing here, so dismissing an autocomplete never kills a turn.
        """
        if self._is_run_in_flight():
            self.repl.cancel_current()
            self.sink.notify("info", "Cancelling current run (Esc)...")

    def request_exit(self) -> None:
        """Ctrl+C/Ctrl+Q: interrupt a visible turn, or quit.

        ``sink.current_turn`` is set exactly while there is something
        the user can see happening (tokens, a running tool, a tool
        awaiting confirmation) — and, because ``AgentDoneEvent``/
        ``AgentErrorEvent`` are dispatched to the sink from inside the
        same stream-consumption task ``is_running`` reflects, the two
        become False together. There is no window where content is
        still visibly streaming but ``is_running`` has already gone
        False, so ``cancel_current()`` is always effective here.

        Once the turn's content is fully rendered, only invisible
        trailing bookkeeping remains (persisting reasoning, saving the
        session) — not cancel-worthy, so this just quits; a pending
        background task gets cancelled for free by the Application
        shutting down.
        """
        if self.sink.current_turn is not None:
            self.repl.cancel_current()
            return
        self.app.exit()

    def handle_ctrl_d(self) -> None:
        """Ctrl+D: delete-forward on a non-empty line, else quit.

        Unlike ``PromptSession`` (where an empty-buffer Ctrl+D raises
        ``EOFError`` for free), ``TextArea`` has no such behavior built
        in, so this is spelled out explicitly. Routed through
        ``request_exit`` rather than an unconditional quit so it stays
        consistent with Ctrl+C/Ctrl+Q (interrupts a visible turn first).
        """
        if self._prompt_input.text:
            self._prompt_input.buffer.delete()
        else:
            self.request_exit()

    def paste_image(self) -> None:
        """Ctrl+V: paste an image from the clipboard, or fall back to text.

        Terminals only ever deliver *text* through their own paste
        mechanism — an image copied to the OS clipboard (e.g. from a
        screenshot tool or a browser) has to be read from the clipboard
        directly (``clipboard.read_clipboard_image``, shelling out to
        wl-paste/xclip/pngpaste) rather than anything a paste keystroke
        could hand the ``TextArea``. Ctrl+V is rebound globally to this
        handler, which would otherwise swallow the ``TextArea``'s native
        text paste (IMPROVEMENTS.md D3): when the clipboard holds no
        image, the clipboard's *text* is read the same way and inserted
        at the cursor instead, so Ctrl+V still works for plain text.
        """
        self.app.create_background_task(self._paste_image_async())

    async def _paste_image_async(self) -> None:
        result = await read_clipboard_image()
        if result is None:
            await self._paste_text_fallback()
            return

        data, mime = result
        suffix = mimetypes.guess_extension(mime) or ".png"
        target_dir = Path(tempfile.gettempdir()) / "phoson-clipboard"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"clipboard-{uuid.uuid4().hex[:8]}{suffix}"
        target.write_bytes(data)

        try:
            self.repl.attachments.attach(str(target))
        except (FileNotFoundError, ValueError) as exc:
            self.sink.notify("error", str(exc))
            return

        suffix = target.suffix.lower()
        warning = provider_compat_warning(
            suffix, getattr(self.repl.config, "provider", None)
        )
        if warning:
            self.sink.notify("warn", warning)

        # Terminal chat inputs can't show a real thumbnail chip — a text
        # placeholder inserted at the cursor is the next best thing: it
        # marks where the image was pasted, and (since it ends up as
        # ordinary text in the message) doubles as an inline reference
        # both the user and the model can read.
        placeholder = f"[image #{len(self.repl.attachments)}] "
        self._prompt_input.buffer.insert_text(placeholder)
        self.app.invalidate()

    async def _paste_text_fallback(self) -> None:
        """No image on the clipboard: paste its text instead (D3), if any.

        Reading via the same platform tool as the image path (rather
        than relying on the terminal's own paste) is what lets this
        double as the "clipboard has text, not an image" case instead
        of silently doing nothing.
        """
        text = await read_clipboard_text()
        if text:
            self._prompt_input.buffer.insert_text(text)
            self.app.invalidate()
            return

        message = "No image on the clipboard (or no clipboard tool available)."
        hint = macos_image_tool_hint()
        if hint:
            message = f"{message} {hint}."
        self.sink.notify("warn", message)

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def run_async(self) -> None:
        # Fire-and-forget: prefetch the model list for autocomplete without
        # delaying first paint. Plain create_task (not create_background_task)
        # since the Application isn't running yet for it to track this against.
        asyncio.create_task(self.model_cache.refresh(self.repl.config))
        asyncio.create_task(self.session_cache.refresh(self.repl.storage))
        # While the full-screen TUI is up, any library/app logger without
        # configured handlers would hit logging's "last resort" handler
        # and print raw warnings over the rendered UI (seen with sub-agent
        # fallbacks). Silence that path for the duration of the session;
        # libraries still emit records for real handler setups.
        logging.getLogger().handlers.append(logging.NullHandler())
        logging.getLogger().propagate = False
        try:
            await self.app.run_async()
        finally:
            await self.repl.shutdown()


__all__ = ["PhosonApp"]
