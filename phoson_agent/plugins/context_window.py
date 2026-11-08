"""Context window resolution for all supported providers.

Provides a single entry point to look up the context_window (in tokens)
for any model, using a mix of static registry and dynamic API queries.
"""

import re
import time
import logging
import warnings
from collections.abc import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Static registry — verified values (tokens)
# ─────────────────────────────────────────────────────────────────────

# Values from the providers' official model pages, verified Nov 2026. Keys are
# ``provider/<normalized id>`` (see :func:`normalize_model_id`). A provider's
# live listing (the resolver's ``catalog`` hook) takes precedence when it
# reports a window; this table covers providers whose listing does not.
CONTEXT_WINDOW_REGISTRY: dict[str, int] = {
    # Anthropic — 1M by default for the 5.x generation
    "anthropic/claude-fable-5-1": 1_000_000,
    "anthropic/claude-opus-5-5": 1_000_000,
    "anthropic/claude-sonnet-5-5": 1_000_000,
    "anthropic/claude-haiku-5-5": 1_000_000,
    "anthropic/claude-opus-4-7": 200_000,
    "anthropic/claude-opus-4-6": 200_000,
    "anthropic/claude-sonnet-4-6": 200_000,
    "anthropic/claude-haiku-4-5": 200_000,
    # OpenAI
    "openai/gpt-5.6-sol": 1_050_000,
    "openai/gpt-5.5": 1_050_000,
    "openai/gpt-4.1": 1_047_576,
    "openai/gpt-4o": 128_000,
    "openai/gpt-4o-mini": 128_000,
    "openai/o3": 200_000,
    "openai/o4-mini": 128_000,
    # Google Gemini
    "gemini/gemini-4-argon": 1_000_000,
    "gemini/gemini-3.8-flash": 1_048_576,
    "gemini/gemini-2.5-pro": 1_048_576,
    # xAI
    "xai/grok-4.7": 500_000,
    "xai/grok-4.6": 500_000,
    "xai/grok-4.5": 500_000,
    "xai/grok-4.3": 1_000_000,
    "xai/grok-4.20-0309-reasoning": 1_000_000,
    "xai/grok-4.20-0309-non-reasoning": 1_000_000,
    "xai/grok-4.20-multi-agent-0309": 1_000_000,
    "xai/grok-build-0.1": 256_000,
    # Cohere
    "cohere/command-a-plus-05-2026": 128_000,
    "cohere/command-a-03-2025": 256_000,
    "cohere/command-a-reasoning-08-2025": 256_000,
    "cohere/command-a-vision-07-2025": 128_000,
    "cohere/command-a-translate-08-2025": 8_000,
    "cohere/command-r7b-12-2024": 128_000,
    "cohere/command-r-08-2024": 128_000,
    "cohere/command-r-plus-08-2024": 128_000,
    # Perplexity
    "perplexity/nemotron-3.5-super-120b-a12b": 1_048_576,
}

#: Provider aliases that share a registry namespace.
_PROVIDER_ALIASES = {"google": "gemini", "grok": "xai", "azure": "openai"}

#: Model-family fallbacks: ``(provider, id prefix, window)``. The longest
#: matching prefix wins. Only families whose *smallest* member has this
#: window are listed, so an unlisted model is never over-estimated
#: (over-estimating delays compaction until the provider rejects the turn).
CONTEXT_WINDOW_FAMILIES: tuple[tuple[str, str, int], ...] = (
    ("anthropic", "claude-", 200_000),
    ("openai", "gpt-6", 1_050_000),
    ("openai", "gpt-4.1", 1_047_576),
    ("openai", "gpt-", 128_000),
    ("openai", "o3", 200_000),
    ("openai", "o1", 128_000),
    ("openai", "o4", 128_000),
    ("xai", "grok-4.20", 1_000_000),
)

# Kept for compatibility with callers that imported them.
ANTHROPIC_PREFIXES = ("claude-",)
OPENAI_PREFIXES = ("gpt-4", "o1", "o3", "o4")


def registry_window(provider: str, model: str) -> int | None:
    """The built-in window for ``provider/model``: exact id, then family."""
    namespace = _PROVIDER_ALIASES.get(provider.lower(), provider.lower())
    name = normalize_model_id(model)
    exact = CONTEXT_WINDOW_REGISTRY.get(f"{namespace}/{name}")
    if exact is not None:
        return exact
    best: tuple[int, int] | None = None
    for family_provider, prefix, window in CONTEXT_WINDOW_FAMILIES:
        if family_provider == namespace and name.startswith(prefix):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), window)
    return best[1] if best else None


# Default fallback when nothing matches
DEFAULT_CONTEXT_WINDOW = 128_000


# ─────────────────────────────────────────────────────────────────────
# Resolver
# ─────────────────────────────────────────────────────────────────────


#: ``(provider, model) -> window`` set by the user (e.g. ``models.json``).
UserOverride = Callable[[str, str], "int | None"]
#: ``provider -> {model id: window}`` from the provider's live model listing,
#: or ``None`` when it could not be fetched.
CatalogFetcher = Callable[[str], Awaitable["dict[str, int] | None"]]

#: After a failed catalog fetch, wait this long before trying that provider again.
CATALOG_RETRY_S = 600.0

_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def normalize_model_id(model: str) -> str:
    """Lower-case, without a vendor prefix (``anthropic/``) or date suffix."""
    name = model.strip().lower()
    if name.startswith("models/"):
        name = name[len("models/") :]
    name = name.rsplit("/", 1)[-1] if "/" in name else name
    return _DATE_SUFFIX.sub("", name)


class ContextWindowResolver:
    """Resolves the context_window for a given provider + model name.

    Strategy, first hit wins:
    0. A window learned from a provider error (:meth:`override`)
    1. The user's own setting (``user_override`` hook, e.g. ``models.json``)
    2. The provider's live model listing (``catalog`` hook; cached per
       provider for the resolver's lifetime, retried after a failure)
    3. Static registry lookup (exact id, then the model family)
    4. For Ollama: query /api/show (with cache)
    5. For OpenRouter: query /api/v1/models (with cache)
    6. For vLLM: query /v1/models ``max_model_len`` (with cache)
    7. Fallback: DEFAULT_CONTEXT_WINDOW

    Args:
        ollama_base_url: Base URL for Ollama API (default: http://localhost:11434).
        openrouter_api_key: Optional OpenRouter API key for model queries.
        vllm_base_url: Base URL for a vLLM server (default:
            http://localhost:8000/v1).
    """

    def __init__(
        self,
        ollama_base_url: str = "http://localhost:11434",
        openrouter_api_key: str | None = None,
        vllm_base_url: str | None = None,
        *,
        user_override: UserOverride | None = None,
        catalog: CatalogFetcher | None = None,
    ) -> None:
        """Initialize the resolver with optional API endpoints and hooks."""
        self._user_override = user_override
        self._catalog = catalog
        self._catalogs: dict[str, dict[str, int]] = {}
        self._catalog_failed_at: dict[str, float] = {}
        self._ollama_base_url = ollama_base_url.rstrip("/")
        self._openrouter_api_key = openrouter_api_key
        self._vllm_base_url = (vllm_base_url or "http://localhost:8000/v1").rstrip("/")
        self._ollama_cache: dict[str, int] = {}
        self._openrouter_cache: dict[str, int] = {}
        self._vllm_cache: dict[str, int] = {}
        # Whole-list caches for providers that expose context length in a
        # *listing* endpoint (/models). Fetching per-model re-downloaded the
        # entire catalog on every switch to an uncached model (OpenRouter's
        # list is large); one fetch now serves every model of that provider.
        self._openrouter_windows: dict[str, int] | None = None
        self._vllm_windows: dict[str, int] | None = None
        self._overrides: dict[str, int] = {}

    # ── Public ────────────────────────────────────────────────────────

    def override(
        self,
        provider: str,
        model: str,
        context_window: int,
    ) -> None:
        """Pin the context window for ``provider/model`` (I-91).

        Used when the provider reveals the real limit in an error
        message (e.g. vLLM's "maximum context length is 8192 tokens")
        for a model that the registry/dynamic lookups could not
        resolve. Takes precedence over every other resolution source
        for this resolver instance.
        """
        if context_window > 0:
            self._overrides[f"{provider}/{model}"] = int(context_window)

    def rebind_endpoints(
        self,
        *,
        ollama_base_url: str,
        openrouter_api_key: str | None,
        vllm_base_url: str,
    ) -> None:
        """Point the resolver at possibly-new provider endpoints, in place.

        The resolver is built once (by the summarizer middleware and by the
        CLI controller); a provider switch or a changed ``models.json``
        base-url override at runtime must not leave it querying the previous
        endpoint. Only an endpoint that actually changed has its cache
        dropped, and every window learned via :meth:`override` is preserved —
        an unchanged provider must not be re-fetched, nor a limit discovered
        from a provider error forgotten.
        """
        normalized_ollama = ollama_base_url.rstrip("/")
        if normalized_ollama != self._ollama_base_url:
            self._ollama_base_url = normalized_ollama
            self._ollama_cache.clear()

        if openrouter_api_key != self._openrouter_api_key:
            self._openrouter_api_key = openrouter_api_key
            self._openrouter_cache.clear()
            self._openrouter_windows = None

        normalized_vllm = vllm_base_url.rstrip("/")
        if normalized_vllm != self._vllm_base_url:
            self._vllm_base_url = normalized_vllm
            self._vllm_cache.clear()
            self._vllm_windows = None

    async def resolve(
        self,
        provider: str,
        model: str,
    ) -> int:
        """Returns the context_window in tokens for the given model."""
        key = f"{provider}/{model}"

        # 0. Explicit override (learned from provider error messages)
        if key in self._overrides:
            return self._overrides[key]

        # 1. The user's own setting
        if self._user_override is not None:
            try:
                configured = self._user_override(provider, model)
            except Exception:  # noqa: BLE001 - a broken hook must not break turns
                configured = None
            if isinstance(configured, int) and configured > 0:
                return configured

        # 2. The provider's live model listing
        listed = await self._from_catalog(provider, model)
        if listed is not None:
            return listed

        # 3. Static registry: exact id, then model family
        static = registry_window(provider, model)
        if static is not None:
            return static

        # 3. Ollama — dynamic query
        if provider == "ollama":
            return await self._resolve_ollama(model)

        # 4. OpenRouter — dynamic query
        if provider == "openrouter":
            return await self._resolve_openrouter(model)

        # 5. vLLM — dynamic query
        if provider == "vllm":
            return await self._resolve_vllm(model)

        # 6. Fallback
        return DEFAULT_CONTEXT_WINDOW

    # ── Provider catalog ──────────────────────────────────────────────

    async def _from_catalog(self, provider: str, model: str) -> int | None:
        if self._catalog is None:
            return None
        windows = self._catalogs.get(provider)
        if windows is None:
            failed_at = self._catalog_failed_at.get(provider)
            if failed_at is not None and time.monotonic() - failed_at < CATALOG_RETRY_S:
                return None
            try:
                fetched = await self._catalog(provider)
            except Exception as exc:  # noqa: BLE001 - best-effort lookup
                logger.debug("Model catalog for %s unavailable: %s", provider, exc)
                fetched = None
            if fetched is None:
                # Failed: retry later. An empty mapping is a successful
                # listing without windows (OpenAI) and is cached as such.
                self._catalog_failed_at[provider] = time.monotonic()
                return None
            windows = {
                key.strip().lower(): value
                for key, value in fetched.items()
                if isinstance(key, str) and isinstance(value, int) and value > 0
            }
            self._catalogs[provider] = windows
            self._catalog_failed_at.pop(provider, None)
        for candidate in (model.strip().lower(), normalize_model_id(model)):
            if candidate in windows:
                return windows[candidate]
        return None

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
            # I-112: the styled notice channel (CLI warnings hook) renders the
            # logger record — no separate warnings.warn, which would double-
            # notify. The log record is also the issue-#23 traceability signal.
            logger.warning(
                "Ollama context window lookup failed for %r; "
                "falling back to default (%d tokens): %s",
                model,
                DEFAULT_CONTEXT_WINDOW,
                exc,
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

        if self._openrouter_windows is None:
            self._openrouter_windows = await self._load_openrouter_windows()

        windows = self._openrouter_windows
        if windows is not None:
            val = windows.get(model)
            if val is not None:
                self._openrouter_cache[model] = val
                return val

        self._openrouter_cache[model] = DEFAULT_CONTEXT_WINDOW
        return DEFAULT_CONTEXT_WINDOW

    async def _load_openrouter_windows(self) -> dict[str, int] | None:
        """Fetch OpenRouter's model catalog once, as ``id → context_length``.

        Returns ``None`` on failure (soft-fail: callers fall back to the
        default) so a later, different model can retry the fetch.
        """
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
                    windows: dict[str, int] = {}
                    for m in data.get("data", []):
                        ctx = m.get("context_length")
                        if ctx is None:
                            ctx = m.get("top_provider", {}).get("context_length")
                        if ctx is not None:
                            windows[m.get("id", "")] = int(ctx)
                    return windows
        except (httpx.HTTPError, ValueError) as exc:
            # I-112: notice channel renders this log record — no separate
            # warnings.warn (would double-notify); log = issue-#23 trace.
            logger.warning(
                "OpenRouter context window lookup failed; "
                "falling back to default (%d tokens): %s",
                DEFAULT_CONTEXT_WINDOW,
                exc,
            )
        return None

    # ── vLLM ──────────────────────────────────────────────────────────

    async def _resolve_vllm(self, model: str) -> int:
        """Resolve the context window from a vLLM server's ``/v1/models``.

        vLLM exposes each served model's ``max_model_len`` in the
        ``/v1/models`` list response. The whole list is fetched once and
        cached (see also the OpenRouter list cache); a later switch to a
        different served model reuses it. Same soft-fail policy as
        Ollama/OpenRouter: best-effort lookup with default fallback.
        """
        if model in self._vllm_cache:
            return self._vllm_cache[model]

        if self._vllm_windows is None:
            self._vllm_windows = await self._load_vllm_windows()

        windows = self._vllm_windows
        if windows is not None:
            val = windows.get(model)
            if val is not None:
                self._vllm_cache[model] = val
                return val
            # I-112: server is up but the model id is missing (or carries no
            # max_model_len) — the warnings.warn is the single signal here
            # (no paired log), so the CLI notice channel shows it once, styled.
            warnings.warn(
                f"vLLM /v1/models response did not include {model!r} "
                f"with a max_model_len; using default "
                f"({DEFAULT_CONTEXT_WINDOW} tokens)",
                UserWarning,
                stacklevel=2,
            )

        self._vllm_cache[model] = DEFAULT_CONTEXT_WINDOW
        return DEFAULT_CONTEXT_WINDOW

    async def _load_vllm_windows(self) -> dict[str, int] | None:
        """Fetch vLLM's served models once, as ``id → max_model_len``.

        Returns ``None`` when the server is unreachable (soft-fail) so a later
        model can retry the fetch.
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self._vllm_base_url}/models")
                if resp.status_code == 200:
                    data = resp.json()
                    windows: dict[str, int] = {}
                    for m in data.get("data", []):
                        ctx = m.get("max_model_len")
                        if ctx is not None:
                            windows[m.get("id", "")] = int(ctx)
                    return windows
        except (httpx.HTTPError, ValueError) as exc:
            # I-112: notice channel renders this log record — no separate
            # warnings.warn (would double-notify); log = issue-#23 trace.
            logger.warning(
                "vLLM context window lookup failed; "
                "falling back to default (%d tokens): %s",
                DEFAULT_CONTEXT_WINDOW,
                exc,
            )
        return None

    # ── Cache management ──────────────────────────────────────────────

    def clear_cache(self) -> None:
        """Clears the internal caches."""
        self._catalogs.clear()
        self._catalog_failed_at.clear()
        self._ollama_cache.clear()
        self._openrouter_cache.clear()
        self._vllm_cache.clear()
        self._openrouter_windows = None
        self._vllm_windows = None
