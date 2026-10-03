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

from prompt_toolkit.formatted_text import HTML

from phoson_llm.schemas import REASONING_EFFORTS

from ..formatting import format_token_indicator

_AGENTS_MD_CACHE_SECONDS = 5.0
_FOOTER_HINT_IDLE = "enter send  ·  ctrl+j newline  ·  / commands"
# While a turn runs the composer is still live: Enter queues the draft and
# run-safe commands (and Ctrl+P) remain available — see `turn_controller`.
_FOOTER_HINT_RUNNING = "esc cancel  ·  enter queue  ·  ctrl+p commands"
_FOOTER_HINT_PICKER = "enter  ·  esc"


def short_cwd(cwd: Path) -> str:
    """Compact display path for the fixed-width header."""
    parts = cwd.parts
    return str(Path(*parts[-2:])) if len(parts) > 2 else str(cwd)


class HeaderModel:
    """Owns the header/footer rendering and its per-frame caches.

    References the owning ``PhosonApp`` (``app``) for ``repl`` / ``sink`` and
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

        I-84: the HTML string is cached and only rebuilt when one of its
        inputs changes — repainting the chat for a spinner glyph must not
        re-stat the filesystem or reformat the header on every frame.
        """
        app = self.app
        repl = app.repl
        cost = repl.session_metrics.total_cost_usd
        cwd = short_cwd(Path.cwd())
        # Session identity (title + short id): the header is the single
        # location for session facts, so the auto/LLM title and the session
        # id live here. It is only shown once the session has actually begun
        # (first message / resume) — a fresh controller has an in-memory id
        # but no session yet, so nothing is displayed. The title is truncated
        # so a long name cannot push the model/token segments off a narrow
        # terminal.
        if repl._controller.session_started:
            short_id = (repl.tree.session_id or "")[:8] or "—"
            title = (repl.tree.title or "").strip()
            if len(title) > 40:
                title = title[:39] + "…"
            if title:
                session_html = (
                    '<style class="header_dim"> | </style>'
                    f'<style class="header">{escape(title, quote=True)}</style>'
                    '<style class="header_dim"> '
                    f"({escape(short_id, quote=True)})</style>"
                )
                session_part = f"{title} ({short_id})"
            else:
                session_html = (
                    '<style class="header_dim"> | </style>'
                    '<style class="header_dim">untitled '
                    f"({escape(short_id, quote=True)})</style>"
                )
                session_part = f"untitled ({short_id})"
        else:
            session_html = ""
            session_part = ""
        # T-2: cost only when > 0 — an idle/fresh session shows just the
        # token count, not a $0.0000 that reads as noise.
        token_cost = (
            f"{self.token_indicator()} tok · ${cost:.4f}"
            if cost > 0
            else f"{self.token_indicator()} tok"
        )

        attachments = len(repl.attachments)
        attach_part = f" · 📎{attachments}" if attachments else ""
        memory_part = " · 📄 agents.md" if self.has_agents_md() else ""
        # Active-monitors indicator (I-126): the plugin reports it via a
        # duck-typed hook; the header is the single place for session
        # facts, so it lives here (in-memory, safe on every paint).
        monitors = repl._controller.monitor_status()
        monitors_part = f" · {monitors}" if monitors else ""
        # Update-available hint (IMPROVEMENTS.md E5): a dim segment at the
        # very end of the header, shown as soon as the background PyPI
        # check lands and never blocking the paint. The shared REPL is
        # the single source of truth for the check result in both
        # front ends (the TUI starts it in ``run_async``).
        update_part = f" | {repl.update_hint}" if repl.update_hint else ""
        # Permission-mode chip (T-6): always visible; the accent word for
        # the *ask* state (confirmations are coming), dim for auto.
        perm_mode = self.permission_mode()
        mode_part = (
            ' <style class="header">ask</style>'
            if perm_mode == "ask"
            else ' <style class="header_dim">· auto</style>'
        )
        # Reasoning-effort chip (Ctrl+E): dim when off, accent with the
        # level when set. Read straight from the in-memory config (the
        # cycle mutates it before invalidating the cache below), so no
        # throttle like the permission policy file read is needed.
        effort = repl.config.reasoning_effort
        effort_part = (
            f' <style class="header">effort: {escape(effort, quote=True)}</style>'
            if effort in REASONING_EFFORTS
            else ' <style class="header_dim">· effort off</style>'
        )

        key = (
            cwd,
            session_part,
            token_cost,
            attach_part,
            memory_part,
            monitors_part,
            update_part,
            perm_mode,
            effort or "",  # None (off) and "" hash identically for cache-key purposes
        )
        if app._header_cache_key != key:
            app._header_cache_key = key
            extras = f"{attach_part}{memory_part}{monitors_part}"
            app._header_cache = HTML(
                '<style class="header_dim">* </style>'
                '<style class="header">phoson </style>'
                f"{session_html}"
                '<style class="header_dim"> | </style>'
                f'<style class="header_dim">{escape(cwd, quote=True)}</style>'
                '<style class="header_dim"> | </style>'
                f'<style class="header_dim">{escape(token_cost, quote=True)}</style>'
                f"{mode_part}"
                f"{effort_part}"
                f'<style class="header_dim">{escape(extras, quote=True)}</style>'
                f'<style class="header_dim">{escape(update_part, quote=True)}</style>'
            )
        return app._header_cache

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
        if app._active_float is not None:
            hint = _FOOTER_HINT_PICKER
        elif app._is_run_in_flight():
            hint = _FOOTER_HINT_RUNNING
        else:
            hint = _FOOTER_HINT_IDLE

        repl = app.repl
        left = f"{repl.current_model} ({repl.config.provider})"
        # Queued-message indicator: messages typed during a turn are sent in
        # order as each turn settles (`turn_controller.enqueue_turn`). Kept in
        # the footer now that the header has no transient state.
        queued = len(getattr(app, "_pending_turns", ()))
        if queued:
            left = f"{left} · {queued} queued"

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
            self.app.repl._context_tokens, self.app.repl._context_window
        )
