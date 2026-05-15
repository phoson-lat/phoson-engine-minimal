"""GitHub Models adapter. Uses the ``GITHUB_TOKEN`` env var."""

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

GITHUB_MODELS_BASE_URL = "https://models.inference.ai.azure.com"
ENV_VAR = "GITHUB_TOKEN"


class GitHubModelsChat(OpenAICompatibleChat):
    """Adapter for GitHub Models (OpenAI-compatible endpoint)."""

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(
            base_url=GITHUB_MODELS_BASE_URL,
            api_key=api_key,
            api_key_env=ENV_VAR,
            provider_name="GitHubModels",
        )
