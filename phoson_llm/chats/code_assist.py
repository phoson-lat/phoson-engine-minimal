"""Google Code Assist adapter (subscription backend, no API key).

Unlike :mod:`phoson_llm.chats.gemini`, which drives the *public* Gemini API
with an API key through ``google-genai``, ``CodeAssistChat`` speaks the
**Code Assist** surface the Gemini CLI uses: ``cloudcode-pa.googleapis.com``,
authenticated with an OAuth access token minted by
:mod:`phoson_llm.auth.code_assist` (see ``/auth login google-code-assist``).
There is no API key anywhere in this path, and no vendor SDK: the adapter is
``httpx``-only, like the ChatGPT subscription adapter.

Protocol notes (the ``v1internal`` surface):

* ``POST {base}:streamGenerateContent?alt=sse`` carries the whole conversation
  each turn — the backend keeps no server-side thread state, which matches
  Phoson's stateless adapters. The body wraps a standard
  ``GenerateContentRequest`` under ``request``, alongside ``model``, ``project``
  (the ``cloudaicompanionProject`` the Code Assist service is onboarded on),
  ``user_prompt_id`` and an optional ``session_id``.
* The project id is discovered once ("onboarding"): ``:loadCodeAssist`` reports
  the tier and any existing project; when the account has none, the *free* tier
  is provisioned through ``:onboardUser`` (a long-running operation) — that call
  must **omit** the project field, since sending one for a free-tier account is
  rejected as a precondition failure.
* Streamed frames are SSE ``data:`` lines, each one a JSON object of the shape
  ``{"response": <GenerateContentResponse>, "traceId": …, "consumedCredits": […]}``.
  Text arrives as ``candidates[].content.parts[].text`` (a part with
  ``thought: true`` is reasoning), tool calls as complete ``functionCall`` parts,
  usage from ``usageMetadata`` and the stop reason from ``candidates[].finishReason``.
* ``temperature`` and ``maxOutputTokens`` are sent in ``generationConfig`` when
  the caller set them; reasoning is requested through
  ``thinkingConfig.includeThoughts``/``thinkingBudget``.

The model catalogue comes from ``:retrieveUserQuota``, which lists the models a
subscription can actually call (there is no public "list models" endpoint on the
subscription surface).
"""

import json
import uuid
import logging
import platform
from typing import Any
from pathlib import Path
from dataclasses import dataclass
from collections.abc import AsyncIterator

import httpx

from phoson_llm.utils import (
    CONTEXT_LENGTH_ERROR_CODE,
    map_error_code,
    scrub_surrogates,
    load_file_as_base64,
    normalize_stop_reason,
    is_context_length_error,
    missing_attachment_placeholder,
)
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    AudioBlock,
    ErrorEvent,
    ImageBlock,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    VideoBlock,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    DocumentBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ReasoningDoneEvent,
    ReasoningStartEvent,
    ReasoningTokenEvent,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.auth.code_assist import (
    CODE_ASSIST_BASE_URL,
    CodeAssistTokens,
    CodeAssistAuthError,
    save_tokens,
    get_valid_tokens,
)

logger = logging.getLogger(__name__)

# ─── Endpoint constants ──────────────────────────────────────────────────────

#: Path suffixes on top of :data:`CODE_ASSIST_BASE_URL` (Google's
#: ``method:verb`` colon convention).
STREAM_PATH = ":streamGenerateContent"
LOAD_CODE_ASSIST_PATH = ":loadCodeAssist"
ONBOARD_USER_PATH = ":onboardUser"
RETRIEVE_QUOTA_PATH = ":retrieveUserQuota"

#: Client identity reported to the backend. The subscription surface is
#: attributed per client, so a stable plugin type is required.
PLUGIN_TYPE = "GEMINI"
IDE_TYPE = "IDE_UNSPECIFIED"
PLATFORM_TYPE = "PLATFORM_UNSPECIFIED"
DEFAULT_CLIENT_VERSION = "1.0.0"

#: Tier ids used by the Code Assist onboarding flow.
TIER_FREE = "free-tier"
TIER_LEGACY = "legacy-tier"

#: No response for this long (no bytes at all) aborts the turn.
DEFAULT_TIMEOUT = httpx.Timeout(60.0, read=900.0)

#: Seconds between polls of the ``:onboardUser`` long-running operation.
ONBOARD_POLL_INTERVAL = 5.0

#: Phoson reasoning levels → Gemini ``thinkingConfig.thinkingBudget``. ``-1``
#: means "let the model decide", ``0`` disables thinking entirely.
_THINKING_BUDGET: dict[str, int] = {
    "minimal": 0,
    "low": 4096,
    "medium": 12288,
    "high": -1,
    "xhigh": -1,
    "max": -1,
}

#: Google canonical status strings → Phoson error codes, for the error bodies
#: the API returns inside a stream or an HTTP failure.
_GOOGLE_STATUS_CODES: dict[str, str] = {
    "RESOURCE_EXHAUSTED": "rate_limit",
    "UNAUTHENTICATED": "auth",
    "PERMISSION_DENIED": "permission",
    "NOT_FOUND": "not_found",
    "INVALID_ARGUMENT": "invalid_request",
    "FAILED_PRECONDITION": "invalid_request",
    "INTERNAL": "server_error",
    "UNAVAILABLE": "overloaded",
    "DEADLINE_EXCEEDED": "timeout",
}

#: Fallback catalogue, used when the quota endpoint reports nothing (for
#: example an account whose tier has no quota buckets yet). Ids mirror the
#: Gemini CLI's own model list.
DEFAULT_MODELS: tuple[tuple[str, str], ...] = (
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
    ("gemini-2.0-flash", "Gemini 2.0 Flash"),
)

#: Retryable Phoson codes when they arrive inside a 5xx/429 error frame.
_RETRYABLE_CODES = frozenset({"rate_limit", "overloaded", "server_error", "timeout"})


# ─── Request construction ────────────────────────────────────────────────────


def user_agent(model: str, version: str = DEFAULT_CLIENT_VERSION) -> str:
    """Build the ``User-Agent`` the backend expects from a Code Assist client."""
    system = (platform.system() or "unknown").lower()
    machine = platform.machine() or "unknown"
    return f"GeminiCLI/{version}/{model or 'unknown'} ({system}; {machine}; terminal)"


def build_headers(
    tokens: CodeAssistTokens,
    *,
    model: str = "",
    version: str = DEFAULT_CLIENT_VERSION,
) -> dict[str, str]:
    """Auth headers for the Code Assist backend (bearer, no API key)."""
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": user_agent(model, version),
        "X-Goog-Api-Client": f"gl-python/{version}",
    }


def _tool_name_map(messages: list[Message]) -> dict[str, str]:
    """Map ``ToolUseBlock.tool_call_id`` → tool name (for tool *results*).

    Gemini's ``functionResponse`` part is keyed by function *name*, while a
    Phoson ``ToolResultBlock`` only carries the call id — the same gap
    :mod:`phoson_llm.chats.gemini` closes with an identical map.
    """
    names: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg.content, str):
            continue
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                names[block.tool_call_id] = block.tool_name
    return names


def _block_part(block: Any, tool_name_by_id: dict[str, str]) -> dict[str, Any]:
    """Convert one Phoson content block to a Gemini ``Part`` dict."""
    if isinstance(block, TextBlock):
        return {"text": block.text}

    if isinstance(block, ImageBlock):
        mime = block.media_type or "image/jpeg"
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            data = load_file_as_base64(path, mime)
            if data is None:
                return {"text": missing_attachment_placeholder("image", path)}
            return {"inlineData": {"mimeType": mime, "data": data.split(",", 1)[-1]}}
        # Hosted URI (gs:// or https://) — pass through as-is.
        return {"fileData": {"mimeType": mime, "fileUri": source}}

    if isinstance(block, DocumentBlock):
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            data = load_file_as_base64(path, "application/pdf")
            if data is None:
                return {"text": missing_attachment_placeholder("document", path)}
            return {
                "inlineData": {
                    "mimeType": "application/pdf",
                    "data": data.split(",", 1)[-1],
                }
            }
        return {"fileData": {"mimeType": "application/pdf", "fileUri": source}}

    if isinstance(block, AudioBlock):
        return {"text": f"[Audio not supported by Gemini: {block.source}]"}

    if isinstance(block, VideoBlock):
        return {"text": f"[Video not supported by Gemini: {block.source}]"}

    if isinstance(block, ToolUseBlock):
        # An assistant turn that requested a tool becomes a ``functionCall``
        # part; carry the Phoson tool_call_id so the later response matches.
        call: dict[str, Any] = {
            "name": block.tool_name,
            "args": block.args or {},
        }
        if block.tool_call_id:
            call["id"] = block.tool_call_id
        return {"functionCall": call}

    if isinstance(block, ToolResultBlock):
        response: dict[str, Any] = {
            "name": tool_name_by_id.get(block.tool_call_id, ""),
            "response": {"result": block.result},
        }
        if block.tool_call_id:
            response["id"] = block.tool_call_id
        return {"functionResponse": response}

    return {"text": f"[Unsupported block: {type(block).__name__}]"}


def _message_parts(
    msg: Message, tool_name_by_id: dict[str, str]
) -> list[dict[str, Any]]:
    """Content parts for one non-system message."""
    if isinstance(msg.content, str):
        return [{"text": msg.content}] if msg.content else []
    return [_block_part(block, tool_name_by_id) for block in msg.content]


def system_instruction(messages: list[Message], config: ModelConfig) -> str | None:
    """Resolve the system prompt (explicit config wins, then a system message)."""
    if config.system:
        return config.system
    for msg in messages:
        if msg.role == "system":
            return msg.content if isinstance(msg.content, str) else None
    return None


def build_generate_body(
    *,
    messages: list[Message],
    config: ModelConfig,
    project_id: str | None,
    tools: list[ToolDefinition] | None = None,
    prompt_id: str | None = None,
    include_thoughts: bool = True,
) -> dict[str, Any]:
    """Build the JSON body for ``POST {base}:streamGenerateContent``."""
    tool_name_by_id = _tool_name_map(messages)
    contents: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            continue
        parts = _message_parts(msg, tool_name_by_id)
        if not parts:
            continue
        contents.append(
            {"role": "model" if msg.role == "assistant" else "user", "parts": parts}
        )

    request: dict[str, Any] = {"contents": contents}

    system = system_instruction(messages, config)
    if system:
        request["systemInstruction"] = {"parts": [{"text": system}]}

    generation: dict[str, Any] = {}
    if config.temperature is not None:
        generation["temperature"] = config.temperature
    if config.max_tokens:
        generation["maxOutputTokens"] = config.max_tokens
    effort = config.reasoning_effort
    if effort:
        # Streaming the model's thoughts back needs includeThoughts; the budget
        # selects how much thinking is allowed (-1 = model default).
        generation["thinkingConfig"] = {
            "includeThoughts": include_thoughts,
            "thinkingBudget": _THINKING_BUDGET.get(effort, -1),
        }
    if generation:
        request["generationConfig"] = generation

    if tools:
        # Gemini wants OpenAPI-flavoured schemas; Phoson hands over the JSON
        # Schema as-is, exactly like the public-API adapter does.
        request["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                    for tool in tools
                ]
            }
        ]

    body: dict[str, Any] = {
        "model": config.model,
        "request": request,
        "user_prompt_id": prompt_id or str(uuid.uuid4()),
    }
    if project_id:
        body["project"] = project_id
    if config.session_id:
        # Groups the turns of one conversation, mirroring the Gemini CLI's
        # session correlation field.
        body["session_id"] = str(config.session_id)

    # Scrub lone surrogates so serialisation cannot blow up on stray bytes from
    # tool output (see phoson_llm.utils.scrub_surrogates, #270).
    return scrub_surrogates(body)


# ─── SSE parsing ─────────────────────────────────────────────────────────────


async def _iter_sse_payloads(
    lines: AsyncIterator[str],
) -> AsyncIterator[dict[str, Any]]:
    """Yield the JSON object of every SSE ``data:`` frame in ``lines``.

    Comments (``:``), keep-alives and the ``[DONE]`` sentinel are skipped, and a
    malformed frame is logged rather than killing the turn.
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


def unwrap_frame(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the ``GenerateContentResponse`` from an SSE frame.

    The subscription endpoint wraps every response in an envelope
    (``{"response": {…}, "traceId": …, "consumedCredits": […]}``). A frame that
    already looks like a bare response is accepted as well, so a future
    unwrapped stream keeps working.
    """
    inner = payload.get("response")
    if isinstance(inner, dict):
        return inner
    if "candidates" in payload or "usageMetadata" in payload:
        return payload
    return None


def _usage_from_metadata(metadata: Any) -> TokenUsage | None:
    """Map ``usageMetadata`` onto Phoson's :class:`TokenUsage`."""
    if not isinstance(metadata, dict):
        return None
    return TokenUsage(
        input=int(metadata.get("promptTokenCount") or 0),
        output=int(metadata.get("candidatesTokenCount") or 0),
        cache_read=int(metadata.get("cachedContentTokenCount") or 0),
    )


def _frame_error(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The error object of a failure frame, bare or inside the envelope.

    The subscription endpoint wraps responses (``{"response": {…}}``), so an
    error may sit either at the top level or under ``response``.
    """
    error = payload.get("error")
    if isinstance(error, dict):
        return error
    inner = payload.get("response")
    if isinstance(inner, dict):
        nested = inner.get("error")
        if isinstance(nested, dict):
            return nested
    return None


def _sse_error(payload: dict[str, Any]) -> tuple[str, str, bool]:
    """Extract ``(message, code, retryable)`` from a failure frame."""
    error = _frame_error(payload) or {}

    message = str(error.get("message") or "the Code Assist backend reported a failure")
    status = str(error.get("status") or "")
    raw_code = str(error.get("code") or "")

    if is_context_length_error(None, message):
        return message, CONTEXT_LENGTH_ERROR_CODE, False
    if status in _GOOGLE_STATUS_CODES:
        code = _GOOGLE_STATUS_CODES[status]
        return message, code, code in _RETRYABLE_CODES
    if "rate limit" in message.lower() or "quota" in message.lower():
        return message, "rate_limit", True
    if "overload" in message.lower() or "unavailable" in message.lower():
        return message, "overloaded", True
    return message, raw_code or "provider_error", False


def _block_reason(response: dict[str, Any]) -> str | None:
    """Prompt-level safety block reason, if the backend reported one."""
    feedback = response.get("promptFeedback")
    if not isinstance(feedback, dict):
        return None
    reason = feedback.get("blockReason")
    return reason if isinstance(reason, str) and reason else None


# ─── Onboarding (tier + project discovery) ───────────────────────────────────


@dataclass
class CodeAssistSetup:
    """The account's Code Assist onboarding state.

    Args:
        project_id: The Cloud project the service is provisioned on
            (``cloudaicompanionProject``), sent with every generate request.
        tier: The tier id the account ended up on ("free-tier",
            "standard-tier", "legacy-tier", …).
        allowed_tiers: Every tier the account may use, as reported by
            ``:loadCodeAssist`` (useful for diagnostics).
    """

    project_id: str | None = None
    tier: str | None = None
    allowed_tiers: tuple[str, ...] = ()


def _tier_from_response(payload: dict[str, Any]) -> str | None:
    """Pick the tier id for an account, mirroring the Gemini CLI's rules.

    The default tier wins when the response marks one; otherwise the first
    allowed tier is used, falling back to ``legacy-tier`` (what the reference
    client does when ``allowedTiers`` is empty).
    """
    for key in ("currentTier", "paidTier"):
        entry = payload.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            return entry["id"]

    tiers = payload.get("allowedTiers")
    if isinstance(tiers, list):
        ids = [
            t.get("id")
            for t in tiers
            if isinstance(t, dict) and isinstance(t.get("id"), str)
        ]
        for entry in tiers:
            if isinstance(entry, dict) and entry.get("isDefault") is True:
                tier_id = entry.get("id")
                if isinstance(tier_id, str) and tier_id:
                    return tier_id
        if ids:
            return ids[0]
    return TIER_LEGACY


def _allowed_tiers(payload: dict[str, Any]) -> tuple[str, ...]:
    tiers = payload.get("allowedTiers")
    if not isinstance(tiers, list):
        return ()
    return tuple(
        t["id"]
        for t in tiers
        if isinstance(t, dict) and isinstance(t.get("id"), str) and t["id"]
    )


def _needs_user_project(payload: dict[str, Any]) -> bool:
    """Whether ``:loadCodeAssist`` says the account must supply its own project.

    Google marks the tier with ``userDefinedCloudaicompanionProject: true`` when
    it will *not* auto-provision a project (typically an enterprise seat on a
    project the user already owns). In that case no ``:onboardUser`` call can
    conjure one: the project has to come from config/env.
    """
    tiers = payload.get("allowedTiers")
    if not isinstance(tiers, list):
        return False
    return any(
        isinstance(t, dict) and t.get("userDefinedCloudaicompanionProject") is True
        for t in tiers
    )


def _ineligible_reasons(payload: dict[str, Any]) -> tuple[str, ...]:
    """Human-readable reasons a tier was refused (``ineligibleTiers``)."""
    tiers = payload.get("ineligibleTiers")
    if not isinstance(tiers, list):
        return ()
    reasons: list[str] = []
    for entry in tiers:
        if not isinstance(entry, dict):
            continue
        message = entry.get("reasonMessage")
        if isinstance(message, str) and message.strip():
            reasons.append(message.strip())
        elif isinstance(entry.get("reasonCode"), str) and entry["reasonCode"]:
            reasons.append(str(entry["reasonCode"]))
    return tuple(reasons)


def _onboarding_suffix(payload: dict[str, Any]) -> str:
    """Best-effort explanation of why no project could be provisioned.

    Appended to the terminal onboarding error so the fix is visible without
    digging into the raw backend payload: either the tier demands a project the
    user owns (``userDefinedCloudaicompanionProject``) or the tier was refused
    outright (``ineligibleTiers``, e.g. Google retiring the free individual
    tier for this client).
    """
    notes: list[str] = []
    if _needs_user_project(payload):
        notes.append(
            "this account's tier needs a Cloud project you own — set "
            "GOOGLE_CLOUD_PROJECT to a project with the Code Assist API enabled "
            "and a Code Assist licence"
        )
    reasons = _ineligible_reasons(payload)
    if reasons:
        notes.append("ineligible tiers: " + "; ".join(reasons))
    if not notes:
        notes.append(
            "set GOOGLE_CLOUD_PROJECT to a licensed project, or check the "
            "account's eligibility"
        )
    return " — " + "; ".join(notes)


def _project_from_value(value: Any) -> str | None:
    """Read a project id from ``"id"``/``{"id": …}``/``{"name": …}`` shapes."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("id", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


async def load_code_assist(
    tokens: CodeAssistTokens,
    *,
    project_id: str | None = None,
    base_url: str = CODE_ASSIST_BASE_URL,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: httpx.Timeout | float | None = None,
) -> dict[str, Any]:
    """Call ``:loadCodeAssist`` and return the raw response payload.

    Raises:
        CodeAssistAuthError: When the backend rejects the request, or answers
            with a non-object body.
    """
    metadata: dict[str, Any] = {
        "ideType": IDE_TYPE,
        "platform": PLATFORM_TYPE,
        "pluginType": PLUGIN_TYPE,
    }
    body: dict[str, Any] = {"metadata": metadata}
    if project_id:
        metadata["duetProject"] = project_id
        body["cloudaicompanionProject"] = project_id

    response = await _post(
        f"{base_url.rstrip('/')}{LOAD_CODE_ASSIST_PATH}",
        json_body=body,
        headers=build_headers(tokens),
        client=client,
        transport=transport,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise CodeAssistAuthError(
            f"Code Assist onboarding lookup failed (HTTP {response.status_code}): "
            f"{_error_detail(response)}",
            terminal=response.status_code in (401, 403),
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise CodeAssistAuthError("Code Assist returned an unexpected payload")
    return payload


async def _poll_operation(
    tokens: CodeAssistTokens,
    name: str,
    *,
    base_url: str,
    interval: float = ONBOARD_POLL_INTERVAL,
    timeout: float | None = None,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Poll a long-running operation until ``done`` and return its result.

    Uses the operation's own ``name`` as the URL path, which is what the
    reference client does for ``:onboardUser`` operations.
    """
    import time
    import asyncio

    url = f"{base_url.rstrip('/')}/{name.lstrip('/')}"
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        response = await _post(
            url,
            json_body={},
            headers=build_headers(tokens),
            client=client,
            transport=transport,
            timeout=None,
        )
        if response.status_code != 200:
            raise CodeAssistAuthError(
                f"Code Assist onboarding failed (HTTP {response.status_code}): "
                f"{_error_detail(response)}",
                terminal=response.status_code in (401, 403),
            )
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("done") is True:
            return payload if isinstance(payload, dict) else {}
        if deadline is not None and time.monotonic() >= deadline:
            raise CodeAssistAuthError(
                "timed out waiting for the Code Assist onboarding operation"
            )
        await asyncio.sleep(interval)


async def onboard_user(
    tokens: CodeAssistTokens,
    *,
    tier: str,
    project_id: str | None = None,
    base_url: str = CODE_ASSIST_BASE_URL,
    timeout: float | None = None,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Provision the account through ``:onboardUser`` and await the operation.

    The free tier must **not** carry a project: the backend answers with a
    precondition failure when one is supplied (the Cloud project is created as
    part of provisioning).
    """
    body: dict[str, Any] = {"tierId": tier}
    if tier != TIER_FREE and project_id:
        body["cloudaicompanionProject"] = project_id
        body["metadata"] = {"duetProject": project_id}

    response = await _post(
        f"{base_url.rstrip('/')}{ONBOARD_USER_PATH}",
        json_body=body,
        headers=build_headers(tokens),
        client=client,
        transport=transport,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise CodeAssistAuthError(
            f"Code Assist onboarding failed (HTTP {response.status_code}): "
            f"{_error_detail(response)}",
            terminal=response.status_code in (401, 403),
        )
    payload = response.json()
    payload = payload if isinstance(payload, dict) else {}

    if payload.get("done") is not True:
        name = payload.get("name")
        if isinstance(name, str) and name:
            payload = await _poll_operation(
                tokens,
                name,
                base_url=base_url,
                timeout=timeout,
                client=client,
                transport=transport,
            )
    return payload


def _deadline_seconds(timeout: httpx.Timeout | float | None) -> float | None:
    """Reduce a client timeout to the seconds budget a poll deadline needs.

    ``_poll_operation`` compares its budget against :func:`time.monotonic`, so it
    cannot take the composite :class:`httpx.Timeout`. The *read* component is the
    one that bounds how long a single response may take, which is what the poll
    wait actually measures.
    """
    if isinstance(timeout, httpx.Timeout):
        return timeout.read
    return timeout


async def ensure_project(
    tokens: CodeAssistTokens,
    *,
    project_id: str | None = None,
    path: str | Path | None = None,
    base_url: str = CODE_ASSIST_BASE_URL,
    persist: bool = True,
    timeout: httpx.Timeout | float | None = None,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CodeAssistSetup:
    """Resolve (and, when needed, provision) the Code Assist project.

    Order of resolution: the project recorded on the credentials, an explicit
    ``project_id``, whatever ``:loadCodeAssist`` reports, ``GOOGLE_CLOUD_PROJECT``,
    and finally a fresh ``:onboardUser`` provisioning for the account's tier.

    The tier and project are written back to the credential store so later turns
    (and ``/auth status``) can see them without another round-trip.
    """
    import os

    setup = CodeAssistSetup()
    payload: dict[str, Any] = {}
    try:
        payload = await load_code_assist(
            tokens,
            project_id=project_id or tokens.project_id,
            base_url=base_url,
            client=client,
            transport=transport,
            timeout=timeout,
        )
    except CodeAssistAuthError as exc:
        if exc.terminal:
            raise
        logger.warning("Code Assist onboarding lookup failed: %s", exc)

    setup.tier = _tier_from_response(payload) if payload else tokens.tier
    setup.allowed_tiers = _allowed_tiers(payload)
    resolved = _project_from_value(payload.get("cloudaicompanionProject"))
    resolved = resolved or project_id or tokens.project_id
    resolved = resolved or os.environ.get("GOOGLE_CLOUD_PROJECT")

    # A project the account may not use is a dead end: surface the backend's own
    # IAM message now instead of failing later on every generate request.
    validation = payload.get("projectValidationError") if payload else None
    if resolved and isinstance(validation, dict):
        detail = str(validation.get("message") or "").strip() or "project rejected"
        raise CodeAssistAuthError(
            f"Code Assist refused project {resolved!r}: {detail}",
            terminal=True,
        )

    if not resolved:
        tier = setup.tier or TIER_FREE
        onboarded = await onboard_user(
            tokens,
            tier=tier,
            project_id=project_id,
            base_url=base_url,
            timeout=_deadline_seconds(timeout),
            client=client,
            transport=transport,
        )
        inner = onboarded.get("response")
        inner = inner if isinstance(inner, dict) else {}
        resolved = _project_from_value(inner.get("cloudaicompanionProject"))
        if not resolved:
            raise CodeAssistAuthError(
                "Code Assist could not provision a project for this account"
                + _onboarding_suffix(payload)
            )
        setup.tier = tier

    setup.project_id = resolved
    if persist and (resolved != tokens.project_id or setup.tier != tokens.tier):
        tokens.project_id = resolved
        tokens.tier = setup.tier or tokens.tier
        try:
            save_tokens(tokens, path)
        except OSError as exc:  # pragma: no cover - read-only home directory
            logger.warning("could not persist the Code Assist project: %s", exc)
    return setup


# ─── Model catalogue ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CodeAssistModel:
    """One entry of the Code Assist model catalogue."""

    slug: str
    display_name: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.slug


async def list_models(
    *,
    base_url: str = CODE_ASSIST_BASE_URL,
    auth_file: str | Path | None = None,
    tokens: CodeAssistTokens | None = None,
    project_id: str | None = None,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: httpx.Timeout | float | None = None,
) -> list[CodeAssistModel]:
    """List the models the signed-in subscription can call.

    Reads ``:retrieveUserQuota``, whose ``buckets`` carry one entry per model
    the account has quota for. When the endpoint reports nothing (or is
    unavailable), the static :data:`DEFAULT_MODELS` catalogue is returned so the
    picker still offers the standard ids.

    Raises:
        CodeAssistAuthError: When no usable login is stored.
    """
    if tokens is None:
        tokens = await get_valid_tokens(
            path=auth_file, client=client, transport=transport
        )

    setup = await ensure_project(
        tokens,
        project_id=project_id,
        path=auth_file,
        base_url=base_url,
        client=client,
        transport=transport,
        timeout=timeout,
    )

    labels = dict(DEFAULT_MODELS)
    if setup.project_id:
        response = await _post(
            f"{base_url.rstrip('/')}{RETRIEVE_QUOTA_PATH}",
            json_body={"project": setup.project_id},
            headers=build_headers(tokens),
            client=client,
            transport=transport,
            timeout=timeout,
        )
        if response.status_code != 200:
            raise CodeAssistAuthError(
                f"model listing failed (HTTP {response.status_code}): "
                f"{_error_detail(response)}",
                terminal=response.status_code in (401, 403),
            )
        payload = response.json()
        buckets = payload.get("buckets") if isinstance(payload, dict) else None
        seen: list[str] = []
        if isinstance(buckets, list):
            for bucket in buckets:
                if not isinstance(bucket, dict):
                    continue
                entry = bucket.get("modelId")
                if isinstance(entry, str) and entry.strip() and entry not in seen:
                    seen.append(entry)
        if seen:
            return [
                CodeAssistModel(slug=s, display_name=labels.get(s, "")) for s in seen
            ]

    return [CodeAssistModel(slug=s, display_name=name) for s, name in DEFAULT_MODELS]


def list_models_sync(**kwargs: Any) -> list[CodeAssistModel]:
    """Synchronous :func:`list_models` for click-style code paths."""
    import asyncio

    return asyncio.run(list_models(**kwargs))


# ─── HTTP plumbing for the API surface ───────────────────────────────────────


async def _post(
    url: str,
    *,
    json_body: dict[str, Any],
    headers: dict[str, str],
    client: httpx.AsyncClient | None,
    transport: httpx.AsyncBaseTransport | None,
    timeout: httpx.Timeout | float | None,
) -> httpx.Response:
    """POST with an owned client when the caller did not supply one."""
    effective = DEFAULT_TIMEOUT if timeout is None else timeout
    if client is not None:
        return await client.post(url, json=json_body, headers=headers)
    async with httpx.AsyncClient(transport=transport, timeout=effective) as owned:
        return await owned.post(url, json=json_body, headers=headers)


async def _read_error_body(response: httpx.Response) -> str:
    """Short, secret-free error text for a failed (non-streamed) response."""
    try:
        body = await response.aread()
    except httpx.HTTPError:  # pragma: no cover - connection died mid-read
        return f"HTTP {response.status_code}"
    text = body.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:200] or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:200]
        for key in ("message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:200]
    return text[:200]


def _error_detail(response: httpx.Response) -> str:
    """Extract a short error string from a failed response (sync helper)."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:200]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:200]
        for key in ("message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:200]
    return str(payload)[:200]


# ─── Adapter ─────────────────────────────────────────────────────────────────


class CodeAssistChat(BaseLLMChat):
    """Google Code Assist subscription adapter (no API key).

    Args:
        api_key: An explicit OAuth access token. Leave unset to use the stored
            login (``~/.phoson/auth-google.json``), which is loaded lazily and
            refreshed automatically when it is about to expire.
        base_url: Backend override. Defaults to
            :data:`phoson_llm.auth.code_assist.CODE_ASSIST_BASE_URL`.
        project_id: Cloud project override; otherwise discovered (and cached)
            through the onboarding handshake, or read from the credentials.
        auth_file: Credential file override (defaults to the Phoson store).
        client_version: Version string reported in the ``User-Agent``.
        timeout: Per-request timeout (connect/read).
        transport: ``httpx`` transport override (tests).

    The adapter needs no model-specific configuration: pass any id returned by
    :func:`list_models` in ``ModelConfig.model``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        project_id: str | None = None,
        auth_file: str | Path | None = None,
        client_version: str = DEFAULT_CLIENT_VERSION,
        timeout: httpx.Timeout | float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key or None
        self._project_id = project_id
        self._auth_file = auth_file
        self._client_version = client_version
        self._transport = transport
        self.base_url = (base_url or CODE_ASSIST_BASE_URL).rstrip("/")
        self._timeout: httpx.Timeout | float = (
            DEFAULT_TIMEOUT if timeout is None else timeout
        )
        # Onboarding is a round-trip per process at most: the project id cannot
        # change mid-session unless a request tells us otherwise.
        self._setup: CodeAssistSetup | None = None

    @property
    def stream_url(self) -> str:
        return f"{self.base_url}{STREAM_PATH}"

    def __repr__(self) -> str:
        return "CodeAssistChat()"

    # ── Credentials ──────────────────────────────────────────────────────────

    async def _credentials(self, *, force_refresh: bool = False) -> CodeAssistTokens:
        """Resolve the credential set for one request."""
        if self._api_key:
            return CodeAssistTokens(
                access_token=self._api_key,
                project_id=self._project_id,
            )
        tokens = await get_valid_tokens(
            path=self._auth_file,
            force_refresh=force_refresh,
            transport=self._transport,
        )
        if self._project_id:
            tokens.project_id = self._project_id
        return tokens

    def _headers(self, tokens: CodeAssistTokens, model: str) -> dict[str, str]:
        return build_headers(tokens, model=model, version=self._client_version)

    async def _resolve_setup(self, tokens: CodeAssistTokens) -> CodeAssistSetup:
        """Resolve (once) the project/tier this account generates on."""
        if self._setup is not None and self._setup.project_id:
            return self._setup
        setup = await ensure_project(
            tokens,
            project_id=self._project_id,
            path=self._auth_file,
            base_url=self.base_url,
            transport=self._transport,
        )
        self._setup = setup
        return setup

    # ── Streaming ────────────────────────────────────────────────────────────

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream one turn from the Code Assist backend.

        Emits the standard event sequence documented by
        :class:`phoson_llm.chats.base.BaseLLMChat`.
        """
        yield LLMStartEvent(model=config.model, message_count=len(messages))

        try:
            tokens = await self._credentials()
            setup = await self._resolve_setup(tokens)
        except CodeAssistAuthError as exc:
            yield ErrorEvent(message=str(exc), code="auth", retryable=not exc.terminal)
            return

        body = build_generate_body(
            messages=messages,
            config=config,
            tools=tools,
            project_id=setup.project_id,
        )
        attempts = 2 if self._api_key is None else 1
        refreshed = False

        for attempt in range(attempts):
            try:
                tokens = await self._credentials(
                    force_refresh=attempt > 0 and not refreshed
                )
            except CodeAssistAuthError as exc:
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
                        self.stream_url,
                        params={"alt": "sse"},
                        json=body,
                        headers=self._headers(tokens, config.model),
                    ) as response:
                        if response.status_code != 200:
                            detail = await _read_error_body(response)
                            if (
                                response.status_code in (401, 403)
                                and self._api_key is None
                            ):
                                retry_auth = True
                                refreshed = True
                            else:
                                if response.status_code in (403, 404):
                                    # A stale cached project id is the likeliest
                                    # cause (it was deleted, or the account
                                    # moved); re-resolve on the next turn.
                                    self._setup = None
                                code = map_error_code(response.status_code)
                                if code == "unknown":
                                    code = (
                                        "invalid_request"
                                        if 400 <= response.status_code < 500
                                        else "server_error"
                                    )
                                yield ErrorEvent(
                                    message=detail,
                                    code=code,
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
                    message=f"Code Assist request failed: {exc}",
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
        with a single ``ErrorEvent`` when the backend reports a failure. Text
        produced before the failure is still emitted as tokens.
        """
        text_acc = ""
        reasoning_acc = ""
        reasoning_open = False
        has_tool_calls = False
        tool_call_index = 0
        stop_reason: object = None
        usage: TokenUsage | None = None
        failure: tuple[str, str, bool] | None = None

        async for frame in _iter_sse_payloads(lines):
            if _frame_error(frame) is not None:
                failure = _sse_error(frame)
                break

            response = unwrap_frame(frame)
            if response is None:
                continue

            blocked = _block_reason(response)
            if blocked:
                failure = (
                    f"the request was blocked by the provider ({blocked})",
                    "content_filter",
                    False,
                )
                break

            metadata = _usage_from_metadata(response.get("usageMetadata"))
            if metadata is not None:
                usage = metadata

            candidates = response.get("candidates")
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                finish = candidate.get("finishReason")
                if isinstance(finish, str) and finish:
                    stop_reason = finish
                content = candidate.get("content")
                parts = content.get("parts") if isinstance(content, dict) else None
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        if part.get("thought") is True:
                            if not reasoning_open:
                                reasoning_open = True
                                yield ReasoningStartEvent()
                            reasoning_acc += text
                            yield ReasoningTokenEvent(content=text)
                        else:
                            text_acc += text
                            yield TokenEvent(content=text)
                    call = part.get("functionCall")
                    if isinstance(call, dict):
                        # Gemini delivers each call whole (no argument deltas),
                        # so a single tool event per part is enough — the index
                        # only has to stay unique within the turn.
                        has_tool_calls = True
                        yield ToolCallEvent(
                            index=tool_call_index,
                            tool_call_id=call.get("id") or f"call_{tool_call_index}",
                            tool_name=call.get("name") or "",
                            args=call.get("args") or {},
                        )
                        tool_call_index += 1

        if reasoning_open or reasoning_acc:
            yield ReasoningDoneEvent(content=reasoning_acc)

        if failure is not None:
            message, code, retryable = failure
            yield ErrorEvent(message=message, code=code, retryable=retryable)
            return

        if usage is not None:
            # Subscription plans are not billed per token, so no meaningful USD
            # figure exists — token counts are still reported for accounting.
            yield UsageEvent(
                model=config.model, usage=usage, cost_usd=0.0, cost_known=False
            )

        normalized = normalize_stop_reason(stop_reason, provider="google")
        # Gemini reports a plain ``STOP`` even when the candidate is a tool
        # call, so surface the more precise reason — but never at the cost of a
        # specific one (``MAX_TOKENS`` must stay distinguishable, F-13).
        if has_tool_calls and normalized in (None, "end_turn"):
            normalized = "tool_use"
        if normalized is None:
            normalized = "end_turn"

        yield LLMDoneEvent(
            content=text_acc,
            has_tool_calls=has_tool_calls,
            stop_reason=normalized,
        )
