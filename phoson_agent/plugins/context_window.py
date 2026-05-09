"""Context window resolution for all supported providers.

Provides a single entry point to look up the context_window (in tokens)
for any model, using a mix of static registry and dynamic API queries.
"""

import warnings

import httpx

# ─────────────────────────────────────────────────────────────────────
# Static registry — verified values (tokens)
# ─────────────────────────────────────────────────────────────────────

# Key format: "provider/model_name"  (model_name as it appears in config)
# Anthropic: all Claude models share 200k
# OpenAI:    128k for all listed models
CONTEXT_WINDOW_REGISTRY: dict[str, int] = {
    # Anthropic
    "anthropic/claude-opus-4-7": 200_000,
    "anthropic/claude-opus-4-6": 200_000,
    "anthropic/claude-sonnet-4-6": 200_000,
    "anthropic/claude-haiku-4-5": 200_000,
    # OpenAI
    "openai/gpt-4o": 128_000,
    "openai/gpt-4o-mini": 128_000,
    "openai/o3": 128_000,
    "openai/o4-mini": 128_000,
}

# Anthropic prefix — any model starting with these gets 200k
ANTHROPIC_PREFIXES = ("claude-",)

# OpenAI prefix — any model starting with these gets 128k
OPENAI_PREFIXES = ("gpt-4", "o1", "o3", "o4")

# Default fallback when nothing matches
DEFAULT_CONTEXT_WINDOW = 128_000


# ─────────────────────────────────────────────────────────────────────
# Resolver
# ─────────────────────────────────────────────────────────────────────


class ContextWindowResolver:
    """Resolves the context_window for a given provider + model name.

    Strategy:
    1. Static registry lookup
    2. Prefix matching for Anthropic/OpenAI
    3. For Ollama: query /api/show (with cache)
    4. For OpenRouter: query /api/v1/models (with cache)
    5. Fallback: DEFAULT_CONTEXT_WINDOW

    Args:
        ollama_base_url: Base URL for Ollama API (default: http://localhost:11434).
        openrouter_api_key: Optional OpenRouter API key for model queries.
    """

    def __init__(
        self,
        ollama_base_url: str = "http://localhost:11434",
        openrouter_api_key: str | None = None,
    ) -> None:
        """Initialize the resolver with optional API endpoints."""
        self._ollama_base_url = ollama_base_url.rstrip("/")
        self._openrouter_api_key = openrouter_api_key
        self._ollama_cache: dict[str, int] = {}
        self._openrouter_cache: dict[str, int] = {}

    # ── Public ────────────────────────────────────────────────────────

    async def resolve(
        self,
        provider: str,
        model: str,
    ) -> int:
        """Returns the context_window in tokens for the given model."""
        key = f"{provider}/{model}"

        # 1. Static registry
        if key in CONTEXT_WINDOW_REGISTRY:
            return CONTEXT_WINDOW_REGISTRY[key]

        # 2. Prefix matching
        if provider == "anthropic":
            if any(model.startswith(p) for p in ANTHROPIC_PREFIXES):
                return 200_000

        if provider == "openai":
            if any(model.startswith(p) for p in OPENAI_PREFIXES):
                return 128_000

        # 3. Ollama — dynamic query
        if provider == "ollama":
            return await self._resolve_ollama(model)

        # 4. OpenRouter — dynamic query
        if provider == "openrouter":
            return await self._resolve_openrouter(model)

        # 5. Fallback
        return DEFAULT_CONTEXT_WINDOW

    # ── Ollama ────────────────────────────────────────────────────────

    async def _resolve_ollama(self, model: str) -> int:
        if model in self._ollama_cache:
            return self._ollama_cache[model]

        # Fallback to default if Ollama is unreachable or returns an unexpected
        # payload. We treat this as a soft-fail because the caller already
        # accepts that resolution may be best-effort.
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self._ollama_base_url}/api/show",
                    json={"name": model},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # num_ctx may be in parameters or in model_info
                    num_ctx = self._extract_ollama_num_ctx(data)
                    if num_ctx:
                        self._ollama_cache[model] = num_ctx
                        return num_ctx
                    warnings.warn(
                        f"Ollama /api/show response for {model!r} contained no"
                        f" num_ctx; using default ({DEFAULT_CONTEXT_WINDOW} tokens)",
                        UserWarning,
                        stacklevel=2,
                    )
        except (httpx.HTTPError, ValueError) as exc:
            warnings.warn(
                f"Failed to fetch Ollama context window for {model!r}: {exc}",
                UserWarning,
                stacklevel=2,
            )

        self._ollama_cache[model] = DEFAULT_CONTEXT_WINDOW
        return DEFAULT_CONTEXT_WINDOW

    @staticmethod
    def _extract_ollama_num_ctx(data: dict) -> int | None:
        """Extracts num_ctx from /api/show response."""
        # Option 1: parameters -> num_ctx
        params = data.get("parameters", {})
        if isinstance(params, str):
            # Sometimes comes as multiline string
            for line in params.splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[0].lower() == "num_ctx":
                    try:
                        return int(parts[1])
                    except ValueError:
                        warnings.warn(
                            f"Could not parse Ollama num_ctx value {parts[1]!r};"
                            " falling back to default context window",
                            UserWarning,
                            stacklevel=3,
                        )
        elif isinstance(params, dict):
            val = params.get("num_ctx")
            if val is not None:
                return int(val)

        # Option 2: model_info -> context_length
        model_info = data.get("model_info", {})
        for k, v in model_info.items():
            if "context_length" in k and isinstance(v, int):
                return v

        return None

    # ── OpenRouter ────────────────────────────────────────────────────

    async def _resolve_openrouter(self, model: str) -> int:
        if model in self._openrouter_cache:
            return self._openrouter_cache[model]

        # Same soft-fail policy as Ollama: best-effort lookup with default
        # fallback. See _resolve_ollama for rationale.
        try:
            headers: dict[str, str] = {}
            if self._openrouter_api_key:
                headers["Authorization"] = f"Bearer {self._openrouter_api_key}"

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    models_list = data.get("data", [])
                    for m in models_list:
                        if m.get("id") == model:
                            ctx = m.get("context_length")
                            if ctx is not None:
                                val = int(ctx)
                                self._openrouter_cache[model] = val
                                return val
                            # Fallback: top_provider.context_length
                            top = m.get("top_provider", {})
                            ctx = top.get("context_length")
                            if ctx is not None:
                                val = int(ctx)
                                self._openrouter_cache[model] = val
                                return val
        except (httpx.HTTPError, ValueError) as exc:
            warnings.warn(
                f"Failed to fetch OpenRouter context window for {model!r}: {exc}",
                UserWarning,
                stacklevel=2,
            )

        self._openrouter_cache[model] = DEFAULT_CONTEXT_WINDOW
        return DEFAULT_CONTEXT_WINDOW

    # ── Cache management ──────────────────────────────────────────────

    def clear_cache(self) -> None:
        """Clears the internal caches."""
        self._ollama_cache.clear()
        self._openrouter_cache.clear()
