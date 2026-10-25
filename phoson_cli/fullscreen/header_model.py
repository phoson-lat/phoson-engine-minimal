"""Header / footer rendering for the full-screen front end (issue #187).

Extracted from ``app.py`` to keep ``PhosonApp`` focused on layout, scroll and
lifecycle.  The header/footer logic is unchanged — this is a move, not a
rewrite.

The render *cache* state (``_header_cache`` / ``_header_cache_key`` /
``_perm_mode_cached`` / ``_agents_md_cached`` and their timestamps) stays on
the owning ``PhosonApp``: ``cycle_permission_mode`` and ``toggle_reasoning``
reset the header cache directly, and the test suite asserts object identity
on ``app._header_cache``.  The controller reads and writes that state through
``app``.

``PhosonApp._get_header_text`` / ``_get_footer_text`` remain thin delegates so
the prompt_toolkit controls (wired in ``_build_layout``) and the test suite
are untouched.
"""

import time
import shutil
from html import escape
from pathlib import Path
from dataclasses import dataclass

from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.formatted_text import HTML

from phoson_llm.schemas import REASONING_EFFORTS

from ..formatting import format_token_indicator
from .lifecycle_controller import exit_armed

_AGENTS_MD_CACHE_SECONDS = 5.0
_FOOTER_HINT_IDLE = "enter send  ·  ctrl+j newline  ·  / commands"
# While a turn runs the composer is still live: Enter queues the draft and
# run-safe commands (and Ctrl+P) remain available — see `turn_controller`.
_FOOTER_HINT_RUNNING = "esc cancel  ·  enter queue  ·  ctrl+p commands"
_FOOTER_HINT_PICKER = "enter  ·  esc"
_FOOTER_HINT_SHELL = "enter run  ·  esc / ⌫ exit shell"
_FOOTER_HINT_EXIT_ARMED = "press ctrl+c again to exit"


def short_cwd(cwd: Path) -> str:
    """Compact display path for the header: ``~`` for home, last two parts."""
    home = Path.home()
    if cwd == home:
        return "~"
    if cwd.parent == home:
        return f"~/{cwd.name}"
    parts = cwd.parts
    return str(Path(*parts[-2:])) if len(parts) > 2 else str(cwd)


_HEADER_BRAND = (
    '<style class="header_dim">* </style><style class="header">phoson</style>'
)
_HEADER_BRAND_WIDTH = len("* phoson")
_HEADER_SEP = " · "
#: Segments dropped first when the header does not fit, least important
#: first. The context gauge, permission mode and effort are never dropped.
_HEADER_DROP_ORDER = (
    "agents_md",
    "update",
    "monitors",
    "cwd",
    "cost",
    "id",
    "attach",
    "title",
)
#: A long title is shortened to this many cells before anything else goes.
_HEADER_TITLE_MAX = 40
#: …and, once lower-priority segments are gone, down to this before dropping.
_HEADER_TITLE_MIN = 12
#: Context-gauge thresholds (% of the window): warn tone, then error tone
#: with a ``/compact`` nudge.
_CONTEXT_WARN_PCT = 70
_CONTEXT_CRITICAL_PCT = 90


@dataclass
class _Segment:
    key: str
    text: str
    style: str
    #: Joined to the previous segment with a space instead of `` · ``.
    glue: bool = False
    #: Can be shortened with an ellipsis (the session title).
    shrink: bool = False


def _context_segment(used: int, window: int) -> _Segment:
    """``38K/128K 30%``, toned by how full the context window is."""
    indicator = format_token_indicator(used, window)
    if window <= 0:
        return _Segment("context", f"{indicator} tok", "header_dim")
    pct = round(100 * used / window)
    if pct >= _CONTEXT_CRITICAL_PCT:
        return _Segment("context", f"{indicator} {pct}% /compact", "header_err")
    style = "header_warn" if pct >= _CONTEXT_WARN_PCT else "header_dim"
    return _Segment("context", f"{indicator} {pct}%", style)


def _shorten(text: str, cells: int) -> str:
    if get_cwidth(text) <= cells:
        return text
    out = ""
    for char in text:
        if get_cwidth(out + char) > cells - 1:
            break
        out += char
    return out.rstrip() + "…"


def _segments_width(segments: list[_Segment]) -> int:
    width = _HEADER_BRAND_WIDTH
    for segment in segments:
        width += (1 if segment.glue else len(_HEADER_SEP)) + get_cwidth(segment.text)
    return width


def _fit_segments(segments: list[_Segment], width: int) -> list[_Segment]:
    """Shorten/drop segments by priority until the header fits *width*.

    The title is first capped at :data:`_HEADER_TITLE_MAX`; then segments
    go in :data:`_HEADER_DROP_ORDER`, except that the title is squeezed to
    :data:`_HEADER_TITLE_MIN` before it is dropped. Whatever still overflows
    (a very narrow terminal) is clipped by the window, as before.
    """
    fitted = [
        _Segment(s.key, _shorten(s.text, _HEADER_TITLE_MAX), s.style, s.glue, s.shrink)
        if s.shrink
        else s
        for s in segments
    ]
    for key in _HEADER_DROP_ORDER:
        if _segments_width(fitted) <= width:
            break
        if key == "title":
            for segment in fitted:
                if segment.shrink:
                    overflow = _segments_width(fitted) - width
                    room = max(_HEADER_TITLE_MIN, get_cwidth(segment.text) - overflow)
                    segment.text = _shorten(segment.text, room)
            if _segments_width(fitted) <= width:
                break
        fitted = [s for s in fitted if s.key != key]
    return fitted


def _segments_html(segments: list[_Segment]) -> str:
    parts = [_HEADER_BRAND]
    for segment in segments:
        sep = " " if segment.glue else _HEADER_SEP
        parts.append(f'<style class="header_dim">{sep}</style>')
        parts.append(
            f'<style class="{segment.style}">{escape(segment.text, quote=True)}</style>'
        )
    return "".join(parts)


class HeaderModel:
    """Owns the header/footer rendering and its per-frame caches.

    References the owning ``PhosonApp`` (``app``) for ``session`` / ``sink`` and
    the shared cache state.  See the module docstring for why the state lives
    on the app rather than here.
    """

    def __init__(self, app) -> None:
        self.app = app

    # ── Header ─────────────────────────────────────────────────────────────

    def get_header_text(self) -> HTML:
        """Compact runtime header: brand · title (id) · cwd · usage · flags.

        The header holds durable session facts only (identity, cwd,
        tokens/cost, permission mode, reasoning effort). The **model/provider
        lives in the footer** (next to the key hints), and live activity
        status is shown exclusively by the in-chat activity line — the header
        carries no transient state, so it stays stable during a turn.

        It is fitted to the terminal width by priority (:func:`_fit_segments`)
        instead of being clipped on the right, so the permission mode and
        effort — the facts that change what the agent will do — are the
        last to go.

        I-84: the HTML string is cached and only rebuilt when one of its
        inputs changes — repainting the chat for a spinner glyph must not
        re-stat the filesystem or reformat the header on every frame.
        """
        app = self.app
        session = app.session
        cost = session.session_metrics.total_cost_usd
        # Session identity (title + short id) is only shown once the session
        # has actually begun (first message / resume) — a fresh controller
        # has an in-memory id but no session yet.
        if session.controller.session_started:
            short_id = (session.tree.session_id or "")[:8] or "—"
            title = (session.tree.title or "").strip() or "untitled"
        else:
            short_id = title = ""
        perm_mode = self.permission_mode()
        effort = session.config.reasoning_effort
        key = (
            short_cwd(Path.cwd()),
            title,
            short_id,
            session.context_tokens,
            session.context_window,
            f"{cost:.4f}",
            len(session.attachments),
            self.has_agents_md(),
            session.controller.monitor_status() or "",
            session.update_hint or "",
            perm_mode,
            effort or "",  # None (off) and "" hash identically
            self._footer_width(),
        )
        if app._header_cache_key != key:
            app._header_cache_key = key
            app._header_cache = HTML(self._build_header(*key))
        return app._header_cache

    def _build_header(
        self,
        cwd: str,
        title: str,
        short_id: str,
        used: int,
        window: int,
        cost: str,
        attachments: int,
        agents_md: bool,
        monitors: str,
        update_hint: str,
        perm_mode: str,
        effort: str,
        width: int,
    ) -> str:
        """The header HTML for these inputs, fitted to *width* columns."""
        segments: list[_Segment] = []
        if short_id:
            segments.append(_Segment("title", title, "header", shrink=True))
            segments.append(_Segment("id", f"({short_id})", "header_dim", glue=True))
        segments.append(_Segment("cwd", cwd, "header_dim"))
        segments.append(_context_segment(used, window))
        # T-2: cost only when > 0 — a fresh session shows no $0.0000 noise.
        if float(cost) > 0:
            segments.append(_Segment("cost", f"${cost}", "header_dim"))
        # Permission-mode chip (T-6): accent for *ask* (confirmations are
        # coming), dim for auto. Effort chip (Ctrl+E): accent when set.
        segments.append(
            _Segment(
                "mode", perm_mode, "header" if perm_mode == "ask" else "header_dim"
            )
        )
        segments.append(
            _Segment("effort", f"effort {effort}", "header")
            if effort in REASONING_EFFORTS
            else _Segment("effort", "effort off", "header_dim")
        )
        if attachments:
            segments.append(_Segment("attach", f"📎{attachments}", "header_dim"))
        if agents_md:
            segments.append(_Segment("agents_md", "📄 agents.md", "header_dim"))
        # Active-monitors indicator (I-126), reported by the plugin.
        if monitors:
            segments.append(_Segment("monitors", monitors, "header_dim"))
        # Update-available hint (IMPROVEMENTS.md E5), set by the background
        # PyPI check.
        if update_hint:
            segments.append(_Segment("update", update_hint, "header_dim"))
        return _segments_html(_fit_segments(segments, width))

    def permission_mode(self) -> str:
        """Current permission mode for the header chip (T-6).

        ``ask`` when the durable policy puts bash on the ask level,
        ``auto`` otherwise (allow is the default for unlisted tools).
        The policy file is re-read at most once per second; Shift+Tab
        (``cycle_permission_mode``) refreshes it immediately.
        """
        app = self.app
        now = time.monotonic()
        if app._perm_mode_cached is None or now - app._perm_mode_checked_at >= 1.0:
            from ..permissions_store import load_policy

            app._perm_mode_cached = (
                "ask" if load_policy().levels.get("bash") == "ask" else "auto"
            )
            app._perm_mode_checked_at = now
        return app._perm_mode_cached

    # ── Footer ─────────────────────────────────────────────────────────────

    def get_footer_text(self) -> HTML:
        """Model + queue on the left, contextual key hints on the right.

        The model moved here from the header: the header holds only durable
        session facts, while the footer pairs the active model/provider with
        the at-most-three state-dependent hints (T-9). The two are pushed to
        opposite edges with a computed run of spaces, so the model never
        displaces the hints on a normal-width terminal.

        On a terminal too narrow for both, the separator falls back to a dim
        ``·`` and the window clips the tail — never wraps the footer into the
        chat (the key map is always available via ``/keys``).
        """
        app = self.app
        if exit_armed(app):
            hint = _FOOTER_HINT_EXIT_ARMED
        elif app._active_float is not None:
            hint = _FOOTER_HINT_PICKER
        elif getattr(app, "_shell_mode", False):
            hint = _FOOTER_HINT_SHELL
        elif app._is_run_in_flight():
            hint = _FOOTER_HINT_RUNNING
        else:
            hint = _FOOTER_HINT_IDLE

        session = app.session
        left = f"{session.current_model} ({session.config.provider})"
        # Queued-message indicator: messages typed during a turn are sent in
        # order as each turn settles (`turn_controller.enqueue_turn`). Kept in
        # the footer now that the header has no transient state.
        queued = len(getattr(app, "_pending_turns", ()))
        if queued:
            left = f"{left} · {queued} queued"
        # Shell-mode outputs ride along with the next message (``!``).
        shell = getattr(session.controller, "pending_shell_count", 0)
        if shell:
            noun = "output" if shell == 1 else "outputs"
            left = f"{left} · {shell} shell {noun} → next message"

        gap = self._footer_width() - len(left) - len(hint) - 1
        spacer = " " * gap if gap >= 1 else "  ·  "
        return HTML(
            f'<style class="footer_model">{escape(left, quote=True)}</style>'
            f'<style class="footer">{spacer}{escape(hint, quote=True)}</style>'
        )

    def _footer_width(self) -> int:
        """Terminal width for footer edge-alignment, best effort.

        Prefers prompt_toolkit's own output size (what actually renders the
        line); falls back to the OS terminal size for detached/test hosts.
        """
        output = getattr(getattr(self.app, "app", None), "output", None)
        if output is not None:
            try:
                return output.get_size().columns
            except Exception:  # noqa: BLE001 - best-effort layout hint only
                pass
        return shutil.get_terminal_size((80, 24)).columns

    # ── Cached lookups ─────────────────────────────────────────────────────

    def has_agents_md(self) -> bool:
        """Whether any AGENTS.md/CLAUDE.md memory file applies here.

        Cached for a short window so the header can render every frame
        without stat-ing the filesystem each time (IMPROVEMENTS.md A3).
        """
        app = self.app
        now = time.monotonic()
        if (
            app._agents_md_cached is None
            or now - app._agents_md_checked_at > _AGENTS_MD_CACHE_SECONDS
        ):
            from ..agents_md import collect_agents_md_files

            app._agents_md_cached = bool(collect_agents_md_files())
            app._agents_md_checked_at = now
        return app._agents_md_cached

    def token_indicator(self) -> str:
        """Short token usage string like '12.4k/128k' for the header."""
        return format_token_indicator(
            self.app.session.context_tokens, self.app.session.context_window
        )
