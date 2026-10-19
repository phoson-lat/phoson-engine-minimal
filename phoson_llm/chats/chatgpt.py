"""ChatGPT subscription adapter (Codex backend, OpenAI Responses API).

Unlike every other adapter in this package, ``ChatGPTChat`` does not talk to
the public OpenAI API: it speaks the **Responses API** exposed by the ChatGPT
subscription backend at ``https://chatgpt.com/backend-api/codex``, the same
surface the ``codex`` CLI uses. Requests are authenticated with an OAuth
access token minted by :mod:`phoson_llm.auth.chatgpt` (see ``/auth login
chatgpt``) plus a ``ChatGPT-Account-ID`` header — there is no API key.

Protocol notes (verified against ``openai/codex``, crate ``codex-rs``):

* ``POST {base}/responses`` with ``store: false`` — the subscription backend
  keeps no server-side conversation state, so every turn resends the whole
  history (which is exactly what Phoson's stateless adapters already do).
* The system prompt travels as the first ``input`` item with role
  ``developer`` (the backend has no ``instructions`` field), tool calls use
  the flattened Responses function shape and tool results are
  ``function_call_output`` items.
* Streaming is SSE; the interesting events are ``response.output_text.delta``,
  ``response.reasoning_summary_text.delta``, ``response.output_item.done``
  (complete messages, reasoning items and function calls),
  ``response.completed`` / ``response.incomplete`` (usage, stop reason) and
  ``response.failed`` (terminal error).
* ``temperature`` and ``max_output_tokens`` are deliberately not sent: the
  subscription models reject sampling controls and manage their own output
  budget. Reasoning is requested through ``reasoning.effort`` and summaries
  come back as a separate channel mapped to Phoson's reasoning events.

This adapter depends only on ``httpx`` — no vendor SDK — so the "no API key"
provider stays as cheap to import as the rest of the package.
"""

import json
import logging
from typing import Any
from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

import httpx

from phoson_llm.utils import (
    CONTEXT_LENGTH_ERROR_CODE,
    map_error_code,
    scrub_surrogates,
    normalize_stop_reason,
    is_context_length_error,
)
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ReasoningDoneEvent,
    ToolCallDeltaEvent,
    ReasoningStartEvent,
    ReasoningTokenEvent,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.auth.chatgpt import (
    ChatGPTTokens,
    ChatGPTAuthError,
    load_tokens,
    save_tokens,
    refresh_tokens,
    get_valid_tokens,
)

logger = logging.getLogger(__name__)

# ─── Endpoint constants ──────────────────────────────────────────────────────

#: ChatGPT subscription backend (the ``codex`` CLI's ChatGPT base URL).
CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

#: Path appended to the base URL for a model turn.
RESPONSES_PATH = "/responses"

#: Model catalogue for the picker: ``GET {base}/models?client_version=…``.
MODELS_PATH = "/models"

#: Value sent in the ``originator`` header. Identifies the client to the
#: backend for telemetry/attribution; arbitrary client names are accepted.
DEFAULT_ORIGINATOR = "phoson_cli"

#: Value reported as the client version in the models query string.
DEFAULT_CLIENT_VERSION = "1.0.0"

#: No response for this long (no bytes at all) aborts the turn.
DEFAULT_TIMEOUT = httpx.Timeout(60.0, read=900.0)

#: Phoson reasoning levels → ChatGPT/Responses ``reasoning.effort`` values.
#: The extended Phoson level ``max`` has no backend equivalent; ``xhigh`` is
#: the highest level the subscription models accept.
_EFFORT_MAP: dict[str, str] = {"max": "xhigh"}

#: Extracted from an error payload when the backend reports one.
_SSE_ERROR_CODES: dict[str, str] = {
    "rate_limit_exceeded": "rate_limit",
    "insufficient_quota": "quota",
    "invalid_request_error": "invalid_request",
    "server_is_overloaded": "overloaded",
    "content_filter": "content_filter",
}


# ─── Request construction ────────────────────────────────────────────────────


def _text_part(text: str, *, role: str) -> dict[str, Any]:
    """One content part for an input message (``input_text``/``output_text``)."""
    kind = "output_text" if role == "assistant" else "input_text"
    return {"type": kind, "text": text}


def _image_item(source: str, detail: str | None) -> dict[str, Any] | None:
    """Build an ``input_image`` part, or ``None`` when the file is missing.

    ``file://`` sources are inlined as ``data:`` URLs; remote URLs and
    pre-encoded ``data:`` URLs pass through unchanged.
    """
    if source.startswith("file://"):
        from phoson_llm.utils import load_file_as_base64, missing_attachment_placeholder

        path = source[7:]
        data = load_file_as_base64(path)
        if data is None:
            placeholder = missing_attachment_placeholder("image", path)
            return {"type": "input_text", "text": placeholder}
        source = data
    part: dict[str, Any] = {"type": "input_image", "image_url": source}
    if detail:
        part["detail"] = detail
    return part


def _message_items(msg: Message) -> list[dict[str, Any]]:
    """Convert one Phoson message into Responses ``input`` items.

    A message can expand to several items: assistant turns carrying both text
    and tool calls become an assistant message plus one ``function_call`` per
    call, and each ``ToolResultBlock`` becomes its own ``function_call_output``
    item (the Responses API has no "tool role" message).

    Historical assistant reasoning (``Message.reasoning``) is intentionally
    dropped: the Responses API only accepts reasoning items that carry the
    provider's own encrypted payload, so replayed plain text would be invalid.
    """
    from phoson_llm.schemas import (
        TextBlock,
        ImageBlock,
        ToolUseBlock,
        ToolResultBlock,
    )

    role = msg.role
    content = msg.content

    if isinstance(content, str):
        if not content:
            return []
        part = _text_part(content, role=role)
        return [{"type": "message", "role": role, "content": [part]}]

    items: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            if block.text:
                parts.append(_text_part(block.text, role=role))
        elif isinstance(block, ImageBlock):
            part = _image_item(block.source, block.detail)
            if part is not None:
                parts.append(part)
        elif isinstance(block, ToolUseBlock):
            items.append(
                {
                    "type": "function_call",
                    "name": block.tool_name,
                    "arguments": json.dumps(block.args, ensure_ascii=False),
                    "call_id": block.tool_call_id,
                }
            )
        elif isinstance(block, ToolResultBlock):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.tool_call_id,
                    "output": block.result,
                }
            )
        # Other block types (audio/video/document) have no Responses input
        # equivalent on this backend and are dropped.
    if parts:
        items.insert(0, {"type": "message", "role": role, "content": parts})
    return items


def _input_items(messages: list[Message], system: str | None) -> list[dict[str, Any]]:
    """Build the ``input`` array: developer instructions first, then turns.

    ``system`` (``ModelConfig.system``) replaces the conversation's own system
    messages when set, mirroring the other adapters. All system text is fused
    into a single ``developer`` item — one stable prefix, exactly what the
    prompt cache wants.
    """
    instructions = system
    if instructions is None:
        chunks = [
            m.content
            for m in messages
            if m.role == "system" and isinstance(m.content, str) and m.content
        ]
        instructions = "\n\n".join(chunks) if chunks else None

    items: list[dict[str, Any]] = []
    if instructions:
        items.append(
            {
                "type": "message",
                "role": "developer",
                "content": [_text_part(instructions, role="developer")],
            }
        )
    for msg in messages:
        if msg.role == "system":
            continue
        items.extend(_message_items(msg))
    return items


def _tools_payload(tools: list[ToolDefinition] | None) -> list[dict[str, Any]] | None:
    """Convert Phoson tools to the Responses API's flattened function shape."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
            "strict": False,
        }
        for t in tools
    ]


def build_responses_body(
    *,
    messages: list[Message],
    config: ModelConfig,
    tools: list[ToolDefinition] | None = None,
) -> dict[str, Any]:
    """Build the JSON body for ``POST {base}/responses``."""
    body: dict[str, Any] = {
        "model": config.model,
        "input": _input_items(messages, config.system),
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "store": False,
        "stream": True,
        # Ask for reasoning back in the stream: without this the backend sends
        # the summary channel only sporadically.
        "include": ["reasoning.encrypted_content"],
    }

    effort = config.reasoning_effort
    if effort:
        body["reasoning"] = {
            "effort": _EFFORT_MAP.get(effort, effort),
            "summary": "auto",
        }

    payload_tools = _tools_payload(tools)
    if payload_tools:
        body["tools"] = payload_tools

    if config.session_id:
        # Groups the turns of one conversation for the prompt cache, the same
        # role codex's per-thread prompt_cache_key plays.
        body["prompt_cache_key"] = str(config.session_id)[:64]

    # Scrub lone surrogates so serialisation cannot blow up on stray bytes
    # from tool output (see phoson_llm.utils.scrub_surrogates, #270).
    return scrub_surrogates(body)


def build_headers(
    tokens: ChatGPTTokens,
    *,
    originator: str = DEFAULT_ORIGINATOR,
    fedramp: bool = False,
) -> dict[str, str]:
    """Auth headers for the Codex backend (bearer + account id)."""
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "originator": originator,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    if tokens.account_id:
        headers["ChatGPT-Account-ID"] = tokens.account_id
    if fedramp:
        headers["X-OpenAI-Fedramp"] = "true"
    return headers


# ─── SSE parsing ─────────────────────────────────────────────────────────────


async def _iter_sse_payloads(
    lines: AsyncIterator[str],
) -> AsyncIterator[dict[str, Any]]:
    """Yield the JSON object of every SSE ``data:`` frame in ``lines``.

    The event *type* is read from the JSON body (``type``), so ``event:``
    lines are ignored — they duplicate it. Comments (``:``), keep-alives and
    the ``[DONE]`` sentinel are skipped, and a malformed frame is logged
    rather than killing the turn.
    """
    buffer: list[str] = []

    async def flush() -> dict[str, Any] | None:
        if not buffer:
            return None
        payload = "\n".join(buffer)
        buffer.clear()
        if payload.strip() in ("", "[DONE]"):
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("ignoring malformed SSE frame: %.200s", payload)
            return None
        return parsed if isinstance(parsed, dict) else None

    async for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            parsed = await flush()
            if parsed is not None:
                yield parsed
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
        # ``event:`` / ``id:`` / ``retry:`` lines carry no extra information.
    parsed = await flush()
    if parsed is not None:
        yield parsed


@dataclass
class _FunctionCallTracker:
    """Accumulates streamed function-call fragments into complete calls.

    The backend sends ``response.output_item.added`` (name/call id),
    ``response.function_call_arguments.delta`` (argument fragments) and
    ``response.output_item.done`` (the authoritative complete call). The
    ``done`` payload wins whenever it is available; the accumulated fragments
    are the fallback for streams that omit it.
    """

    names: dict[str, str] = field(default_factory=dict)
    call_ids: dict[str, str] = field(default_factory=dict)
    args: dict[str, str] = field(default_factory=dict)
    indexes: dict[str, int] = field(default_factory=dict)

    def track(self, item: dict[str, Any], index: int) -> None:
        """Register a function call announced by ``output_item.added``."""
        item_id = str(item.get("id") or item.get("call_id") or index)
        self.indexes[item_id] = index
        if name := item.get("name"):
            self.names[item_id] = str(name)
        if call_id := item.get("call_id"):
            self.call_ids[item_id] = str(call_id)
        self.args.setdefault(item_id, "")

    def feed(self, item_id: str, chunk: str) -> ToolCallDeltaEvent | None:
        """Ingest an argument fragment; returns the delta event to emit."""
        self.args[item_id] = self.args.get(item_id, "") + chunk
        return ToolCallDeltaEvent(
            index=self.indexes.get(item_id, 0),
            tool_name=self.names.get(item_id, ""),
            args_chunk=chunk,
        )

    def finish(self, item: dict[str, Any], index: int) -> ToolCallEvent:
        """Build the complete call from a ``output_item.done`` payload."""
        item_id = str(item.get("id") or item.get("call_id") or index)
        name = str(item.get("name") or self.names.get(item_id, ""))
        call_id = str(item.get("call_id") or self.call_ids.get(item_id, ""))
        raw = item.get("arguments")
        if not isinstance(raw, str) or not raw:
            raw = self.args.get(item_id, "")
        return ToolCallEvent(
            index=self.indexes.get(item_id, index),
            tool_call_id=call_id or f"call_{index}",
            tool_name=name or "unknown",
            args=_parse_tool_args(raw),
        )


def _parse_tool_args(raw: str) -> dict[str, Any]:
    """Best-effort parse of a function call's JSON argument string."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("could not parse tool arguments (truncated JSON?): %.200s", raw)
        return {"_raw": raw, "_truncated": True}
    if isinstance(parsed, dict):
        return parsed
    return {"_raw": raw}


def _item_text(item: dict[str, Any], *, kinds: tuple[str, ...]) -> str:
    """Concatenate the text of the content/summary parts matching ``kinds``."""
    chunks: list[str] = []
    for key in ("content", "summary"):
        parts = item.get(key)
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") in kinds:
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    return "".join(chunks)


def _usage_from_response(response: dict[str, Any]) -> TokenUsage | None:
    """Map the Responses ``usage`` object onto Phoson's :class:`TokenUsage`."""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    return TokenUsage(
        input=int(usage.get("input_tokens") or 0),
        output=int(usage.get("output_tokens") or 0),
        cache_read=int(details.get("cached_tokens") or 0),
        cache_write=int(details.get("cache_write_tokens") or 0),
    )


def _sse_error(payload: dict[str, Any]) -> tuple[str, str, bool]:
    """Extract ``(message, code, retryable)`` from a failure event."""
    response = payload.get("response")
    error: dict[str, Any] = {}
    if isinstance(response, dict) and isinstance(response.get("error"), dict):
        error = response["error"]
    elif isinstance(payload.get("error"), dict):
        error = payload["error"]

    message = str(error.get("message") or "the ChatGPT backend reported a failure")
    raw_code = str(error.get("code") or payload.get("code") or "")

    if raw_code == "context_length_exceeded" or is_context_length_error(None, message):
        return message, CONTEXT_LENGTH_ERROR_CODE, False
    if raw_code in _SSE_ERROR_CODES:
        code = _SSE_ERROR_CODES[raw_code]
        return message, code, code in ("rate_limit", "overloaded")
    if "rate_limit" in raw_code:
        return message, "rate_limit", True
    if raw_code.startswith("server") or "overload" in raw_code:
        return message, "overloaded", True
    return message, raw_code or "provider_error", False


# ─── Model catalogue ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChatGPTModel:
    """One entry of the subscription model catalogue."""

    slug: str
    display_name: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.slug


async def list_models(
    *,
    base_url: str = CHATGPT_CODEX_BASE_URL,
    auth_file: str | Path | None = None,
    client_version: str = DEFAULT_CLIENT_VERSION,
    tokens: ChatGPTTokens | None = None,
    originator: str = DEFAULT_ORIGINATOR,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ChatGPTModel]:
    """Fetch the models the signed-in subscription can use.

    Calls ``GET {base}/models?client_version=…``, which is what the codex CLI
    uses to populate its picker. The slugs returned here are exactly the ones
    to pass to ``ChatGPTChat``.

    Raises:
        ChatGPTAuthError: When no usable login is stored.
    """
    if tokens is None:
        tokens = await get_valid_tokens(
            path=auth_file, client=client, transport=transport
        )
    url = f"{base_url.rstrip('/')}{MODELS_PATH}"
    params = {"client_version": client_version}
    headers = build_headers(tokens, originator=originator)
    headers.pop("Content-Type", None)
    headers.pop("Accept", None)

    if client is not None:
        response = await client.get(url, params=params, headers=headers)
    else:
        async with httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(30.0)
        ) as owned:
            response = await owned.get(url, params=params, headers=headers)

    if response.status_code != 200:
        raise ChatGPTAuthError(
            f"model listing failed (HTTP {response.status_code}): {response.text[:200]}"
        )
    payload = response.json()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    result: list[ChatGPTModel] = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug") or entry.get("id")
        if not isinstance(slug, str) or not slug.strip():
            continue
        display = entry.get("display_name") or entry.get("title") or ""
        result.append(
            ChatGPTModel(
                slug=slug,
                display_name=display if isinstance(display, str) else "",
            )
        )
    return result


def list_models_sync(**kwargs: Any) -> list[ChatGPTModel]:
    """Synchronous :func:`list_models` for click-style code paths."""
    import asyncio

    return asyncio.run(list_models(**kwargs))


# ─── Adapter ─────────────────────────────────────────────────────────────────


class ChatGPTChat(BaseLLMChat):
    """ChatGPT subscription adapter (Codex Responses backend).

    Args:
        api_key: An explicit OAuth access token. Leave unset to use the stored
            login (``~/.phoson/auth.json``), which is loaded lazily and
            refreshed automatically when it is about to expire.
        base_url: Backend override. Defaults to
            :data:`CHATGPT_CODEX_BASE_URL`; useful for gateways and tests.
        account_id: ``ChatGPT-Account-ID`` override for the access token.
            Taken from the stored credentials when omitted.
        auth_file: Credential file override (defaults to the Phoson store).
        originator: ``originator`` header value.
        fedramp: Send ``X-OpenAI-Fedramp: true`` (FedRAMP accounts).
        timeout: Per-request timeout (connect/read).
        transport: ``httpx`` transport override (tests).

    The adapter needs no model-specific configuration: pass any slug returned
    by :func:`list_models` in ``ModelConfig.model``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        account_id: str | None = None,
        auth_file: str | Path | None = None,
        originator: str = DEFAULT_ORIGINATOR,
        fedramp: bool = False,
        timeout: httpx.Timeout | float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key or None
        self._account_id = account_id
        self._auth_file = auth_file
        self._originator = originator
        self._fedramp = fedramp
        self._transport = transport
        self.base_url = (base_url or CHATGPT_CODEX_BASE_URL).rstrip("/")
        if timeout is None:
            self._timeout: httpx.Timeout | float = DEFAULT_TIMEOUT
        else:
            self._timeout = timeout

    @property
    def responses_url(self) -> str:
        return f"{self.base_url}{RESPONSES_PATH}"

    # ── Credentials ──────────────────────────────────────────────────────────

    async def _credentials(self, *, force_refresh: bool = False) -> ChatGPTTokens:
        """Resolve the credential set for one request.

        An explicit ``api_key`` is used as-is (no store, no refresh); otherwise
        the stored login is loaded and refreshed when needed.
        """
        if self._api_key:
            return ChatGPTTokens(
                access_token=self._api_key,
                refresh_token="",
                account_id=self._account_id,
            )
        tokens = await get_valid_tokens(
            path=self._auth_file,
            force_refresh=force_refresh,
            transport=self._transport,
        )
        if self._account_id:
            tokens.account_id = self._account_id
        return tokens

    async def _refresh_after_401(self) -> bool:
        """Refresh the stored credentials after an auth rejection.

        Returns ``True`` when a *new* token was obtained, meaning the request
        is worth retrying once. The backend can reject an access token that
        still looks unexpired (e.g. the session was revoked server-side), so a
        single refresh-and-retry turns a spurious failure into a normal turn.
        """
        if self._api_key:
            return False
        stored = load_tokens(self._auth_file)
        if stored is None or not stored.refresh_token:
            return False
        try:
            refreshed = await refresh_tokens(
                stored.refresh_token, transport=self._transport
            )
        except ChatGPTAuthError as exc:
            logger.debug("refresh after 401 failed: %s", exc)
            return False
        refreshed.email = refreshed.email or stored.email
        refreshed.plan_type = refreshed.plan_type or stored.plan_type
        refreshed.id_token = refreshed.id_token or stored.id_token
        refreshed.account_id = (
            self._account_id or refreshed.account_id or stored.account_id
        )
        save_tokens(refreshed, self._auth_file)
        return refreshed.access_token != stored.access_token

    def _headers(self, tokens: ChatGPTTokens) -> dict[str, str]:
        headers = build_headers(
            tokens, originator=self._originator, fedramp=self._fedramp
        )
        if self._account_id:
            headers["ChatGPT-Account-ID"] = self._account_id
        return headers

    # ── Streaming ────────────────────────────────────────────────────────────

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream one turn from the ChatGPT subscription backend.

        Emits the standard event sequence documented by
        :class:`phoson_llm.chats.base.BaseLLMChat`. Reasoning summaries and
        tool calls follow the provider's own item order, so a turn with tool
        calls may interleave several reasoning blocks with tool events.
        """
        yield LLMStartEvent(model=config.model, message_count=len(messages))

        body = build_responses_body(messages=messages, config=config, tools=tools)
        attempts = 2 if self._api_key is None else 1
        # A 401 refresh already stores a fresh token: the retried attempt must
        # reuse it rather than burn a second refresh-token round-trip.
        refreshed = False

        for attempt in range(attempts):
            try:
                tokens = await self._credentials(
                    force_refresh=attempt > 0 and not refreshed
                )
            except ChatGPTAuthError as exc:
                yield ErrorEvent(
                    message=str(exc), code="auth", retryable=not exc.terminal
                )
                return

            retry_auth = False
            try:
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=self._timeout
                ) as client:
                    async with client.stream(
                        "POST",
                        self.responses_url,
                        json=body,
                        headers=self._headers(tokens),
                    ) as response:
                        if response.status_code != 200:
                            detail = await _read_error_body(response)
                            if (
                                response.status_code == 401
                                and attempt + 1 < attempts
                                and await self._refresh_after_401()
                            ):
                                retry_auth = True
                                refreshed = True
                            else:
                                yield ErrorEvent(
                                    message=detail,
                                    code=_http_error_code(response.status_code, detail),
                                    retryable=response.status_code == 429
                                    or 500 <= response.status_code < 600,
                                )
                                return
                        else:
                            async for event in self._consume(
                                response.aiter_lines(), config=config
                            ):
                                yield event
                            return
            except httpx.HTTPError as exc:
                yield ErrorEvent(
                    message=f"ChatGPT backend request failed: {exc}",
                    code="connection_error",
                    retryable=True,
                )
                return

            if not retry_auth:
                return

    async def _consume(
        self, lines: AsyncIterator[str], *, config: ModelConfig
    ) -> AsyncIterator[LLMEvent]:
        """Translate the SSE stream into Phoson events.

        Always terminates the stream with ``UsageEvent`` + ``LLMDoneEvent``, or
        with a single ``ErrorEvent`` when the backend reports a failure. The
        partial text produced before the failure is still emitted as tokens.
        """
        text_acc = ""
        reasoning_acc = ""
        reasoning_open = False
        streamed_items: set[str] = set()
        calls = _FunctionCallTracker()
        next_index = 0
        has_tool_calls = False
        stop_reason: str | None = None
        usage: TokenUsage | None = None
        failure: tuple[str, str, bool] | None = None

        async for payload in _iter_sse_payloads(lines):
            kind = payload.get("type")
            if kind == "response.output_text.delta":
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    text_acc += delta
                    yield TokenEvent(content=delta)

            elif kind in (
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            ):
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    if not reasoning_open:
                        reasoning_open = True
                        yield ReasoningStartEvent()
                    reasoning_acc += delta
                    yield ReasoningTokenEvent(content=delta)

            elif kind == "response.output_item.added":
                item = payload.get("item")
                if isinstance(item, dict):
                    item_id = item.get("id")
                    if isinstance(item_id, str):
                        streamed_items.add(item_id)
                    if item.get("type") == "function_call":
                        calls.track(item, index=next_index)
                        next_index += 1

            elif kind == "response.function_call_arguments.delta":
                delta = payload.get("delta")
                item_id = payload.get("item_id") or payload.get("call_id")
                if isinstance(delta, str) and delta and isinstance(item_id, str):
                    event = calls.feed(item_id, delta)
                    if event is not None:
                        yield event

            elif kind == "response.output_item.done":
                item = payload.get("item")
                if isinstance(item, dict):
                    async for event in _item_done_events(
                        item,
                        index=next_index,
                        calls=calls,
                        streamed_items=streamed_items,
                        text_acc=text_acc,
                        reasoning_acc=reasoning_acc,
                    ):
                        if isinstance(event, TokenEvent):
                            text_acc += event.content
                        elif isinstance(event, ToolCallEvent):
                            has_tool_calls = True
                            next_index += 1
                        elif isinstance(event, ReasoningTokenEvent):
                            if not reasoning_open:
                                reasoning_open = True
                                yield ReasoningStartEvent()
                            reasoning_acc += event.content
                        yield event

            elif kind in ("response.completed", "response.incomplete"):
                response = payload.get("response")
                response = response if isinstance(response, dict) else {}
                usage = _usage_from_response(response) or usage
                stop_reason = _terminal_stop_reason(
                    incomplete=kind == "response.incomplete",
                    response=response,
                    has_tool_calls=has_tool_calls,
                )

            elif kind in ("response.failed", "error"):
                failure = _sse_error(payload)
                break

        if reasoning_open or reasoning_acc:
            yield ReasoningDoneEvent(content=reasoning_acc)

        if failure is not None:
            message, code, retryable = failure
            yield ErrorEvent(message=message, code=code, retryable=retryable)
            return

        if usage is not None:
            # Subscription plans are not billed per token, so no meaningful USD
            # figure exists — the token counts are still reported for context
            # accounting and cost is marked unknown.
            yield UsageEvent(
                model=config.model, usage=usage, cost_usd=0.0, cost_known=False
            )

        if stop_reason is None:
            stop_reason = "tool_use" if has_tool_calls else "end_turn"

        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
            stop_reason=stop_reason,
        )


async def _item_done_events(
    item: dict[str, Any],
    *,
    index: int,
    calls: _FunctionCallTracker,
    streamed_items: set[str],
    text_acc: str,
    reasoning_acc: str,
) -> AsyncIterator[LLMEvent]:
    """Events produced by a completed ``output_item``.

    Text and reasoning that was already streamed as deltas is not repeated;
    content the backend only sent in the final item is emitted here so a
    non-incremental stream still produces a complete answer.
    """
    item_type = item.get("type")
    item_id = item.get("id")
    seen = isinstance(item_id, str) and item_id in streamed_items

    if item_type == "function_call":
        yield calls.finish(item, index)

    elif item_type == "message":
        body = _item_text(item, kinds=("output_text",))
        if not seen and body and body != text_acc:
            yield TokenEvent(content=body)
        # A refusal surfaces as ordinary assistant text plus a refusal stop
        # reason, so the user reads the model's explanation instead of silence.
        refusals = _item_text(item, kinds=("refusal",))
        if refusals and refusals not in text_acc:
            yield TokenEvent(content=refusals)

    elif item_type == "reasoning":
        body = _item_text(item, kinds=("summary_text", "reasoning_text"))
        if body and body != reasoning_acc and body not in reasoning_acc:
            yield ReasoningTokenEvent(content=body)


def _terminal_stop_reason(
    *, incomplete: bool, response: dict[str, Any], has_tool_calls: bool
) -> str:
    """Map the end of a Responses stream onto Phoson's stop-reason vocabulary.

    ``response.completed`` carries ``end_turn`` when the backend decided the
    turn is not actually finished (``false``), which is a pause, not an end.
    ``response.incomplete`` names why the model stopped early.
    """
    if incomplete:
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason == "max_output_tokens":
            return "max_tokens"
        if reason in ("content_filter", "safety"):
            return "refusal"
        if reason == "interrupted":
            return "pause_turn"
        return "other"

    end_turn = response.get("end_turn")
    if end_turn is False:
        return "pause_turn"
    if response.get("status") == "incomplete":
        return "max_tokens"
    if has_tool_calls:
        return "tool_use"
    if isinstance(end_turn, bool):
        return "end_turn"
    # The backend may omit ``end_turn`` entirely: fall back to the generic
    # OpenAI-compatible vocabulary ("completed" is not a finish_reason there,
    # so an unknown value must not be mistaken for a clean end).
    return normalize_stop_reason(response.get("status"), provider="openai_compat") or (
        "tool_use" if has_tool_calls else "end_turn"
    )


async def _read_error_body(response: httpx.Response) -> str:
    """Read and trim a failed response body for the error event."""
    try:
        raw = (await response.aread()).decode("utf-8", errors="replace")
    except httpx.HTTPError as exc:  # pragma: no cover - transport already failed
        return f"could not read the error body: {exc}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:300] or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:300]
        if isinstance(error, str):
            return error[:300]
        if isinstance(payload.get("message"), str):
            return payload["message"][:300]
    return str(payload)[:300]


def _http_error_code(status: int, detail: str) -> str:
    """Phoson error code for a failed HTTP response."""
    if status == 400 and is_context_length_error(status, detail):
        return CONTEXT_LENGTH_ERROR_CODE
    if status == 401:
        return "auth"
    if status == 403:
        return "permission"
    return map_error_code(status)


#: Codes this adapter can emit, for reference by the CLI error UI.
CHATGPT_ERROR_CODES: frozenset[str] = frozenset(
    {"auth", "permission", "rate_limit", "overloaded", "quota", "connection_error"}
    | {CONTEXT_LENGTH_ERROR_CODE}
)

#: Re-exported for the CLI's ``/auth status`` command.
__all__ = [
    "ChatGPTChat",
    "ChatGPTModel",
    "CHATGPT_CODEX_BASE_URL",
    "CHATGPT_ERROR_CODES",
    "build_headers",
    "build_responses_body",
    "list_models",
    "list_models_sync",
]
