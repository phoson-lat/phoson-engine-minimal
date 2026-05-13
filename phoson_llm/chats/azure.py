"""Azure OpenAI adapter.

Constructs the endpoint URL from resource, deployment, and api-version.
"""

from __future__ import annotations

import os

from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

AZURE_API_VERSION = "2024-08-01-preview"
AZURE_ENDPOINT_ENV = "AZURE_OPENAI_ENDPOINT"
AZURE_API_KEY_ENV = "AZURE_OPENAI_API_KEY"
AZURE_DEPLOYMENT_ENV = "AZURE_OPENAI_DEPLOYMENT"


class AzureChat(OpenAICompatibleChat):
    """Adapter for Azure OpenAI Service.

    Args:
        azure_endpoint: Resource URL (e.g. ``https://my-resource.openai.azure.com``).
            Defaults to ``AZURE_OPENAI_ENDPOINT`` env var.
        api_key: API key. Defaults to ``AZURE_OPENAI_API_KEY`` env var.
        deployment: Deployment name. Defaults to ``AZURE_OPENAI_DEPLOYMENT`` env var.
        api_version: API version (default ``2024-08-01-preview``).
    """

    def __init__(
        self,
        azure_endpoint: str | None = None,
        api_key: str | None = None,
        deployment: str | None = None,
        api_version: str = AZURE_API_VERSION,
    ) -> None:
        endpoint = (
            azure_endpoint or os.environ.get(AZURE_ENDPOINT_ENV) or ""
        ).rstrip("/")
        dep = deployment or os.environ.get(AZURE_DEPLOYMENT_ENV) or ""
        key = api_key or os.environ.get(AZURE_API_KEY_ENV) or ""

        base_url = (
            f"{endpoint}/openai/deployments/{dep}"
            f"/chat/completions?api-version={api_version}"
        )

        super().__init__(
            base_url=base_url,
            api_key=key,
            max_tokens_key="max_tokens",
            default_headers={"api-key": key},
            provider_name="Azure",
        )
