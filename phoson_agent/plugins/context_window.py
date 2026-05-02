"""Context window resolution for all supported providers.

Provides a single entry point to look up the context_window (in tokens)
for any model, using a mix of static registry and dynamic API queries.
"""

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
    """Resuelve el context_window de un modelo dado provider + model name.

    Estrategia:
    1. Lookup en registry estático
    2. Prefix matching para Anthropic/OpenAI
    3. Para Ollama: query a /api/show (con caché)
    4. Para OpenRouter: query a /api/v1/models (con caché)
    5. Fallback: DEFAULT_CONTEXT_WINDOW
    """

    def __init__(
        self,
        ollama_base_url: str = "http://localhost:11434",
        openrouter_api_key: str | None = None,
    ) -> None:
        self._ollama_base_url = ollama_base_url.rstrip("/")
        self._openrouter_api_key = openrouter_api_key
        # Caché: model_name → context_window
        self._ollama_cache: dict[str, int] = {}
        self._openrouter_cache: dict[str, int] = {}

    # ── Public ────────────────────────────────────────────────────────

    async def resolve(
        self,
        provider: str,
        model: str,
    ) -> int:
        """Devuelve el context_window en tokens para el modelo dado."""
        key = f"{provider}/{model}"

        # 1. Registry estático
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

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self._ollama_base_url}/api/show",
                    json={"name": model},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # num_ctx puede estar en parameters o en model_info
                    num_ctx = self._extract_ollama_num_ctx(data)
                    if num_ctx:
                        self._ollama_cache[model] = num_ctx
                        return num_ctx
        except Exception:
            pass  # Fall through to default

        self._ollama_cache[model] = DEFAULT_CONTEXT_WINDOW
        return DEFAULT_CONTEXT_WINDOW

    @staticmethod
    def _extract_ollama_num_ctx(data: dict) -> int | None:
        """Extrae num_ctx del response de /api/show."""
        # Opción 1: parameters → num_ctx
        params = data.get("parameters", {})
        if isinstance(params, str):
            # A veces viene como string multilinea
            for line in params.splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[0].lower() == "num_ctx":
                    try:
                        return int(parts[1])
                    except ValueError:
                        pass
        elif isinstance(params, dict):
            val = params.get("num_ctx")
            if val is not None:
                return int(val)

        # Opción 2: model_info → context_length
        model_info = data.get("model_info", {})
        for k, v in model_info.items():
            if "context_length" in k and isinstance(v, int):
                return v

        return None

    # ── OpenRouter ────────────────────────────────────────────────────

    async def _resolve_openrouter(self, model: str) -> int:
        if model in self._openrouter_cache:
            return self._openrouter_cache[model]

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
        except Exception:
            pass  # Fall through to default

        self._openrouter_cache[model] = DEFAULT_CONTEXT_WINDOW
        return DEFAULT_CONTEXT_WINDOW

    # ── Cache management ──────────────────────────────────────────────

    def clear_cache(self) -> None:
        self._ollama_cache.clear()
        self._openrouter_cache.clear()
