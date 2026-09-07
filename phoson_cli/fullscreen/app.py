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

import os
import re
import time
import uuid
import asyncio
import logging
import tempfile
import mimetypes
from typing import Any
from pathlib import Path
from collections.abc import Callable, Sequence, Coroutine

from prompt_toolkit import Application
from prompt_toolkit.styles import Style
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.widgets import Frame, TextArea
from prompt_toolkit.completion import merge_completers
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEvent
from prompt_toolkit.layout.layout import Layout, FocusableElement
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.containers import Float, HSplit, Window, FloatContainer
from prompt_toolkit.layout.processors import (
    Processor,
    Transformation,
    TransformationInput,
)
from prompt_toolkit.key_binding.key_bindings import (
    KeyBindings,
    DynamicKeyBindings,
    ConditionalKeyBindings,
    merge_key_bindings,
)

from phoson_agent import Choice, FormField
from phoson_llm.schemas import REASONING_EFFORTS

from .. import warnings_hook
from .keys import build_key_bindings, listing_for_config
from .sink import FullScreenSink
from ..repl import PhosonRepl
from ..theme import (
    Theme,
    load_theme,
    build_prompt_style,
    build_picker_style_dict,
)
from .floats import FloatsController

# Re-export for backwards compatibility: ``test_fullscreen_shell_unit`` imports
# the historical name ``_bash_card_rows`` from this module (moved to
# :func:`phoson_cli.fullscreen.floats.bash_card_rows` in #187).
from .floats import bash_card_rows as _bash_card_rows  # noqa: F401
from .render import BlockAnsiCache, BlockFormattedTextCache

# render_banner is no longer imported here (T-1: the banner is not injected
# into the sink). It is used by the /about command in commands.py.
from ..config import (
    PhosonConfig,
    save_config,
    enabled_providers_from_config,
)
from ..pickers import BasePicker
from ..commands import Command, CommandHandler, parse_command
from .chat_pane import (
    _PERF_LOGGER,
    ChatPane,
    ChatScrollbarMargin,
    enable_perf_counter,
)
from .clipboard import (
    read_clipboard_text,
    read_clipboard_image,
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
from .model_cache import ModelCache
from ..attachments import provider_compat_warning
from .command_host import FullScreenCommandHost
from .confirmation import FullScreenConfirmationService
from .header_model import HeaderModel
from .header_model import short_cwd as _short_cwd_impl
from .session_cache import SessionListCache
from .rewind_controller import RewindController

# Text selection (IMPROVEMENTS.md G3, #57): the chat pane sets
# ``mouse_support=True`` so the scroll wheel can be handled by the app
# (see `_on_chat_mouse`) — but enabling mouse tracking is a terminal-level
# switch (xterm DECSET 1000/1002/1006), not something prompt_toolkit or
# this app controls independently: once it is on, the terminal stops
# treating click-drag as native text selection and instead reports every
# mouse event to the app over the same channel as the wheel. There is no
# way to keep the wheel app-driven while leaving plain drag as native
# selection — it is a single on/off switch. Every mouse-aware TUI hits this
# (Claude Code's NO_FLICKER mode, Pi, OpenCode all replace native selection
# with their own drag-to-copy for the same reason, and OpenCode's issue
# tracker shows that mechanism grows its own bugs — clipboard clobbered by
# incidental selection, mouse capture stuck across SSH/tmux hops).
# The one universal escape hatch is a *terminal* feature, not an app one:
# holding Shift while dragging tells the terminal to ignore the app's mouse
# tracking for that gesture and fall back to its own native selection
# (works in GNOME Terminal, iTerm2, Alacritty, WezTerm, Ghostty, kitty,
# Windows Terminal — see each terminal's own docs for the exact modifier).
# Advertising it in the footer (rather than only in a docstring) is the
# fix: the terminal already does the work, the hint just needs to be
# discoverable.
#
# The footer itself is *contextual* (T-9): at most three hints for the
# current state, so it never truncates at 80 columns. The full cheatsheet
# (scroll, reasoning, paste image, clear, rewind, exit, Shift+Drag) lives
# in ``/keys`` and ``docs/cli/mouse-and-links.md`` — not on every frame.
# (The footer hint strings moved to ``header_model.py`` in #187.)

# How often the subagent panel animation frame advances while active.
# Kept at 0.12 s (I-84): 0.2 s made the braille spinner visibly lag
# (2 s/rotation vs 1.2 s). The streaming freeze in
# `tick_activity_frame()` — not the tick rate — is what cuts CPU.
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

# (The AGENTS.md re-check interval moved to ``header_model.py`` in #187.)

# Max height (in lines) the multiline input grows to before it scrolls
# internally (IMPROVEMENTS.md A2).
_INPUT_MAX_LINES = 5


class _ComposerPlaceholderProcessor(Processor):
    """Synchronous ``Processor`` that renders an empty-composer placeholder.

    ``TextArea`` has no ``placeholder=`` in this prompt_toolkit version, so
    the idle hint (``Ask anything · @ files · / commands``) is faked the ptk
    way: an input processor appends the hint text on every render while the
    buffer is empty and the cursor is at 0, and stops the moment the user
    types. It is styled via the ``auto-suggestion`` class (a muted tone), so
    it reads as a hint, not content — and, never becoming buffer text, it
    can't be submitted.

    An input *processor* (rather than the auto-suggestion mechanism) is used
    because ptk's ``_async_suggester`` background task only fires on text
    changes and would never populate the initial empty buffer.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    def apply_transformation(
        self, transformation_input: TransformationInput
    ) -> Transformation:
        buffer = transformation_input.buffer_control.buffer
        if (
            buffer.text == ""
            and buffer.document.cursor_position == 0
            and transformation_input.lineno
            == transformation_input.document.line_count - 1
        ):
            return Transformation(
                fragments=transformation_input.fragments
                + [("class:auto-suggestion", self._text)]
            )
        return Transformation(fragments=transformation_input.fragments)


# Default persistent input-history file — the *same* file the classic REPL
# writes (see ``PhosonRepl.run``), so the two front ends share one history.
# Overridable per-run via ``PhosonConfig.history_file`` (used by tests).
_DEFAULT_HISTORY_FILE = Path("~/.phoson/history.txt").expanduser()


def _skill_names() -> list[str]:
    """Skill names for the ``/skills <name>`` completer (G5).

    Evaluated per completion pass (``StaticArgCompleter`` accepts a
    callable) so a skill added mid-session completes without a restart.
    Discovery is a handful of ``stat`` calls, and it only runs once the
    user has typed ``/skills ``. Never raises — a broken skills directory
    must not break the composer.
    """
    from ..skills import discover_skills

    try:
        return [skill.name for skill in discover_skills()]
    except Exception:  # noqa: BLE001 - completion is best-effort
        return []


# ``_bash_card_rows`` is now :func:`phoson_cli.fullscreen.floats.bash_card_rows`
# (moved in #187); the import alias keeps the historical name importable.


class PhosonApp:
    """Full-screen front end over :class:`~phoson_cli.repl.PhosonRepl`."""

    def __init__(self, config: PhosonConfig) -> None:
        self.theme = load_theme()
        # Kept for _build_application (runs before self.repl exists): the
        # [keys] remap overrides (IMPROVEMENTS.md E6) come from the same
        # config object the shared REPL later wraps.
        self._config = config

        # Chat pane (scroll + windowed render + bounds cache), #187 slice 2.
        # Owns the pane state (see ChatPane.__init__); ``PhosonApp`` exposes
        # proxy properties so ``app._full_ansi_text`` / ``app._window_top`` /
        # … keep working for the test suite and ``apply_theme`` /
        # ``_reset_transcript``. Created before the layout: it reads
        # ``sink`` / ``_chat_window`` lazily, both of which exist by the time
        # anything renders.
        self._chat_pane = ChatPane(self)
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
        # Header permission-mode chip cache (T-6): the policy file is only
        # re-read at most once per second — the header repaints on every
        # frame and must not stat the disk each time (I-84).
        self._perm_mode_cached: str | None = None
        self._perm_mode_checked_at: float = 0.0
        # Header HTML cache (I-84): rebuilt only when an input changes.
        self._header_cache_key: tuple[str, ...] | None = None
        self._header_cache = HTML("")

        # Float overlay state (pickers, confirmations). While a Float is
        # open, the base key bindings are entirely disabled and only the
        # active Float's own bindings run — see `_build_application`.
        self._active_float: Float | None = None
        self._float_kb: KeyBindings | None = None
        # T-12: the palette opens as a background task, so ``_active_float``
        # is only set when the task runs. This synchronous flag guards the
        # window between two fast Ctrl+P presses so only one palette can
        # be scheduled.
        self._palette_open = False

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
        # Modal Float dialogs live in their own controller (#187); ``PhosonApp``
        # keeps thin delegates so the public ``run_float_*`` surface is unchanged.
        self._floats = FloatsController(self)

        self.sink = FullScreenSink(
            on_invalidate=self.app.invalidate,
            theme=self.theme,
            show_reasoning=getattr(config, "show_reasoning", True),
        )
        self.repl = PhosonRepl(
            config, sink=self.sink, confirmation=FullScreenConfirmationService(self)
        )
        self._commands = CommandHandler(self.repl, host=FullScreenCommandHost(self))
        self.apply_theme(load_theme(config.theme, registry=self.repl.theme_registry))
        # Header/footer model (#187). Created after ``self.repl`` exists (it
        # reads ``repl.session_metrics`` / ``repl.config``); the ptk controls
        # only invoke the header/footer delegates during rendering, i.e. after
        # ``__init__`` completes, so this ordering is safe.
        self._header = HeaderModel(self)
        # Rewind / undo-jump controller (#187); ``PhosonApp`` keeps thin
        # delegates so the ``keys.py`` name lookups and the test suite work.
        self._rewind = RewindController(self)

        # T-1: the banner is no longer injected into the sink. The header
        # already carries provider/model/session; the art is available via
        # /about. The empty-state hint in render_chat is the only thing
        # the user sees before their first message.
        self._banner_block = None

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
            # T-14 (#171): the pane is *windowed* — the control only ever
            # renders the visible slice (see _render_chat), so ptk's own
            # vertical scroll stays at 0 and the thumb is drawn from the
            # app's logical scroll state via the custom margin below.
            right_margins=[ChatScrollbarMargin(self._scrollbar_state)],
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
                    SlashCompleter(lambda: self.repl._controller.command_catalog),
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
                    # /theme <tier> — includes tiers contributed by plugins.
                    StaticArgCompleter(
                        ("/theme ",),
                        lambda: list(self.repl.theme_registry.valid_names()),
                    ),
                    # /skills <name> — discovered lazily per completion
                    # pass so a skill added mid-session shows up (G5).
                    StaticArgCompleter(("/skills ",), _skill_names),
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
            # T-4: the empty composer shows a dim placeholder ("Ask
            # anything · @ files · / commands") instead of a bare shell
            # prompt. prompt_toolkit's TextArea has no placeholder= here,
            # so a synchronous input processor renders it while the buffer
            # is empty (see _ComposerPlaceholderProcessor).
            input_processors=[
                _ComposerPlaceholderProcessor("Ask anything  ·  @ files  ·  / commands")
            ],
            style="class:prompt_text",
        )
        # T-4: the composer is an *object*, not a shell prompt. A single
        # rounded Frame (one separator — the top rule above) replaces the
        # old two-rule ``─``/``—`` sandwich; the ``❯`` stays *inside* the
        # box as an in-composer cue, not a leading shell glyph. The same
        # Frame the picker Floats use, so the chrome is one visual language.
        self._composer_frame = Frame(body=self._prompt_input)
        # The footer is intentionally keyboard hints only — and contextual
        # (T-9): three hints for the current state, never a truncated
        # cheatsheet. Stable runtime facts live in the compact header,
        # the full key map in /keys.
        footer_window = Window(
            content=FormattedTextControl(self._get_footer_text), height=1
        )

        main_container = HSplit(
            [
                header_window,
                self._chat_window,
                separator_line,
                self._composer_frame,
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
            build_key_bindings(
                self,
                overrides=getattr(self._config, "key_bindings", None),
            ),
            Condition(lambda: self._active_float is None),
        )
        float_kb = DynamicKeyBindings(lambda: self._float_kb)
        return Application(
            layout=self._layout,
            key_bindings=merge_key_bindings([base_kb, float_kb]),
            full_screen=True,
            mouse_support=True,
            # I-84: floor on repaint frequency so a burst of invalidations
            # coalesces into one layout/ANSI pass. Deliberately BELOW the
            # activity tick interval (0.12 s) so a spinner tick is never
            # deferred: each tick paints on its own frame and the braille
            # animates at its full 8.3 fps. Key *processing* is unaffected
            # regardless (only painting is deferred), so scroll/keys still
            # paint on the first available frame — navigation stays
            # event-driven and fluid.
            min_redraw_interval=0.035,
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
                # The composer's rounded Frame (T-4) and the picker Floats
                # share the ``frame``/``frame.border``/``frame.label``
                # classes, so one visual language covers all input chrome.
                "frame": f"bg:{self.theme.completion_bg}",
                "frame.border": self.theme.pt_accent,
                "frame.label": f"bold {self.theme.pt_accent}",
                # T-4: the empty-composer placeholder is a prompt_toolkit
                # auto-suggestion, styled in a muted tone so it reads as a
                # hint, not real content.
                "auto-suggestion": self.theme.pt_muted_deep,
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
        self._apply_style()
        self._block_ansi_cache.clear(0)
        self._block_ft_cache.clear(0)
        self._header_cache_key = None  # rebuild header for the new palette
        self.sink.dirty = True
        self.app.invalidate()

    # ── Scroll / windowed render ─────────────────────────────────────────
    # The chat pane owns its state and rendering logic (``chat_pane.ChatPane``
    # — #187 slice 2). The methods below are thin delegates so ``keys.py``
    # scroll bindings keep working; the properties proxy the pane state so the
    # test suite (``app._full_ansi_text``, ``app._window_top``, …) and
    # ``apply_theme`` / ``_reset_transcript`` are untouched.

    # --- Scroll method delegates ---

    def _get_visible_window_height(self) -> int:
        return self._chat_pane.get_visible_window_height()

    def _get_effective_scroll(self, window: Window | None = None) -> int:
        return self._chat_pane.get_effective_scroll(window)

    def _get_chat_cursor_position(self) -> Point:
        return self._chat_pane.get_chat_cursor_position()

    def scroll_page_up(self) -> None:
        self._chat_pane.scroll_page_up()

    def scroll_page_down(self) -> None:
        self._chat_pane.scroll_page_down()

    def scroll_line_up(self) -> None:
        self._chat_pane.scroll_line_up()

    def scroll_line_down(self) -> None:
        self._chat_pane.scroll_line_down()

    def scroll_home(self) -> None:
        self._chat_pane.scroll_home()

    def scroll_end(self) -> None:
        self._chat_pane.scroll_end()

    def _on_chat_mouse(self, mouse_event: MouseEvent) -> object:
        return self._chat_pane.on_chat_mouse(mouse_event)

    # --- Pane state proxies (test suite + cache resets read these directly) ---

    @property
    def _chat_scroll_top(self) -> int:
        return self._chat_pane._chat_scroll_top

    @_chat_scroll_top.setter
    def _chat_scroll_top(self, value: int) -> None:
        self._chat_pane._chat_scroll_top = value

    @property
    def _auto_scroll(self) -> bool:
        return self._chat_pane._auto_scroll

    @_auto_scroll.setter
    def _auto_scroll(self, value: bool) -> None:
        self._chat_pane._auto_scroll = value

    @property
    def _total_chat_lines(self) -> int:
        return self._chat_pane._total_chat_lines

    @_total_chat_lines.setter
    def _total_chat_lines(self, value: int) -> None:
        self._chat_pane._total_chat_lines = value

    @property
    def _cache_dirty(self) -> bool:
        return self._chat_pane._cache_dirty

    @_cache_dirty.setter
    def _cache_dirty(self, value: bool) -> None:
        self._chat_pane._cache_dirty = value

    @property
    def _last_width(self) -> int:
        return self._chat_pane._last_width

    @_last_width.setter
    def _last_width(self, value: int) -> None:
        self._chat_pane._last_width = value

    @property
    def _full_ansi_text(self) -> str:
        return self._chat_pane._full_ansi_text

    @_full_ansi_text.setter
    def _full_ansi_text(self, value: str) -> None:
        self._chat_pane._full_ansi_text = value

    @property
    def _full_ansi_bounds(self) -> list[int]:
        return self._chat_pane._full_ansi_bounds

    @_full_ansi_bounds.setter
    def _full_ansi_bounds(self, value: list[int]) -> None:
        self._chat_pane._full_ansi_bounds = value

    @property
    def _frozen_ansi_bounds(self) -> list[int]:
        return self._chat_pane._frozen_ansi_bounds

    @_frozen_ansi_bounds.setter
    def _frozen_ansi_bounds(self, value: list[int]) -> None:
        self._chat_pane._frozen_ansi_bounds = value

    @property
    def _frozen_ansi_ids(self) -> tuple[int, ...] | None:
        return self._chat_pane._frozen_ansi_ids

    @_frozen_ansi_ids.setter
    def _frozen_ansi_ids(self, value: tuple[int, ...] | None) -> None:
        self._chat_pane._frozen_ansi_ids = value

    @property
    def _chat_content_epoch(self) -> int:
        return self._chat_pane._chat_content_epoch

    @_chat_content_epoch.setter
    def _chat_content_epoch(self, value: int) -> None:
        self._chat_pane._chat_content_epoch = value

    @property
    def _window_top(self) -> int:
        return self._chat_pane._window_top

    @_window_top.setter
    def _window_top(self, value: int) -> None:
        self._chat_pane._window_top = value

    @property
    def _window_total(self) -> int:
        return self._chat_pane._window_total

    @_window_total.setter
    def _window_total(self, value: int) -> None:
        self._chat_pane._window_total = value

    @property
    def _window_height(self) -> int:
        return self._chat_pane._window_height

    @_window_height.setter
    def _window_height(self, value: int) -> None:
        self._chat_pane._window_height = value

    @property
    def _window_epoch(self) -> int:
        return self._chat_pane._window_epoch

    @_window_epoch.setter
    def _window_epoch(self, value: int) -> None:
        self._chat_pane._window_epoch = value

    @property
    def _windowed_ansi(self) -> ANSI:
        return self._chat_pane._windowed_ansi

    @_windowed_ansi.setter
    def _windowed_ansi(self, value: ANSI) -> None:
        self._chat_pane._windowed_ansi = value

    @property
    def _block_ansi_cache(self) -> BlockAnsiCache:
        return self._chat_pane._block_ansi_cache

    @property
    def _block_ft_cache(self) -> BlockFormattedTextCache:
        return self._chat_pane._block_ft_cache

    # ── Rendering ────────────────────────────────────────────────────────

    # Header / footer rendering lives in :class:`phoson_cli.fullscreen.
    # header_model.HeaderModel` (#187); the delegates below keep the ptk
    # control wiring and the test suite working. The render cache state
    # (``_header_cache`` / ``_perm_mode_cached`` / ``_agents_md_cached``)
    # stays on the app so ``cycle_permission_mode`` / ``toggle_reasoning``
    # can reset it directly.

    def _get_header_text(self) -> HTML:
        """Compact runtime header: brand · model (provider) · cwd · usage."""
        return self._header.get_header_text()

    def _permission_mode(self) -> str:
        """Current permission mode for the header chip (T-6)."""
        return self._header.permission_mode()

    def _get_footer_text(self) -> HTML:
        """Contextual footer: at most three hints for the current state."""
        return self._header.get_footer_text()

    def _has_agents_md(self) -> bool:
        """Whether any AGENTS.md/CLAUDE.md memory file applies here."""
        return self._header.has_agents_md()

    def _token_indicator(self) -> str:
        """Short token usage string like '12.4k/128k' for the header."""
        return self._header.token_indicator()

    @staticmethod
    def _short_cwd(cwd: Path) -> str:
        """Compact display path for the fixed-width header."""
        return _short_cwd_impl(cwd)

    # Render / windowing method delegates (bodies in ``ChatPane``, #187
    # slice 2).

    def _compute_chat_bounds(self, text: str, prefix_len: int, width: int) -> list[int]:
        """Per-line char offsets for *text*, built incrementally (T-14 follow-up)."""
        return self._chat_pane.compute_chat_bounds(text, prefix_len, width)

    def _render_chat(self) -> ANSI:
        """Render the visible chat window (windowed, O(visible))."""
        return self._chat_pane.render_chat()

    def _scrollbar_state(self) -> tuple[int, int]:
        """(total_lines, scroll_top) for :class:`ChatScrollbarMargin`."""
        return self._chat_pane.scrollbar_state()

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
        # T-12: a leading "!" (with the rest non-blank) is a shell command,
        # not an agent turn or a slash command.
        if text.startswith("!") and text[1:].strip():
            self._run_task = self.app.create_background_task(
                self._run_bash_line(text[1:].strip())
            )
            return
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
        count_renders = (
            enable_perf_counter(self.app) if os.environ.get("PHOSON_PERF") else None
        )
        turn_start = time.monotonic()
        renders_before = count_renders() if count_renders else 0
        try:
            await self.repl._run_agent(text)
        except asyncio.CancelledError:
            pass
        finally:
            ticker.cancel()
            self.sink.end_pending_activity()
            self.app.invalidate()
            if count_renders is not None:
                elapsed = time.monotonic() - turn_start
                renders = count_renders() - renders_before
                _PERF_LOGGER.info(
                    "perf: turn=%.1fs renders=%d avg_fps=%.1f",
                    elapsed,
                    renders,
                    renders / elapsed if elapsed > 0 else 0.0,
                )

    async def _tick_activity_indicators(self) -> None:
        """Animate the transient in-chat activity and subagent indicators."""
        while True:
            await asyncio.sleep(_SUBAGENT_TICK_SECONDS)
            activity_active = self.sink.tick_activity_frame()
            subagents_active = self.sink.tick_subagent_frame()
            if activity_active or subagents_active:
                self.sink.dirty = True
                self.app.invalidate()

    # ── T-12: command palette + `!` bash ───────────────────────────────

    def open_command_palette(self) -> None:
        """Ctrl+P: open the command palette over every slash command (T-12).

        The palette is a modal Float (like the model/theme pickers), so it
        can be opened from a calm screen and its confirm dispatches the
        chosen command through the normal ``/command`` path.
        """
        if self._active_float is not None:
            return  # a picker/confirmation is already open
        if self._is_run_in_flight():
            self.sink.notify(
                "warn",
                "A turn is already running — press Esc to cancel it first.",
            )
            return
        if self._palette_open:
            return  # a palette is already scheduled/animating open
        self._palette_open = True
        self.app.create_background_task(self._run_command_palette())

    async def _run_command_palette(self) -> None:
        """Host the palette as a background task with a synchronous guard.

        ``_active_float`` is only set when the task actually runs (the
        float is opened inside the task), so a fast second Ctrl+P before
        the first task ticks would schedule a second palette and clobber
        ``_active_float`` / ``_float_kb``. ``self._palette_open`` closes
        that window; it is released in ``finally`` so a failure path
        (e.g. no entries, exception) can't wedge the guard.
        """
        try:
            await self._run_command_palette_inner()
        finally:
            self._palette_open = False

    async def _run_command_palette_inner(self) -> None:
        from ..palette_picker import (
            PaletteEntry,
            PalettePickerResult,
            build_command_palette,
        )

        catalog = self.repl._controller.command_catalog
        entries: list[PaletteEntry] = []
        for spec in catalog.specs:
            display = " · ".join(spec.names) if len(spec.names) > 1 else spec.primary
            entries.append(
                PaletteEntry(
                    name=spec.primary,
                    display=display,
                    help=spec.help,
                )
            )
        if not entries:
            self.sink.notify("info", "No commands available.")
            return
        picker = build_command_palette(entries, theme=self.theme)
        result = await self.run_float_picker(picker)
        if not isinstance(result, PalettePickerResult):
            return
        if result.cancelled or not result.command_name:
            return
        if self._is_run_in_flight():
            # A run could have started while the float was open.
            self.sink.notify(
                "warn",
                "A turn is already running — press Esc to cancel it first.",
            )
            return
        await self._run_command(Command(name=result.command_name, args=""))

    async def _run_bash_line(self, command: str) -> None:
        """T-12: run a ``!``-prefixed shell command, respecting T-6 perms.

        The command is gated by the same bash permission policy the agent's
        bash tool uses (allow → run, ask → the T-6 confirmation card, deny →
        refused). The result is rendered as a normal bash tool card, so the
        transcript reads identically whether the agent or the user ran it.
        """
        from ..tools.bash import _run_bash
        from ..permissions_store import (
            LEVEL_ASK,
            LEVEL_DENY,
            load_policy,
        )

        policy = load_policy()
        decision = policy.check("bash", command)
        if decision == LEVEL_DENY:
            self.sink.add_bash_card(command, "", error="denied by permissions policy")
            self.app.invalidate()
            return
        if decision == LEVEL_ASK:
            allowed = await self.run_float_bash_card(
                command,
                on_always=lambda cmd: self.repl._controller._remember_bash_pattern(cmd),
            )
            if not allowed:
                self.sink.add_bash_card(command, "", error="denied by the user")
                self.app.invalidate()
                return

        started = time.monotonic()
        result = await _run_bash(command)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # Infra-level failures (spawn / timeout) are execution errors, not
        # command output — render them as an ✗ card. A non-zero exit code
        # still yields its stdout+stderr as the card body, matching how the
        # agent's bash tool reports results. These are matched by the exact
        # one-line shapes ``_run_bash`` returns (anchored fullmatch), so a
        # real command whose output merely *starts* with that phrase is not
        # misclassified as an error.
        stripped = result.strip()
        error = (
            stripped
            if (
                re.fullmatch(r"Command timed out after \d+s", stripped, re.IGNORECASE)
                or re.fullmatch(r"Failed to spawn shell: .+", stripped, re.DOTALL)
            )
            else None
        )
        self.sink.add_bash_card(command, result, duration_ms=elapsed_ms, error=error)
        self.app.invalidate()

    # ── Float overlays (pickers, confirmations) ─────────────────────────
    # Modal dialog bodies live in :class:`phoson_cli.fullscreen.floats.
    # FloatsController` (#187); the delegates below keep the public
    # ``run_float_*`` surface and the ``keys.py`` name lookups working.

    async def run_float_picker(self, picker: BasePicker) -> Any:
        """Show ``picker`` as a modal Float; return its result once resolved."""
        return await self._floats.run_float_picker(picker)

    async def run_float_confirm(self, prompt: str) -> bool:
        """Show a yes/no Float; return the answer (False on cancel/Ctrl+C)."""
        return await self._floats.run_float_confirm(prompt)

    async def run_float_bash_card(
        self,
        command: str,
        *,
        on_always: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> bool:
        """T-6: the permission card — command in monospace, 3 actions."""
        return await self._floats.run_float_bash_card(command, on_always=on_always)

    async def run_float_select(
        self, title: str, message: str, choices: Sequence[Choice]
    ) -> str | None:
        """Show a simple keyboard selector for a plugin interaction."""
        return await self._floats.run_float_select(title, message, choices)

    async def run_float_form(
        self, title: str, fields: Sequence[FormField]
    ) -> dict[str, str] | None:
        """Collect a small plugin form in a modal, never exposing widgets to plugins."""
        return await self._floats.run_float_form(title, fields)

    def _open_float(
        self, float_: Float, kb: KeyBindings, focus_target: FocusableElement
    ) -> None:
        self._floats.open_float(float_, kb, focus_target)

    def _close_float(self, float_: Float) -> None:
        self._floats.close_float(float_)

    def clear(self) -> None:
        self.sink.blocks.clear()
        self.sink.clear_reasoning_state()
        # The banner is dropped with the transcript (unlike rewind, which
        # re-seeds it): forget the reference so a later apply_theme doesn't
        # look for an object that no longer exists in the pane.
        self._banner_block = None
        self.sink.drop_error_notice()
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

    def cycle_permission_mode(self) -> None:
        """Shift+Tab (T-6): cycle the visible permission mode ask → auto.

        The mode is the durable per-tool policy (``permissions.json``);
        cycling it sets *bash*'s level, which is the tool the SOTA
        harnesses gate by default. The header chip refreshes immediately
        and the user is told the new state + how to fine-tune
        per-tool with /permissions.
        """
        from ..permissions_store import LEVEL_ASK, set_level, load_policy, save_policy

        policy = load_policy()
        current = policy.levels.get("bash")
        if current == LEVEL_ASK:
            set_level(policy, "bash", "allow")
            new_mode = "auto"
        else:
            set_level(policy, "bash", LEVEL_ASK)
            new_mode = "ask"
        save_policy(policy)
        self._perm_mode_cached = new_mode
        self._perm_mode_checked_at = time.monotonic()
        self._header_cache_key = None  # rebuild the chip on the next frame
        self.sink.notify(
            "info",
            f"Permission mode → {new_mode}"
            + (
                " — bash commands now confirm with Yes / Always / No"
                if new_mode == "ask"
                else " — bash runs freely (per-tool rules: /permissions)"
            ),
        )

    def cycle_reasoning_effort(self) -> None:
        """Ctrl+E: cycle the reasoning effort off → low → medium → high →
        xhigh → max (wraps to off).

        Mirrors the T-6 permission-mode cycle: the value lives on the
        durable config (persisted like ``/reasoning-effort``), the run
        picks it up at the *next* turn (the controller reads
        ``config.reasoning_effort`` when building each run's ModelConfig),
        the header chip refreshes immediately, and the user is told the
        new state + how to set it explicitly. Ctrl+T stays the
        show/hide toggle for the reasoning block — different axis.
        """
        current = self.repl.config.reasoning_effort
        if current not in REASONING_EFFORTS:
            current = None  # "off"
        levels = (*REASONING_EFFORTS, None)
        next_effort = levels[(levels.index(current) + 1) % len(levels)]
        self.repl.config.reasoning_effort = next_effort
        save_config(self.repl.config, only_fields={"reasoning_effort"})
        self._header_cache_key = None  # rebuild the chip on the next frame
        self.sink.notify(
            "info",
            f"Reasoning effort → {next_effort or 'off'}"
            " · applies from the next turn (explicit: /reasoning-effort)",
        )

    def keys_listing(self) -> list[tuple[str, str]]:
        """The effective key map for ``/keys`` (IMPROVEMENTS.md E6).

        Built from the same config object that :meth:`_build_application`
        bound from, so what the command lists is exactly what the TUI
        binds (remaps apply at startup — see ``/keys`` output).
        """
        return listing_for_config(self._config)

    def _is_prefixed_escape(self) -> bool:
        """True when this Esc is the *prefix* of an Alt+<key> sequence.

        Many terminals encode **Alt+<key>** as ``ESC`` + <key> (the
        Meta/Alt convention). For Alt+Backspace the bytes are
        ``0x1b 0x7f``; prompt_toolkit's VT100 parser emits them as two
        KeyPresses — ``escape`` first, then ``c-h`` (Ctrl+H, data
        ``'\\x7f'``). Because the escape binding is registered ``eager``,
        ``handle_escape`` fires for the first KeyPress while the second
        is still in ``key_processor.input_queue``.

        The heuristic (issue #108): the second key's ``data`` is the
        *original* terminal byte. For Meta-encoded keys this is a
        printable ASCII character (0x20–0x7e) or DEL (0x7f). For
        unrelated keys that merely happen to be in the queue (Ctrl+C
        = ``\\x03``, Enter = ``\\r``, another Esc = ``\\x1b``), the
        data is a control character below 0x20. We only suppress the
        Esc when the next queued key looks like a Meta-encoded payload.
        """
        processor = getattr(self.app, "key_processor", None)
        if processor is None:
            return False
        queue = getattr(processor, "input_queue", None)
        if queue is None:
            return False
        for kp in queue:
            # The _Flush sentinel is an internal marker, not a real key.
            if kp.data == "_Flush":
                continue
            # Meta/Alt encoding: the byte after ESC is in the range
            # 0x20 (space) through 0x7f (DEL). This covers:
            #   Alt+letter  → data = the letter (0x41-0x7a)
            #   Alt+digit   → data = the digit  (0x30-0x39)
            #   Alt+Backspace → data = '\\x7f' (DEL)
            # It does NOT match control characters that arrive from
            # separate key events (Ctrl+C '\\x03', Enter '\\r',
            # another Esc '\\x1b'), which are all below 0x20.
            if kp.data:
                code = ord(kp.data[0])
                if 0x20 <= code <= 0x7F:
                    return True
        return False

    def handle_escape(self) -> None:
        """Escape: cancel the in-flight run; double-tap opens the rewind.

        Precedence (G1, coordinated with #68 and #108):
        - **Prefix guard (#108):** if this Esc is the prefix of a longer
          terminal sequence (Alt+<key>), it is silently ignored — neither
          cancelling a run nor arming the double-tap window.
        - While a run is in flight, a *clean* Esc keeps its *immediate*
          cancel role (the binding is registered ``eager`` in ``keys.py``
          so a double tap mid-run can never be swallowed as a chord) and
          no double-tap state is recorded.
        - While idle, a lone clean Esc still does nothing here (inside
          Float pickers they bind Esc themselves and take precedence). A
          second clean Esc within
          ``_REWIND_DOUBLE_ESC_WINDOW_SECONDS`` opens the rewind picker
          (``handle_rewind``).
        """
        # Issue #108: Alt+Backspace (ESC 0x7f) arrives as escape + c-h
        # in the same batch. The eager handler fires for the escape while
        # c-h is still queued — that means this was NOT a deliberate Esc.
        if self._is_prefixed_escape():
            return
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

    # Rewind / undo-jump (G1) lives in :class:`phoson_cli.fullscreen.
    # rewind_controller.RewindController` (#187); the delegates below keep the
    # ``keys.py`` name lookups and the test suite (``app._rewind_stack`` /
    # ``app._apply_rewind`` / ``app._reset_transcript``) working. The
    # ``_rewind_stack`` state stays on the app.

    async def handle_rewind(self) -> None:
        """Double-Esc (idle): pick an earlier user message and rewind (G1)."""
        await self._rewind.handle_rewind()

    async def _apply_rewind(self, user_node_id: str) -> None:
        """Rewind to just before ``user_node_id`` and redraw the pane."""
        await self._rewind.apply_rewind(user_node_id)

    def undo_jump(self) -> None:
        """Ctrl+Z: undo the last rewind jump (G1) and redraw to that point."""
        self._rewind.undo_jump()

    def _reset_transcript(self) -> None:
        """Drop the transcript and its ANSI cache (see RewindController)."""
        self._rewind.reset_transcript()

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
        # Autonomous monitor wake loop (I-126): the full-screen front end
        # has its own event loop entry point (no PhosonRepl.run), so it
        # starts the loop here. No-op when enable_monitors is off.
        self.repl._controller.start_monitor_wake_loop()
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
        # ``warnings.warn(...)`` (context-window/model-listing fallbacks —
        # e.g. vLLM's /v1/models not listing the configured model id) would
        # otherwise hit the I-112 hook installed by ``main()`` and print a
        # notice to stdout, tearing the alt-screen render. Mute the hook for
        # the session; the NullHandler above absorbs the routed records.
        # ``logging.captureWarnings(True)`` additionally swaps ``showwarning``
        # for the duration of the run and restores ours on exit, so the
        # classic-mode hook stays active after the TUI closes.
        warnings_hook.set_fullscreen_active(True)
        logging.captureWarnings(True)
        try:
            await self.app.run_async()
        finally:
            logging.captureWarnings(False)
            warnings_hook.set_fullscreen_active(False)
            await self.repl.shutdown()


__all__ = ["PhosonApp"]
