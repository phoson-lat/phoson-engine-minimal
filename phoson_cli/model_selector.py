import asyncio
import warnings
from types import SimpleNamespace
from decimal import Decimal, InvalidOperation
from dataclasses import field, replace, dataclass, is_dataclass
from collections.abc import Callable

import httpx

from .config import PhosonConfig
from .models import (
    KNOWN_PROVIDERS,
    ModelOption,
    load_models_file,
    provider_settings,
    normalize_provider,
    apply_model_overrides,
)


class ModelListingError(Exception):
    """A provider's live model listing failed (network/API error).

    Raised by the per-provider listers; the single-provider fast path
    (``list_available_models``) catches it and degrades to the
    current-model fallback + ``UserWarning`` exactly as before, while the
    multi-provider view (``list_models_for_providers``) turns it into a
    ``ProviderListing(available=False)`` instead of a silent fallback.
    """


@dataclass
class ProviderListing:
    """One provider's live model listing (I-113).

    ``available=False`` means the live fetch failed (or there is no
    lister for the provider) — callers must show an ``unavailable``
    marker instead of treating ``options`` (empty) as the real catalog.
    """

    provider: str
    options: list[ModelOption] = field(default_factory=list)
    available: bool = True
    error: str = ""


async def list_available_models(config: PhosonConfig) -> list[ModelOption]:
    """List models for the configured provider via a live query.

    Always fetches directly from the provider — no listing is cached to
    or read from ``~/.phoson/models.json``, so the picker never shows a
    stale list. User model overrides from that file (label, context
    window, custom entries) are still applied on top of whatever the
    provider returns, or of the single current-model fallback used when
    the live request fails (with a ``UserWarning``).
    """
    data = load_models_file()
    provider = config.provider.lower()
    try:
        options = await _fetch_provider_models(config)
    except ModelListingError as exc:
        warnings.warn(str(exc), UserWarning, stacklevel=2)
        options = [ModelOption(id=config.model, label=config.model, provider=provider)]
    return _prioritize_current(
        apply_model_overrides(data, options, provider),
        config.model,
        order_key=_provider_order_key(provider),
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


def _current_model_for(config: PhosonConfig, provider: str) -> str:
    """Model id used as the *current* marker for *provider* (I-113).

    The active provider keeps ``config.model``; other providers fall back
    to their ``default_model`` from ``models.json`` (when configured),
    then to ``config.model``.
    """
    if normalize_provider(provider) == normalize_provider(config.provider):
        return config.model
    return provider_settings(load_models_file(), provider).get(
        "default_model", config.model
    )


def _variant_config(config: PhosonConfig, provider: str, model: str) -> PhosonConfig:
    """Clone *config* with ``provider``/``model`` overridden.

    ``dataclasses.replace`` for real :class:`PhosonConfig` instances; a
    shallow attribute copy for stand-ins (tests use ``SimpleNamespace``).
    """
    if is_dataclass(config) and not isinstance(config, type):
        return replace(config, provider=provider, model=model)
    ns = SimpleNamespace(**vars(config))
    ns.provider = provider
    ns.model = model
    return ns  # type: ignore[return-value]


async def _fetch_listing(config: PhosonConfig, provider: str) -> list[ModelOption]:
    """Live listing for *provider*, with overrides and ordering applied.

    The single-provider ``config`` is cloned (see :func:`_variant_config`)
    so the existing per-provider listers work unmodified. Raises
    :class:`ModelListingError` when the live request fails.
    """
    current = _current_model_for(config, provider)
    variant = _variant_config(config, provider, current)
    options = await _fetch_provider_models(variant)
    return _prioritize_current(
        apply_model_overrides(load_models_file(), options, provider),
        current,
        order_key=_provider_order_key(provider),
    )


async def list_models_for_providers(
    config: PhosonConfig, providers: list[str]
) -> list[ProviderListing]:
    """Live model listings for several providers **concurrently** (I-113).

    The active provider comes first, then the rest in the given order.
    Each provider that fails its live fetch (or has no known lister)
    yields a ``ProviderListing(available=False, error=...)`` — never a
    silent single-model fallback, so callers can mark it ``unavailable``.
    """
    active = normalize_provider(config.provider)
    ordered = [p for p in providers if normalize_provider(p) == active]
    ordered += [p for p in providers if normalize_provider(p) != active]

    async def _one(provider: str) -> ProviderListing:
        canonical = normalize_provider(provider)
        if canonical not in KNOWN_PROVIDERS:
            return ProviderListing(
                provider=canonical, available=False, error="unknown provider"
            )
        try:
            options = await _fetch_listing(config, canonical)
        except ModelListingError as exc:
            return ProviderListing(provider=canonical, available=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 — one provider must not sink the view
            return ProviderListing(
                provider=canonical,
                available=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        return ProviderListing(provider=canonical, options=options)

    results = await asyncio.gather(*(_one(p) for p in ordered))
    return list(results)


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
        raise ModelListingError(f"Failed to fetch OpenAI models: {exc}") from exc

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
        raise ModelListingError(f"Failed to fetch Anthropic models: {exc}") from exc

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
        raise ModelListingError(f"Failed to fetch OpenRouter models: {exc}") from exc

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
                agentic_index=_openrouter_agentic_index(item),
            )
        )
    return _prioritize_current(
        options,
        config.model,
        order_key=_provider_order_key("openrouter"),
    )


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
        raise ModelListingError(f"Failed to fetch Ollama models: {exc}") from exc

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
        raise ModelListingError(f"Failed to fetch Groq models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch DeepSeek models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch Together models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch Mistral models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch Perplexity models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch Fireworks models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch Cohere models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch xAI models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch NVIDIA models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch GitHub Models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch Gemini models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch Azure deployments: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch vLLM models: {exc}") from exc
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
        raise ModelListingError(f"Failed to fetch LM Studio models: {exc}") from exc
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
    options: list[ModelOption],
    current_model: str,
    *,
    order_key: Callable[[ModelOption], tuple] | None = None,
) -> list[ModelOption]:
    """Dedup and order ``options``: current model first, then *order_key*.

    ``order_key`` is the provider-specific secondary comparator (I-113:
    OpenRouter sorts by ``agentic_index``). Defaults to alphabetical by id.
    """
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

    sort_key = order_key or (lambda option: (option.id.lower(),))
    deduped.sort(key=lambda option: (option.id != current_model, sort_key(option)))
    return deduped


def _openrouter_order_key(option: ModelOption) -> tuple:
    """I-113 secondary key: highest ``agentic_index`` first, models without
    the field last (alphabetical among themselves)."""
    if option.agentic_index is not None:
        return (0.0, -option.agentic_index, option.id.lower())
    return (1.0, 0.0, option.id.lower())


def _provider_order_key(provider: str) -> Callable[[ModelOption], tuple] | None:
    """Provider-specific ``_prioritize_current`` key, or the alphabetical
    default (``None``)."""
    if provider == "openrouter":
        return _openrouter_order_key
    return None


def _openrouter_agentic_index(item: dict) -> float | None:
    """``benchmarks.artificial_analysis.agentic_index`` from an OpenRouter model."""
    benchmarks = item.get("benchmarks")
    if not isinstance(benchmarks, dict):
        return None
    aa = benchmarks.get("artificial_analysis")
    if not isinstance(aa, dict):
        return None
    index = aa.get("agentic_index")
    if isinstance(index, bool):
        return None
    if isinstance(index, (int, float)):
        return float(index)
    return None
