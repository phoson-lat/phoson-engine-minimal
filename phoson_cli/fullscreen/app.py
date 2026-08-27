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
from collections.abc import Callable

from prompt_toolkit import Application
from prompt_toolkit.styles import Style
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.widgets import Frame, TextArea
from prompt_toolkit.completion import merge_completers
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.formatted_text import ANSI, HTML, to_formatted_text
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

from phoson_llm.schemas import REASONING_EFFORTS

from .keys import build_key_bindings, listing_for_config
from .sink import FullScreenSink
from ..repl import PhosonRepl
from ..theme import (
    VALID_NAMES,
    Theme,
    load_theme,
    build_prompt_style,
    build_picker_style_dict,
)
from .render import BlockAnsiCache, render_chat
from .._views import render_banner
from ..config import (
    PhosonConfig,
    save_config,
    enabled_providers_from_config,
)
from ..pickers import BasePicker
from ..commands import Command, CommandHandler, parse_command
from .clipboard import (
    read_clipboard_text,
    clipboard_write_hint,
    read_clipboard_image,
    write_clipboard_text,
    macos_image_tool_hint,
)
from .completer import (
    PathCompleter,
    SlashCompleter,
    ModelArgCompleter,
    ResumeArgCompleter,
    StaticArgCompleter,
    SessionsArgCompleter,
)
from .copy_range import (
    Pos,
    step_page,
    range_text,
    plain_lines,
    clamp_position,
    selection_line_span,
    apply_reverse_highlight,
)
from ..controller import MAX_RESUME_REPLAY_MESSAGES
from ..formatting import format_token_indicator
from .model_cache import ModelCache
from ..attachments import provider_compat_warning
from .command_host import FullScreenCommandHost
from .confirmation import FullScreenConfirmationService
from .session_cache import SessionListCache

_FOOTER_HINT = (
    '<style class="footer"> [Enter] Send  [Ctrl+J] New line  [PgUp/PgDn] Scroll'
    "  [Ctrl+T] Reasoning  [Ctrl+V] Paste image  [Ctrl+L] Clear"
    "  [F2] Copy  [Esc Esc] Rewind  [Ctrl+C / Ctrl+Q] Exit</style>"
)

_FOOTER_HINT_COPY = (
    '<style class="footer"> [↑/↓/←/→] Move  [PgUp/PgDn] Jump page  '
    "[Enter] Copy  [Esc] Cancel</style>"
)

# How often the subagent panel animation frame advances while active.
_SUBAGENT_TICK_SECONDS = 0.12

# Double-Esc rewind (IMPROVEMENTS.md G1): a second Esc within this window
# (measured in monotonic seconds between *delivered* key presses) opens the
# rewind picker. The window must be comfortably LARGER than prompt_toolkit's
# ``ttimeoutlen`` (0.5 s): the VT100 input layer delays delivery of a lone
# Esc by ``ttimeoutlen`` to disambiguate it from the start of an escape
# sequence (arrow keys, ``\x1b[A``, ...). As a result the *delivered*
# interval between two idle Esc presses is clamped to ~``ttimeoutlen`` from
# below regardless of how quickly the user tapped — a 0.5 s window would
# therefore miss real double-taps. 1.0 s sits well above that floor while
# staying below the gap of two deliberately separate Esc presses, so a slow
# single Esc never opens the picker. The *single* Esc cancel (#68) is
# unaffected: that binding stays eager and fires immediately while a run is
# in flight, and no double-tap state is recorded then.
_REWIND_DOUBLE_ESC_WINDOW_SECONDS = 1.0

# How often the header re-checks for AGENTS.md/CLAUDE.md memory files
# (IMPROVEMENTS.md A3) — the prompt itself re-reads them every turn; this
# cache only avoids stat-ing the filesystem on every rendered frame.
_AGENTS_MD_CACHE_SECONDS = 5.0

# Max height (in lines) the multiline input grows to before it scrolls
# internally (IMPROVEMENTS.md A2).
_INPUT_MAX_LINES = 5

# Copy mode (IMPROVEMENTS.md G3): the pane is already fully rendered to a
# width-capped ANSI string in ``_cached_ansi``; the selection range math works
# over the *untrimmed* transcript lines at that width. A page step moves the
# extending endpoint by one visible page (in lines) of the chat pane.
# Default persistent input-history file — the *same* file the classic REPL
# writes (see ``PhosonRepl.run``), so the two front ends share one history.
# Overridable per-run via ``PhosonConfig.history_file`` (used by tests).
_DEFAULT_HISTORY_FILE = Path("~/.phoson/history.txt").expanduser()


def _ansi_fragments(ansi: ANSI) -> list[tuple[str, str]]:
    """Parse *ansi* into the ``(style, text)`` shape ``copy_range`` consumes.

    ``to_formatted_text`` can emit a 3-tuple ``(style, text, mouse_handler)``
    for clickable fragments; the chat pane never uses those, so the extra
    element is dropped here at the boundary and the pure range helpers stay
    free of prompt_toolkit types.
    """
    return [(style, text) for style, text, *_ in to_formatted_text(ansi)]


def _one_line(text: str) -> str:
    """Collapse whitespace to a single line (rewind notices/previews)."""
    return " ".join(text.split())


class PhosonApp:
    """Full-screen front end over :class:`~phoson_cli.repl.PhosonRepl`."""

    def __init__(self, config: PhosonConfig) -> None:
        self.theme = load_theme(getattr(config, "theme", None))
        # Kept for _build_application (runs before self.repl exists): the
        # [keys] remap overrides (IMPROVEMENTS.md E6) come from the same
        # config object the shared REPL later wraps.
        self._config = config

        self._chat_scroll_top = 0
        self._auto_scroll = True
        self._total_chat_lines = 1
        self._cached_ansi = ANSI("")
        # The un-highlighted transcript at the current width; copy mode
        # re-derives the highlighted view from this every pass (G3).
        self._chat_base_ansi = ANSI("")
        # Plain (no-escape) lines of the current render — the coordinate space
        # for copy-mode range math and navigation.
        self._chat_plain_lines: list[str] = []
        self._cache_dirty = True
        self._last_width = 80
        # Immutable transcript blocks render to ANSI once per width (#perf).
        self._block_ansi_cache = BlockAnsiCache()
        self._run_task: asyncio.Task | None = None
        # Double-Esc rewind (IMPROVEMENTS.md G1): monotonic timestamp of the
        # last idle Esc press, and the stack of pre-rewind cursors that
        # ``undo_jump`` (Ctrl+Z) pops to restore the previous point. The
        # double-tap rides on whatever key the ``escape`` action is bound
        # to — remapping ``escape`` moves the single-Esc run cancel and
        # the double-tap together (unbinding it disables both).
        self._last_escape_at = 0.0
        self._rewind_stack: list[str] = []
        # Header AGENTS.md indicator cache (see `_has_agents_md`).
        self._agents_md_cached: bool | None = None
        self._agents_md_checked_at: float = 0.0

        # Float overlay state (pickers, confirmations). While a Float is
        # open, the base key bindings are entirely disabled and only the
        # active Float's own bindings run — see `_build_application`.
        self._active_float: Float | None = None
        self._float_kb: KeyBindings | None = None

        # Copy mode (IMPROVEMENTS.md G3, #57): the full-screen app captures
        # the mouse for the chat scroll-wheel, which removes the terminal's
        # native click-drag selection. Copy mode is the terminal-independent
        # answer: a transient overlay where the user anchors a start point,
        # extends a range with the arrows, and yanks it to the system
        # clipboard. ``_copy_anchor`` is fixed at entry; ``_copy_cursor`` is
        # the live endpoint (either order selects the span between them).
        # ``_copy_active`` gates the base bindings off and the copy bindings
        # on (see ``_build_application``).
        self._copy_active = False
        self._copy_anchor: Pos = Pos(0, 0)
        self._copy_cursor: Pos = Pos(0, 0)
        # Built once: the copy-mode key set is static (it binds to bound
        # methods that read live state), so it is created here and exposed to
        # the Application via a DynamicKeyBindings that is only active while
        # ``_copy_active`` is True (see ``_build_application``).
        self._copy_kb = self._build_copy_key_bindings()

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

        self._banner_block = render_banner(
            provider=self.repl.config.provider,
            model=self.repl.current_model,
            session_id=self.repl.tree.session_id,
            theme=self.theme,
            show_meta=False,  # shown in the header instead — not twice
        )
        self.sink.blocks.append(self._banner_block)

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
                        [*REASONING_EFFORTS, "off"],
                    ),
                    # /provider <name> — small static set, same inline
                    # autocomplete pattern as /reasoning-effort (#55).
                    StaticArgCompleter(
                        ("/provider ",),
                        lambda: enabled_providers_from_config(self.repl.config),
                    ),
                    # /theme <tier> — the four tiers are a fixed set (E4).
                    StaticArgCompleter(("/theme ",), list(VALID_NAMES)),
                    SessionsArgCompleter(self.session_cache),
                    ResumeArgCompleter(self.session_cache),
                    # @file mentions in free text (E3) — completes repo
                    # paths after a trailing "@"; the controller expands
                    # the picked path into the file's content on send.
                    # Rooted at Path.cwd() (same as the controller
                    # resolves mentions) — repl doesn't exist yet here.
                    PathCompleter(Path.cwd()),
                ]
            ),
            complete_while_typing=True,
            style="class:prompt_text",
        )

        bottom_margin = Window(height=1, char="—", style="class:separator")
        # The footer is intentionally keyboard hints only. Stable runtime
        # facts live in the compact header, avoiding duplicated UI chrome.
        # Its text is dynamic (``_get_footer_text``): the normal hint row
        # swaps for the copy-mode hints while copy mode is active (G3).
        footer_window = Window(
            content=FormattedTextControl(self._get_footer_text), height=1
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
        #
        # Copy mode (G3) needs the *same* gating: while it is active the
        # base bindings are off (so a lone arrow / Enter / Esc can't scroll
        # the chat or submit the composer underneath) and a dedicated
        # :class:`ConditionalKeyBindings` set for copy mode is on. Focus
        # moves onto the (non-focusable) chat window, so the composer's own
        # buffer bindings no longer have priority — the app-level copy
        # bindings win for Enter/arrows/Esc/Ctrl+Y (see ``enter_copy_mode``).
        base_kb = ConditionalKeyBindings(
            build_key_bindings(
                self,
                overrides=getattr(self._config, "key_bindings", None),
            ),
            Condition(lambda: self._active_float is None and not self._copy_active),
        )
        copy_kb = DynamicKeyBindings(
            lambda: self._copy_kb if self._copy_active else None
        )
        float_kb = DynamicKeyBindings(lambda: self._float_kb)
        return Application(
            layout=self._layout,
            key_bindings=merge_key_bindings([base_kb, copy_kb, float_kb]),
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

    def apply_theme(self, theme: Theme) -> None:
        """Switch the active theme at runtime (IMPROVEMENTS.md E4).

        Extends the classic :meth:`PhosonRepl.apply_theme` with the
        full-screen shell's own theme consumers: the prompt_toolkit
        style dict (chat pane, header, composer, float frames) and the
        sink (all transcript renderables read ``sink.theme`` at build
        time — existing blocks stay as they were rendered, new ones use
        the new palette). The banner block is re-rendered in place and
        the ANSI cache is dropped so the chat pane repaints cleanly.
        """
        self.theme = theme
        self.repl.apply_theme(theme)
        self.sink.theme = theme
        if self._banner_block is not None:
            index = self.sink.blocks.index(self._banner_block)
            self._banner_block = render_banner(
                provider=self.repl.config.provider,
                model=self.repl.current_model,
                session_id=self.repl.tree.session_id,
                theme=self.theme,
                show_meta=False,
            )
            self.sink.blocks[index] = self._banner_block
        self._apply_style()
        self._block_ansi_cache.clear(0)
        self.sink.dirty = True
        self.app.invalidate()

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
        # Update-available hint (IMPROVEMENTS.md E5): a dim segment at the
        # very end of the header, shown as soon as the background PyPI
        # check lands and never blocking the paint. The shared REPL is
        # the single source of truth for the check result in both
        # front ends (the TUI starts it in ``run_async``).
        update_part = f" | {repl.update_hint}" if repl.update_hint else ""
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
            f'<style class="header_dim">{update_part}</style>'
        )

    def _get_footer_text(self) -> HTML:
        """Keyboard-hint footer line.

        Normally :data:`_FOOTER_HINT`; while copy mode is active it swaps for
        :data:`_FOOTER_HINT_COPY` so the on-screen hints always describe the
        keys that currently do something (G3).
        """
        return HTML(_FOOTER_HINT_COPY if self._copy_active else _FOOTER_HINT)

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
        """Render the chat pane, highlighting the selection in copy mode (G3).

        The *base* transcript (blocks + in-flight turn) is rendered to ANSI at
        the pane width only when the sink is dirty or the width changed, and
        cached in ``_chat_base_ansi``. While copy mode is active, the selected
        rows are additionally wrapped in a reverse-video marker before the
        string is wrapped — the highlight is derived from the cached base on
        every pass (the sink is stable during copy mode), so exiting copy
        mode restores the plain view with no cache invalidation.
        """
        term_width = shutil.get_terminal_size((80, 24)).columns
        width = max(40, term_width - 4)

        if self.sink.dirty or width != self._last_width:
            text = render_chat(self.sink, width, self._block_ansi_cache)
            self._chat_base_ansi = ANSI(text)
            self._total_chat_lines = max(1, len(text.splitlines()))
            self._chat_plain_lines = plain_lines(_ansi_fragments(ANSI(text)))
            self.sink.dirty = False
            self._last_width = width

        if not self._copy_active:
            self._cached_ansi = self._chat_base_ansi
            return self._cached_ansi

        fragments = _ansi_fragments(self._chat_base_ansi)
        lines = self._chat_plain_lines
        lo, hi = selection_line_span(lines, self._copy_anchor, self._copy_cursor)
        self._cached_ansi = ANSI(apply_reverse_highlight(fragments, lo, hi))
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

    def keys_listing(self) -> list[tuple[str, str]]:
        """The effective key map for ``/keys`` (IMPROVEMENTS.md E6).

        Built from the same config object that :meth:`_build_application`
        bound from, so what the command lists is exactly what the TUI
        binds (remaps apply at startup — see ``/keys`` output).
        """
        return listing_for_config(self._config)

    def handle_escape(self) -> None:
        """Escape: cancel the in-flight run; double-tap opens the rewind.

        Precedence (G1, coordinated with #68):
        - While a run is in flight, Esc keeps its *immediate* cancel role
          (the binding is registered ``eager`` in ``keys.py`` so a double
          tap mid-run can never be swallowed as a chord) and no
          double-tap state is recorded.
        - While idle, a lone Esc still does nothing here (inside Float
          pickers they bind Esc themselves and take precedence). A second
          Esc within ``_REWIND_DOUBLE_ESC_WINDOW_SECONDS`` opens the
          rewind picker (``handle_rewind``).
        """
        if self._is_run_in_flight():
            self.repl.cancel_current()
            self.sink.notify("info", "Cancelling current run (Esc)...")
            return
        now = time.monotonic()
        if now - self._last_escape_at <= _REWIND_DOUBLE_ESC_WINDOW_SECONDS:
            self._last_escape_at = 0.0
            self.app.create_background_task(self.handle_rewind())
            return
        self._last_escape_at = now

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
        if self._active_float is not None or self._is_run_in_flight():
            return
        candidates = self.repl.jump_candidates()
        if not candidates:
            self.sink.notify("info", "Nothing to rewind to in this session.")
            return

        from ..rewind_picker import build_rewind_picker

        picker = build_rewind_picker(
            candidates, theme=self.theme, invalidate=self.app.invalidate
        )
        result = await self.run_float_picker(picker)
        if result.cancelled or result.node_id is None:
            return
        await self._apply_rewind(result.node_id)

    async def _apply_rewind(self, user_node_id: str) -> None:
        """Rewind to just before ``user_node_id`` and redraw the pane.

        Each rewind pushes the previous cursor onto ``_rewind_stack``
        (``undo_jump`` pops them back in reverse order), so consecutive
        rewinds are individually restorable with repeated Ctrl+Z.
        """
        previous_cursor = self.repl.current_node_id
        ok, info = self.repl.jump_to_user_turn(user_node_id)
        if not ok:
            self.sink.notify("error", str(info))
            return

        # The pre-rewind cursor is restorable (undo_jump) as long as the
        # session doesn't change underneath (a new/load/compact replaces
        # the tree, after which a stale node id is rejected by
        # ``jump_to_node`` and the stack entry is dropped).
        if previous_cursor is not None:
            self._rewind_stack.append(previous_cursor)
        try:
            path = self.repl.tree.get_path(self.repl.current_node_id)
        except (ValueError, AttributeError, TypeError):
            self.sink.notify("error", "Could not redraw after rewind — cursor lost.")
            return

        self._reset_transcript()
        if len(path) > MAX_RESUME_REPLAY_MESSAGES:
            self.sink.print_history(path, tail=MAX_RESUME_REPLAY_MESSAGES)
        else:
            self.sink.print_history(path)
        self.repl._context_tokens = self.repl.summarizer.estimate_tokens(path)
        self._auto_scroll = True
        self._chat_scroll_top = 0
        self.app.invalidate()

        # Re-populate the composer with the turn being rewound — editing
        # it and resending replaces the abandoned branch in one Enter.
        turn_text = self.repl.message_text(user_node_id).strip()
        if turn_text:
            self._prompt_input.text = turn_text

        self.sink.notify(
            "info",
            f"Rewound to just before “{_one_line(turn_text)[:40]}” "
            f"(cursor → {info[:8]}) — Ctrl+Z to restore, "
            "edit and Enter to re-send.",
        )

    def undo_jump(self) -> None:
        """Ctrl+Z: undo the last rewind jump (G1) and redraw to that point.

        Pops the pre-rewind cursor pushed by ``_apply_rewind`` (the
        session's continuation point) and redraws the transcript up to
        it. A plain Esc is *not* used: it would race the single-Esc run
        cancel (#68) and the picker overlays' own Esc. The binding is
        remappable (``undo_jump`` in ``[keys]``); the composer's buffer
        ignores Ctrl+Z, so the key is free.
        """
        if self._active_float is not None or self._is_run_in_flight():
            return
        if not self._rewind_stack:
            self.sink.notify("info", "No rewind to undo.")
            return
        cursor = self._rewind_stack.pop()
        ok, info = self.repl.jump_to_node(cursor)
        if not ok:
            # The tree changed underneath (new/resume/compact replaced
            # the session): every remaining stack entry is stale too, so
            # drop the whole stack instead of warning per entry.
            self._rewind_stack = []
            self.sink.notify("warn", str(info))
            return
        try:
            path = self.repl.tree.get_path(cursor)
        except (ValueError, AttributeError, TypeError):
            self.sink.notify("error", "Could not redraw — the node no longer exists.")
            self._rewind_stack = []
            return
        self._reset_transcript()
        if len(path) > MAX_RESUME_REPLAY_MESSAGES:
            self.sink.print_history(path, tail=MAX_RESUME_REPLAY_MESSAGES)
        else:
            self.sink.print_history(path)
        self.repl._context_tokens = self.repl.summarizer.estimate_tokens(path)
        self._auto_scroll = True
        self._chat_scroll_top = 0
        self.app.invalidate()
        self.sink.notify(
            "info",
            f"Back to the previous point (cursor → {info[:8]})."
            + (
                f" Ctrl+Z again — {len(self._rewind_stack)} more rewind(s) "
                "on the stack."
                if self._rewind_stack
                else ""
            ),
        )

    def _reset_transcript(self) -> None:
        """Drop the transcript and its ANSI cache, re-seeding the banner.

        Rewind/undo-redraws (G1) rebuild the pane from the tree; the
        immutable-block cache must be dropped too, otherwise old
        (discarded) block ids could shadow freshly rendered ones.
        """
        self.sink.blocks.clear()
        self._block_ansi_cache.clear(0)
        self._banner_block = render_banner(
            provider=self.repl.config.provider,
            model=self.repl.current_model,
            session_id=self.repl.tree.session_id,
            theme=self.theme,
            show_meta=False,
        )
        self.sink.blocks.append(self._banner_block)
        self.sink.dirty = True
        self.app.invalidate()

    # ── Copy mode (IMPROVEMENTS.md G3, #57) ────────────────────────────────

    def _copy_lines(self) -> list[str]:
        """The plain transcript lines copy mode navigates (cached per render)."""
        if self._chat_plain_lines:
            return self._chat_plain_lines
        return plain_lines(_ansi_fragments(self._chat_base_ansi))

    def _visible_anchor(self) -> tuple[int, int]:
        """The ``(line, col)`` of the top-left cell of the visible chat pane.

        The chat text is rendered already wrapped to the pane width, so each
        plain line is exactly one visible row and the visible window is the
        contiguous row range ``scroll .. scroll + height - 1``. The anchor
        therefore lands on the first visible line, column 0.
        """
        lines = self._copy_lines()
        if not lines:
            return (0, 0)
        scroll = self._get_effective_scroll()
        return (min(scroll, len(lines) - 1), 0)

    def _visible_cursor(self) -> Pos:
        """The ``(line, col)`` of the bottom-right cell of the visible pane.

        The starting cursor when copy mode opens — one full visible page of
        text is selected by default, so the common "copy what I'm looking at"
        case is a single Enter away.
        """
        lines = self._copy_lines()
        if not lines:
            return Pos(0, 0)
        scroll = self._get_effective_scroll()
        height = self._get_visible_window_height()
        bottom = min(scroll + height - 1, len(lines) - 1)
        return Pos(bottom, len(lines[bottom]))

    def enter_copy_mode(self) -> None:
        """F2: open copy mode — anchor at the top of the visible pane.

        The full-screen app captures the mouse for the chat scroll-wheel
        (``mouse_support=True``), which removes the terminal's native
        click-drag selection. Copy mode is the terminal-independent answer:
        the anchor is fixed here, the cursor starts at the bottom of the
        visible pane (a full page selected), and the user extends with the
        arrows before pressing Enter to yank the range.
        """
        if self._active_float is not None or self._is_run_in_flight():
            self.sink.notify(
                "info", "Not while a run is in flight or a picker is open."
            )
            return
        # Even an empty transcript renders a placeholder row, so there is
        # always something to anchor on; the recompute path is exercised by
        # the test that empties the cache.
        self._copy_anchor = Pos(*self._visible_anchor())
        self._copy_cursor = self._visible_cursor()
        self._copy_active = True
        # Move focus onto the chat window so the composer's buffer bindings
        # (which would otherwise intercept arrows/Enter/Ctrl+Y) no longer
        # have priority; the app-level copy bindings then handle everything.
        # (The base bindings are already gated off by ``_copy_active``.)
        self.app.layout.focus(self._chat_window)
        self.app.invalidate()

    def _copy_set_cursor(self, pos: Pos) -> None:
        lines = self._copy_lines()
        self._copy_cursor = clamp_position(lines, pos)
        self.app.invalidate()

    def _copy_move(self, dline: int, dcol: int) -> None:
        """Arrow: move the extending endpoint by ``dline`` rows / ``dcol`` chars.

        Left/right wrap to the end/start of the neighbouring row so a full
        line can be traversed without bouncing; up/down keep the column
        (clamped to the destination row's length).
        """
        lines = self._copy_lines()
        if not lines:
            return
        c = self._copy_cursor
        if dcol == -1:
            if c.col > 0:
                self._copy_set_cursor(Pos(c.line, c.col - 1))
            elif c.line > 0:
                self._copy_set_cursor(Pos(c.line - 1, len(lines[c.line - 1])))
            return
        if dcol == 1:
            if c.col < len(lines[c.line]):
                self._copy_set_cursor(Pos(c.line, c.col + 1))
            elif c.line < len(lines) - 1:
                self._copy_set_cursor(Pos(c.line + 1, 0))
            return
        if dline < 0:
            if c.line > 0:
                self._copy_set_cursor(Pos(c.line - 1, c.col))
            return
        if c.line < len(lines) - 1:
            self._copy_set_cursor(Pos(c.line + 1, c.col))

    def copy_move_up(self) -> None:
        self._copy_move(-1, 0)

    def copy_move_down(self) -> None:
        self._copy_move(1, 0)

    def copy_move_left(self) -> None:
        self._copy_move(0, -1)

    def copy_move_right(self) -> None:
        self._copy_move(0, 1)

    def _copy_page(self, direction: int) -> None:
        """PgUp/PgDn: jump the extending endpoint a full page (in lines)."""
        step = direction * max(1, self._get_visible_window_height())
        self._copy_set_cursor(step_page(self._copy_lines(), self._copy_cursor, step))

    def copy_page_up(self) -> None:
        self._copy_page(-1)

    def copy_page_down(self) -> None:
        self._copy_page(1)

    def _copy_to_clipboard(self) -> None:
        """Yank the current range to the system clipboard (Ctrl+Y in copy mode)."""
        lines = self._copy_lines()
        text = range_text(lines, self._copy_anchor, self._copy_cursor)
        if not text.strip():
            self.sink.notify(
                "warn", "Nothing selected (extend the range with the arrows)."
            )
            return
        self.app.create_background_task(self._copy_to_clipboard_async(text))

    async def _copy_to_clipboard_async(self, text: str) -> None:
        ok = await write_clipboard_text(text)
        if ok:
            count = len(text)
            self.sink.notify("info", f"Copied {count} characters to the clipboard.")
        else:
            hint = clipboard_write_hint()
            self.sink.notify(
                "warn",
                "Could not copy: no clipboard tool available"
                + (f" — {hint}." if hint else "."),
            )

    def exit_copy_mode(self, *, copied: bool = False) -> None:
        """Leave copy mode and restore the composer focus + base bindings.

        ``copied`` is informational (the notice was already raised by the copy
        action); the exit itself always restores focus and re-enables the base
        key bindings.
        """
        self._copy_active = False
        self.app.layout.focus(self._prompt_input)
        self.app.invalidate()

    def copy_cancel(self) -> None:
        """Esc in copy mode: leave without copying."""
        self.exit_copy_mode()

    def copy_copy(self) -> None:
        """Enter in copy mode: yank the range, then exit."""
        self._copy_to_clipboard()
        self.exit_copy_mode(copied=True)

    def _build_copy_key_bindings(self) -> KeyBindings:
        """The app-level key set active while ``_copy_active`` is True (G3).

        Merged into the Application alongside the base bindings (which are
        gated off during copy mode) and any Float. Focus is on the chat
        window, so the composer's buffer bindings are not in the active set —
        these handlers own arrows, Enter, Esc and Ctrl+Y for the duration.
        """
        kb = KeyBindings()

        def _bind(sequence: str, method: Callable[[], None]) -> None:
            # Bound handlers receive a KeyPressEvent; the copy-mode methods
            # are no-arg, so ignore the event (same pattern as keys.py).
            def _handler(_event: object, _m: Callable[[], None] = method) -> None:  # noqa: ARG001
                _m()

            kb.add(sequence)(_handler)  # type: ignore[call-overload]

        _bind("up", self.copy_move_up)
        _bind("down", self.copy_move_down)
        _bind("left", self.copy_move_left)
        _bind("right", self.copy_move_right)
        _bind("home", self._copy_move_to_line_start)
        _bind("end", self._copy_move_to_line_end)
        _bind("pageup", self.copy_page_up)
        _bind("pagedown", self.copy_page_down)
        _bind("enter", self.copy_copy)
        _bind("c-m", self.copy_copy)
        _bind("c-y", self._copy_to_clipboard)
        _bind("escape", self.copy_cancel)
        return kb

    def _copy_move_to_line_start(self) -> None:
        c = self._copy_cursor
        self._copy_set_cursor(Pos(c.line, 0))

    def _copy_move_to_line_end(self) -> None:
        lines = self._copy_lines()
        c = self._copy_cursor
        self._copy_set_cursor(Pos(c.line, len(lines[c.line]) if lines else 0))

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
        # Startup PyPI update check (IMPROVEMENTS.md E5): background, at
        # most one round trip per day, never blocks first paint. The hint
        # lands in the header as soon as the check settles; on_settle
        # invalidates so even a fully idle screen repaints it.
        self.repl.start_update_check(on_settle=self.app.invalidate)
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
