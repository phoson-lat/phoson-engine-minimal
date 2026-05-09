import warnings
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass

import httpx

from .config import PhosonConfig


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str
    provider: str
    description: str = ""
    context_length: int | None = None
    pricing: str = ""


async def list_available_models(config: PhosonConfig) -> list[ModelOption]:
    provider = config.provider.lower()
    if provider == "openrouter":
        return await _list_openrouter_models(config)
    if provider == "openai":
        return await _list_openai_models(config)
    if provider == "anthropic":
        return await _list_anthropic_models(config)
    if provider == "ollama":
        return await _list_ollama_models(config)
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
    except Exception as exc:
        warnings.warn(f"Failed to fetch OpenAI models: {exc}", UserWarning, stacklevel=2)
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
    except Exception as exc:
        warnings.warn(f"Failed to fetch Anthropic models: {exc}", UserWarning, stacklevel=2)
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
    except Exception as exc:
        warnings.warn(f"Failed to fetch OpenRouter models: {exc}", UserWarning, stacklevel=2)
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
    except Exception as exc:
        warnings.warn(f"Failed to fetch Ollama models: {exc}", UserWarning, stacklevel=2)
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
