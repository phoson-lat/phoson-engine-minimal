import warnings
from decimal import Decimal, InvalidOperation

import httpx

from .config import PhosonConfig
from .models import ModelOption, load_models_file, apply_model_overrides


async def list_available_models(config: PhosonConfig) -> list[ModelOption]:
    """List models for the configured provider via a live query.

    Always fetches directly from the provider — no listing is cached to
    or read from ``~/.phoson/models.json``, so the picker never shows a
    stale list. User model overrides from that file (label, context
    window, custom entries) are still applied on top of whatever the
    provider returns, or of the single current-model fallback a lister
    returns when the request fails.
    """
    data = load_models_file()
    provider = config.provider.lower()
    options = await _fetch_provider_models(config)
    return _prioritize_current(
        apply_model_overrides(data, options, provider), config.model
    )


async def _fetch_provider_models(config: PhosonConfig) -> list[ModelOption]:
    """Live (network/local-API) model listing for the configured provider."""
    provider = config.provider.lower()
    if provider == "openrouter":
        return await _list_openrouter_models(config)
    if provider == "openai":
        return await _list_openai_models(config)
    if provider == "anthropic":
        return await _list_anthropic_models(config)
    if provider == "ollama":
        return await _list_ollama_models(config)
    if provider in ("gemini", "google"):
        return await _list_gemini_models(config)
    if provider == "groq":
        return await _list_groq_models(config)
    if provider == "deepseek":
        return await _list_deepseek_models(config)
    if provider == "together":
        return await _list_together_models(config)
    if provider == "mistral":
        return await _list_mistral_models(config)
    if provider == "perplexity":
        return await _list_perplexity_models(config)
    if provider == "fireworks":
        return await _list_fireworks_models(config)
    if provider == "cohere":
        return await _list_cohere_models(config)
    if provider in ("xai", "grok"):
        return await _list_xai_models(config)
    if provider == "nvidia":
        return await _list_nvidia_models(config)
    if provider == "github":
        return await _list_github_models(config)
    if provider == "azure":
        return await _list_azure_models(config)
    if provider in ("bedrock", "aws"):
        return await _list_bedrock_models(config)
    if provider == "vllm":
        return await _list_vllm_models(config)
    if provider == "lmstudio":
        return await _list_lmstudio_models(config)
    return [ModelOption(id=config.model, label=config.model, provider=provider)]


async def _list_openai_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "openai_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="openai")]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch OpenAI models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="openai")]

    options = [
        ModelOption(
            id=item.get("id", ""),
            label=item.get("id", ""),
            provider="openai",
            description=item.get("owned_by", ""),
        )
        for item in data.get("data", [])
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_anthropic_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "anthropic_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="anthropic")]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch Anthropic models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="anthropic")]

    options = [
        ModelOption(
            id=item.get("id", ""),
            label=item.get("display_name") or item.get("id", ""),
            provider="anthropic",
            description=item.get("type", ""),
        )
        for item in data.get("data", [])
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_openrouter_models(config: PhosonConfig) -> list[ModelOption]:
    headers: dict[str, str] = {}
    api_key = getattr(config, "openrouter_api_key", None)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers=headers or None,
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch OpenRouter models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="openrouter")]

    items = data.get("data") or data.get("models") or []
    options = []
    for item in items:
        model_id = item.get("id", "")
        if not model_id:
            continue
        context_length = item.get("context_length")
        pricing = _format_openrouter_pricing(item.get("pricing") or {})
        options.append(
            ModelOption(
                id=model_id,
                label=model_id,
                provider="openrouter",
                description=item.get("name") or item.get("description") or "",
                context_length=context_length
                if isinstance(context_length, int)
                else None,
                pricing=pricing,
            )
        )
    return _prioritize_current(options, config.model)


async def _list_ollama_models(config: PhosonConfig) -> list[ModelOption]:
    base_url = (
        getattr(config, "ollama_base_url", None) or "http://localhost:11434"
    ).rstrip("/")
    url = f"{base_url}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch Ollama models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="ollama")]

    options = []
    for item in data.get("models", []):
        name = item.get("name", "")
        if not name:
            continue
        size = item.get("details", {}).get("parameter_size") or ""
        family = item.get("details", {}).get("family") or ""
        description = " · ".join(part for part in [family, size] if part)
        options.append(
            ModelOption(
                id=name,
                label=name,
                provider="ollama",
                description=description,
            )
        )
    if not options:
        options = [ModelOption(id=config.model, label=config.model, provider="ollama")]
    return _prioritize_current(options, config.model)


async def _list_groq_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "groq_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="groq")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(f"Failed to fetch Groq models: {exc}", UserWarning, stacklevel=2)
        return [ModelOption(id=config.model, label=config.model, provider="groq")]
    options = [
        ModelOption(id=item.get("id", ""), label=item.get("id", ""), provider="groq")
        for item in data.get("data", [])
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_deepseek_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "deepseek_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="deepseek")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.deepseek.com/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch DeepSeek models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="deepseek")]
    options = [
        ModelOption(
            id=item.get("id", ""), label=item.get("id", ""), provider="deepseek"
        )
        for item in data.get("data", [])
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_together_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "together_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="together")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.together.xyz/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch Together models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="together")]
    items = data if isinstance(data, list) else data.get("data", [])
    options = [
        ModelOption(
            id=item.get("id", ""),
            label=item.get("display_name") or item.get("id", ""),
            provider="together",
            description=item.get("description", ""),
        )
        for item in items
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_mistral_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "mistral_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="mistral")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.mistral.ai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch Mistral models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="mistral")]
    options = [
        ModelOption(id=item.get("id", ""), label=item.get("id", ""), provider="mistral")
        for item in data.get("data", [])
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_perplexity_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "perplexity_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="perplexity")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.perplexity.ai/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch Perplexity models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="perplexity")]
    items = data.get("data", []) if isinstance(data, dict) else data
    options = [
        ModelOption(
            id=item.get("id", ""),
            label=item.get("name") or item.get("id", ""),
            provider="perplexity",
        )
        for item in items
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_fireworks_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "fireworks_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="fireworks")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.fireworks.ai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch Fireworks models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="fireworks")]
    options = [
        ModelOption(
            id=item.get("name", item.get("id", "")),
            label=item.get("name", item.get("id", "")),
            provider="fireworks",
            description=item.get("displayName", ""),
        )
        for item in data.get("models", data.get("data", []))
        if item.get("name") or item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_cohere_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "cohere_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="cohere")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.cohere.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch Cohere models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="cohere")]
    options = [
        ModelOption(
            id=item.get("name", item.get("id", "")),
            label=item.get("name", item.get("id", "")),
            provider="cohere",
        )
        for item in data.get("models", data.get("data", []))
        if item.get("name") or item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_xai_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "xai_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="xai")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.x.ai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(f"Failed to fetch xAI models: {exc}", UserWarning, stacklevel=2)
        return [ModelOption(id=config.model, label=config.model, provider="xai")]
    options = [
        ModelOption(id=item.get("id", ""), label=item.get("id", ""), provider="xai")
        for item in data.get("data", [])
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_nvidia_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "nvidia_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="nvidia")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://integrate.api.nvidia.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch NVIDIA models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="nvidia")]
    options = [
        ModelOption(id=item.get("id", ""), label=item.get("id", ""), provider="nvidia")
        for item in data.get("data", [])
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_github_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "github_token", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="github")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://models.inference.ai.azure.com/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch GitHub Models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="github")]
    items = data if isinstance(data, list) else data.get("data", [])
    options = [
        ModelOption(
            id=item.get("id", item.get("name", "")),
            label=item.get("name") or item.get("id", ""),
            provider="github",
        )
        for item in items
        if item.get("id") or item.get("name")
    ]
    return _prioritize_current(options, config.model)


async def _list_gemini_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "gemini_api_key", None)
    if not api_key:
        return [ModelOption(id=config.model, label=config.model, provider="gemini")]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": api_key},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch Gemini models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="gemini")]
    options = [
        ModelOption(
            id=item.get("name", "").replace("models/", ""),
            label=item.get("displayName")
            or item.get("name", "").replace("models/", ""),
            provider="gemini",
            description=item.get("description", ""),
        )
        for item in data.get("models", [])
        if item.get("name")
    ]
    return _prioritize_current(options, config.model)


async def _list_azure_models(config: PhosonConfig) -> list[ModelOption]:
    api_key = getattr(config, "azure_openai_api_key", None)
    endpoint = getattr(config, "azure_openai_endpoint", None)
    if not api_key or not endpoint:
        return [ModelOption(id=config.model, label=config.model, provider="azure")]
    try:
        url = f"{endpoint.rstrip('/')}/openai/deployments?api-version=2024-06-01"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                headers={"api-key": api_key},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch Azure deployments: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="azure")]
    options = [
        ModelOption(
            id=item.get("id", item.get("name", "")),
            label=item.get("model", item.get("name", "")),
            provider="azure",
        )
        for item in data.get("data", [])
        if item.get("id") or item.get("name")
    ]
    return _prioritize_current(options, config.model)


async def _list_bedrock_models(config: PhosonConfig) -> list[ModelOption]:
    return [ModelOption(id=config.model, label=config.model, provider="bedrock")]


async def _list_vllm_models(config: PhosonConfig) -> list[ModelOption]:
    base_url = (
        getattr(config, "vllm_base_url", None) or "http://localhost:8000/v1"
    ).rstrip("/")
    url = f"{base_url}/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(f"Failed to fetch vLLM models: {exc}", UserWarning, stacklevel=2)
        return [ModelOption(id=config.model, label=config.model, provider="vllm")]
    options = [
        ModelOption(id=item.get("id", ""), label=item.get("id", ""), provider="vllm")
        for item in data.get("data", [])
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


async def _list_lmstudio_models(config: PhosonConfig) -> list[ModelOption]:
    base_url = (
        getattr(config, "lmstudio_base_url", None) or "http://localhost:1234/v1"
    ).rstrip("/")
    url = f"{base_url}/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.warn(
            f"Failed to fetch LM Studio models: {exc}", UserWarning, stacklevel=2
        )
        return [ModelOption(id=config.model, label=config.model, provider="lmstudio")]
    options = [
        ModelOption(
            id=item.get("id", ""), label=item.get("id", ""), provider="lmstudio"
        )
        for item in data.get("data", [])
        if item.get("id")
    ]
    return _prioritize_current(options, config.model)


def _format_openrouter_pricing(pricing: dict) -> str:
    prompt = _format_price_per_million(pricing.get("prompt"))
    completion = _format_price_per_million(pricing.get("completion"))
    if not prompt and not completion:
        return ""
    parts: list[str] = []
    if prompt:
        parts.append(f"in {prompt}")
    if completion:
        parts.append(f"out {completion}")
    return " · ".join(parts)


def _format_price_per_million(raw: object) -> str:
    if raw in {None, ""}:
        return ""
    try:
        value = Decimal(str(raw)) * Decimal(1_000_000)
    except (InvalidOperation, ValueError):
        return str(raw)

    if value == 0:
        return "$0/M"
    if value >= 100:
        formatted = f"${value.quantize(Decimal('1')):,}/M"
        return formatted.replace(".0", "")
    if value >= 1:
        formatted = f"${value.quantize(Decimal('0.01')):,}/M"
        return formatted.replace(".00", "")
    formatted = f"${value.quantize(Decimal('0.001')):,}/M"
    return formatted.replace(".000", "")


def _prioritize_current(
    options: list[ModelOption], current_model: str
) -> list[ModelOption]:
    seen: set[str] = set()
    deduped: list[ModelOption] = []
    for option in options:
        if not option.id or option.id in seen:
            continue
        seen.add(option.id)
        deduped.append(option)

    if current_model and current_model not in seen:
        deduped.append(
            ModelOption(id=current_model, label=current_model, provider="custom")
        )

    deduped.sort(key=lambda option: (option.id != current_model, option.id.lower()))
    return deduped
