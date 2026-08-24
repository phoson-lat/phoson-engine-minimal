"""Holds the last known session list for the /sessions autocomplete."""

import logging

from phoson_agent.sessions.models import SessionMeta

_LOGGER = logging.getLogger(__name__)


class SessionListCache:
    """Holds the most recent sessions for inline autocomplete."""

    def __init__(self, max_sessions: int = 20) -> None:
        self.sessions: list[SessionMeta] = []
        self._max = max_sessions

    async def refresh(self, storage) -> None:
        """Refetch session metadata (most recent first)."""
        try:
            metas = await storage.list_meta()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Session cache refresh failed", exc_info=True)
            return
        self.sessions = list(metas)[: self._max]


__all__ = ["SessionListCache"]
