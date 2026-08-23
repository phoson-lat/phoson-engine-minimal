"""Background-refreshed model id cache for inline ``/model`` autocomplete.

Fetching the model list (``list_available_models``) is a live network
call — too slow to run synchronously while the user types. This cache
is refreshed in the background (on startup, and again after ``/model``
or ``/provider`` change the active provider/model) so the completer has
*something* to work with. Before the first refresh completes, typing
``/model `` just shows no suggestions yet — graceful degradation rather
than blocking input on a network round trip.
"""

import logging

from ..config import PhosonConfig
from ..model_selector import list_available_models

_LOGGER = logging.getLogger("phoson_cli.fullscreen.model_cache")


class ModelCache:
    """Holds the last known list of model ids for the active provider."""

    def __init__(self) -> None:
        self.model_ids: list[str] = []

    async def refresh(self, config: PhosonConfig) -> None:
        """Refetch the model list; leaves the previous list on failure."""
        try:
            models = await list_available_models(config)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Model cache refresh failed", exc_info=True)
            return
        self.model_ids = [m.id for m in models]


__all__ = ["ModelCache"]
