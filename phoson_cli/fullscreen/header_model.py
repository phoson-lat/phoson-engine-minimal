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
from pathlib import Path

from prompt_toolkit.formatted_text import HTML

from phoson_llm.schemas import REASONING_EFFORTS

from ..formatting import format_token_indicator

_AGENTS_MD_CACHE_SECONDS = 5.0
_FOOTER_HINT_IDLE = "enter send  ·  ctrl+j newline  ·  / commands"
_FOOTER_HINT_RUNNING = "esc cancel"
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
        """Compact runtime header: brand · model (provider) · cwd · usage · status.

        The header is the single location for session facts in the
        full-screen UI. The lower line deliberately contains only keyboard
        hints, so no model/provider/cost/token/cwd value is repeated.

        I-84: the HTML string is cached and only rebuilt when one of its
        inputs changes — repainting the chat for a spinner glyph must not
        re-stat the filesystem or reformat the header on every frame.
        """
        app = self.app
        repl = app.repl
        cost = repl.session_metrics.total_cost_usd
        model_provider = f"{repl.current_model} ({repl.config.provider})"
        cwd = short_cwd(Path.cwd())
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
        status = app.sink.status_text()
        # T-2: the idle status is empty (no "Online"); only show the
        # separator when there is actually a live status to display.
        status_part = (
            f'<style class="header_dim"> | </style>'
            f'<style class="header_dim">{status}</style>'
            if status
            else ""
        )
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
            f' <style class="header">effort: {effort}</style>'
            if effort in REASONING_EFFORTS
            else ' <style class="header_dim">· effort off</style>'
        )

        key = (
            model_provider,
            cwd,
            token_cost,
            attach_part,
            memory_part,
            monitors_part,
            update_part,
            status,
            perm_mode,
            effort or "",  # None (off) and "" hash identically for cache-key purposes
        )
        if app._header_cache_key != key:
            app._header_cache_key = key
            extras = f"{attach_part}{memory_part}{monitors_part}"
            app._header_cache = HTML(
                '<style class="header"> phoson </style>'
                '<style class="header_dim"> | </style>'
                f'<style class="header_dim">{model_provider}</style>'
                '<style class="header_dim"> | </style>'
                f'<style class="header_dim">{cwd}</style>'
                '<style class="header_dim"> | </style>'
                f'<style class="header_dim">{token_cost}</style>'
                f"{mode_part}"
                f"{effort_part}"
                f'<style class="header_dim">{extras}</style>'
                f"{status_part}"
                f'<style class="header_dim">{update_part}</style>'
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
        """Contextual footer: at most three hints for the current state.

        Replaces the fixed 8-shortcut cheatsheet (T-9), which truncated at
        80 columns. The hints are deliberately short so the line survives
        narrow terminals; the full key map is ``/keys``, and the
        Shift+Drag text-selection note lives in
        ``docs/cli/mouse-and-links.md`` (and /keys).
        """
        app = self.app
        if app._active_float is not None:
            hint = _FOOTER_HINT_PICKER
        elif app._is_run_in_flight():
            hint = _FOOTER_HINT_RUNNING
        else:
            hint = _FOOTER_HINT_IDLE
        return HTML(f'<style class="footer">{hint}</style>')

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
