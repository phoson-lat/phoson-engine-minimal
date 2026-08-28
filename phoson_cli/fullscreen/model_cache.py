"""Background-refreshed model id cache for inline ``/model`` autocomplete.

Fetching the model list (``list_models_for_providers``) is a live network
call — too slow to run synchronously while the user types. This cache
is refreshed in the background (on startup, and again after ``/model``
or ``/provider`` change the active provider/model) so the completer has
*something* to work with. Before the first refresh completes, typing
``/model `` just shows no suggestions yet — graceful degradation rather
than blocking input on a network round trip.

Since I-113 the cache spans **all configured providers** (concurrent
live fetch), so typing ``/model claude`` suggests Claude from Anthropic
even when OpenRouter is the active provider.
"""

import logging

from ..config import PhosonConfig, enabled_providers_from_config
from ..model_selector import list_models_for_providers

_LOGGER = logging.getLogger("phoson_cli.fullscreen.model_cache")


class ModelCache:
    """Holds the last known model ids across all configured providers."""

    def __init__(self) -> None:
        self.model_ids: list[str] = []
        #: id → provider (I-113), so the inline dropdown can show which
        #: provider each suggestion belongs to.
        self.model_providers: dict[str, str] = {}

    async def refresh(self, config: PhosonConfig) -> None:
        """Refetch model lists for every configured provider.

        Leaves the previous list untouched when every provider fails, so
        a transient outage never wipes working suggestions.
        """
        try:
            listings = await list_models_for_providers(
                config, enabled_providers_from_config(config)
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Model cache refresh failed", exc_info=True)
            return
        ids: list[str] = []
        providers: dict[str, str] = {}
        seen: set[str] = set()
        for listing in listings:
            for model in listing.options:
                if model.id and model.id not in seen:
                    seen.add(model.id)
                    ids.append(model.id)
                    providers[model.id] = model.provider
        if ids:
            self.model_ids = ids
            self.model_providers = providers


__all__ = ["ModelCache"]
