"""Holds the last known session list for the /sessions autocomplete."""

import logging

from phoson_agent.sessions.models import SessionMeta

_LOGGER = logging.getLogger(__name__)


class SessionListCache:
    """Holds the most recent sessions for inline autocomplete."""

    def __init__(self, max_sessions: int = 20) -> None:
        self.sessions: list[SessionMeta] = []
        self._max = max_sessions

    async def refresh(self, storage, cwd: str | None = None) -> None:
        """Refetch session metadata (most recent first).

        When *cwd* is given the list is scoped to sessions started in that
        working directory (plus legacy/global ones) — #212.
        """
        try:
            metas = await storage.list_meta(cwd=cwd)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Session cache refresh failed", exc_info=True)
            return
        self.sessions = list(metas)[: self._max]


__all__ = ["SessionListCache"]
