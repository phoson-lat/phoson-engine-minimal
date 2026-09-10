"""OmniRoute adapter.

OmniRoute is a local OpenAI-compatible AI gateway that aggregates many
upstream providers behind one endpoint (default ``http://localhost:20128/v1``).
See https://github.com/diegosouzapw/OmniRoute
"""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

OMNIROUTE_DEFAULT_BASE_URL = "http://localhost:20128/v1"
ENV_VAR = "OMNIROUTE_API_KEY"


class OmniRouteChat(OpenAICompatibleChat):
    """Adapter for the OmniRoute AI gateway (OpenAI-compatible endpoint).

    Args:
        api_key: Optional API key if OmniRoute is deployed with auth.
            Falls back to ``OMNIROUTE_API_KEY``.
        base_url: OmniRoute endpoint. Defaults to the local gateway.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or OMNIROUTE_DEFAULT_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="OmniRoute",
        )
