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

import asyncio
import logging
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

# render_banner is no longer imported here (T-1: the banner is not injected
# into the sink). It is used by the /about command in commands.py.
from ..config import (
    PhosonConfig,
    enabled_providers_from_config,
)
from ..pickers import BasePicker
from ..commands import Command, CommandHandler
from .chat_pane import (
    ChatPane,
    ChatScrollbarMargin,
)
from .clipboard import (
    paste_image_from_clipboard,
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
from .command_host import FullScreenCommandHost
from .confirmation import FullScreenConfirmationService
from .header_model import HeaderModel
from .header_model import short_cwd as _short_cwd_impl
from .state_cycles import (
    clear_transcript as _clear_transcript_impl,
)
from .state_cycles import (
    toggle_reasoning as _toggle_reasoning_impl,
)
from .state_cycles import (
    cycle_permission_mode as _cycle_permission_mode_impl,
)
from .state_cycles import (
    cycle_reasoning_effort as _cycle_reasoning_effort_impl,
)
from .session_cache import SessionListCache
from .turn_controller import (
    submit as _submit_impl,
)
from .turn_controller import (
    dispatch as _dispatch_impl,
)
from .turn_controller import (
    run_turn as _run_turn_impl,
)
from .turn_controller import (
    run_command as _run_command_impl,
)
from .turn_controller import (
    is_run_in_flight as _is_run_in_flight_impl,
)
from .turn_controller import (
    tick_activity_indicators as _tick_activity_indicators_impl,
)
from .escape_controller import (
    handle_escape as _handle_escape_impl,
)
from .escape_controller import (
    is_prefixed_escape as _is_prefixed_escape_impl,
)
from .rewind_controller import RewindController
from .palette_controller import PaletteController
from .lifecycle_controller import (
    request_exit as _request_exit_impl,
)
from .lifecycle_controller import (
    handle_ctrl_d as _handle_ctrl_d_impl,
)

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

# (Double-Esc rewind window moved to ``escape_controller.py`` in #187.)

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
        # Command palette controller (#187); see palette_controller.py.
        self._palette = PaletteController(self)

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

    # --- Pane state forwarding (tests / external callers) ---

    def __getattr__(self, name: str) -> Any:
        if "_chat_pane" in self.__dict__ and hasattr(self._chat_pane, name):
            return getattr(self._chat_pane, name)
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            name.startswith(
                (
                    "_chat_",
                    "_full_ansi_",
                    "_frozen_ansi_",
                    "_window",
                    "_total_chat_",
                    "_auto_scroll",
                    "_cache_dirty",
                    "_last_width",
                    "_block_ansi_cache",
                    "_block_ft_cache",
                )
            )
            and "_chat_pane" in self.__dict__
        ):
            setattr(self._chat_pane, name, value)
            return
        super().__setattr__(name, value)

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

        Body in :mod:`phoson_cli.fullscreen.turn_controller` (#187).
        """
        _submit_impl(self)

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

        Body in :mod:`phoson_cli.fullscreen.turn_controller` (#187).
        """
        return _is_run_in_flight_impl(self)

    async def _dispatch(self, text: str) -> None:
        await _dispatch_impl(self, text)

    async def _run_command(self, cmd: Command) -> None:
        await _run_command_impl(self, cmd)

    async def _run_turn(self, text: str) -> None:
        await _run_turn_impl(self, text)

    async def _tick_activity_indicators(self) -> None:
        """Animate the transient in-chat activity and subagent indicators."""
        await _tick_activity_indicators_impl(self)

    # ── T-12: command palette + `!` bash ───────────────────────────────

    def open_command_palette(self) -> None:
        """Ctrl+P: open the command palette over every slash command (T-12).

        Hosted by :class:`~phoson_cli.fullscreen.palette_controller.
        PaletteController` (#187); kept as a delegate for the ``keys.py``
        name lookup and the test suite.
        """
        self._palette.open()

    async def _run_bash_line(self, command: str) -> None:
        """T-12: run a ``!``-prefixed shell command via the command host."""
        await self._commands.host.run_bash_line(command)

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
        """Ctrl+L: drop the transcript and its ANSI cache."""
        _clear_transcript_impl(self)

    def toggle_reasoning(self) -> None:
        """Ctrl+T: toggle the live thinking block, or expand a past node's."""
        _toggle_reasoning_impl(self)

    def cycle_permission_mode(self) -> None:
        """Shift+Tab (T-6): cycle the visible permission mode ask → auto."""
        _cycle_permission_mode_impl(self)

    def cycle_reasoning_effort(self) -> None:
        """Ctrl+E: cycle reasoning effort off → low → medium → high → xhigh → max."""
        _cycle_reasoning_effort_impl(self)

    def keys_listing(self) -> list[tuple[str, str]]:
        """The effective key map for ``/keys`` (IMPROVEMENTS.md E6).

        Built from the same config object that :meth:`_build_application`
        bound from, so what the command lists is exactly what the TUI
        binds (remaps apply at startup — see ``/keys`` output).
        """
        return listing_for_config(self._config)

    def _is_prefixed_escape(self) -> bool:
        """True when this Esc is the *prefix* of an Alt+<key> sequence.

        Body in :mod:`phoson_cli.fullscreen.escape_controller` (#187).
        """
        return _is_prefixed_escape_impl(self)

    def handle_escape(self) -> None:
        """Escape: cancel the in-flight run; double-tap opens the rewind.

        Body in :mod:`phoson_cli.fullscreen.escape_controller` (#187).
        """
        _handle_escape_impl(self)

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

        Body in :mod:`phoson_cli.fullscreen.lifecycle_controller` (#187).
        """
        _request_exit_impl(self)

    def handle_ctrl_d(self) -> None:
        """Ctrl+D: delete-forward on a non-empty line, else quit.

        Body in :mod:`phoson_cli.fullscreen.lifecycle_controller` (#187).
        """
        _handle_ctrl_d_impl(self)

    def paste_image(self) -> None:
        """Ctrl+V: paste an image from the clipboard, or fall back to text."""
        self.app.create_background_task(paste_image_from_clipboard(self))

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def run_async(self) -> None:
        # Fire-and-forget: prefetch the model list for autocomplete without
        # delaying first paint. Plain create_task (not create_background_task)
        # since the Application isn't running yet for it to track this against.
        asyncio.create_task(self.model_cache.refresh(self.repl.config))
        asyncio.create_task(
            self.session_cache.refresh(self.repl.storage, cwd=str(Path.cwd()))
        )
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
