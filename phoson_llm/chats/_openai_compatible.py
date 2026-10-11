"""Shared helpers for OpenAI-compatible chat adapters.

Both :class:`phoson_llm.chats.openai.OpenAIChat` and
:class:`phoson_llm.chats.openrouter.OpenRouterChat` speak the same
``/v1/chat/completions`` protocol and used to carry their own ~120 LOC
copy of the streaming loop. The loop now lives here as
:func:`stream_chat_completions` and the adapters are thin wrappers that
configure the client and provide a cost callback.
"""

import json
import math
import warnings
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, NotRequired
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from openai import AsyncOpenAI, APIStatusError, APIConnectionError

from phoson_llm.utils import (
    CONTEXT_LENGTH_ERROR_CODE,
    map_error_code,
    scrub_surrogates,
    normalize_stop_reason,
    is_context_length_error,
)
from phoson_llm.schemas import (
    LLMEvent,
    ErrorEvent,
    JsonObject,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ReasoningDoneEvent,
    ToolCallDeltaEvent,
    ReasoningStartEvent,
    ReasoningTokenEvent,
    cap_reasoning,
)

if TYPE_CHECKING:
    from phoson_llm.schemas import Message, ContentBlock, ToolDefinition


# ─── Cost callback contract ──────────────────────────────────────────────────


class CostCalculator(Protocol):
    """Compute the USD cost and confidence flag for a finished call.

    Implementations may return ``(0.0, False)`` to signal that the cost is
    not knowable (e.g. OpenRouter, where the provider's price varies).

    ``input_tokens`` is the **net** (uncached) input: the cached portion is
    reported separately in ``cache_read_tokens`` / ``cache_write_tokens`` and
    must not be included in ``input_tokens`` — otherwise it would be priced
    twice (full input rate *plus* the cache rate). OpenAI-compatible usage
    reports this net value as ``prompt_tokens`` minus the cache details.
    """

    def __call__(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
    ) -> tuple[float, bool]: ...


def _no_cost(
    *,
    model: str,  # noqa: ARG001
    input_tokens: int,  # noqa: ARG001
    output_tokens: int,  # noqa: ARG001
    cache_read_tokens: int,  # noqa: ARG001
    cache_write_tokens: int,  # noqa: ARG001
) -> tuple[float, bool]:
    """Cost callback that always reports unknown cost. Default for aggregators."""
    return (0.0, False)


# ─── Message dict types ──────────────────────────────────────────────────────


class _MessageDict(TypedDict):
    """Top-level shape of a single OpenAI-compatible message dict.

    Return element type of :func:`_convert_messages`. All optional keys use
    ``NotRequired`` to accurately reflect which fields each variant carries.
    """

    role: str
    content: NotRequired[str | list[dict]]
    tool_calls: NotRequired[list[dict]]
    tool_call_id: NotRequired[str]
    reasoning_content: NotRequired[str]


#: Explicit prompt-cache marker (Alibaba Model Studio et al.). Placed on a
#: text *content part* (array form), it asks the provider to cache everything
#: from the start of the ``messages`` array up to that part. Only at most
#: four markers are allowed per request; the cached block must be ≥1024 tokens
#: and the entry lives 5 minutes, renewed on every hit.
_EPHEMERAL_CACHE_CONTROL: JsonObject = {"type": "ephemeral"}


# ─── Message / tool conversion ──────────────────────────────────────────────


def _convert_content_block(block: "ContentBlock") -> JsonObject:
    """Converts a Phoson ContentBlock to OpenAI-compatible API format."""
    from phoson_llm.utils import load_file_as_base64, missing_attachment_placeholder
    from phoson_llm.schemas import (
        TextBlock,
        AudioBlock,
        ImageBlock,
        VideoBlock,
        ToolUseBlock,
        ToolResultBlock,
    )

    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}

    if isinstance(block, ImageBlock):
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            source = load_file_as_base64(path, block.media_type)
            if source is None:
                return {
                    "type": "text",
                    "text": missing_attachment_placeholder("image", path),
                }
        return {
            "type": "image_url",
            "image_url": {
                "url": source,
                "detail": block.detail,
            },
        }

    if isinstance(block, AudioBlock):
        source = block.source
        if source.startswith("file://"):
            path = source[7:]
            data = load_file_as_base64(path)
            if data is None:
                return {
                    "type": "text",
                    "text": missing_attachment_placeholder("audio", path),
                }
            b64 = data.split(",", 1)[-1]
        else:
            b64 = source
        return {
            "type": "input_audio",
            "input_audio": {
                "data": b64,
                "format": block.format,
            },
        }

    if isinstance(block, VideoBlock):
        return {
            "type": "text",
            "text": f"[Video not directly supported by this provider: {block.source}]",
        }

    if isinstance(block, (ToolUseBlock, ToolResultBlock)):
        raise TypeError(
            f"ToolUseBlock/ToolResultBlock should not reach _convert_content_block. "
            f"Got: {type(block)}"
        )

    return {
        "type": "text",
        "text": f"[Unsupported content block: {type(block).__name__}]",
    }


def _reasoning_field(msg: "Message", preserve_thinking: bool | None) -> str | None:
    """The ``reasoning_content`` value to attach to a historical assistant
    message, or ``None`` when it should be omitted (#134).

    Emitted when the message carries reasoning and the caller has not
    explicitly disabled preservation (``preserve_thinking is not False``).
    ``None`` (the default) lets the adapter decide — OpenAI-compatible
    reasoning models (Qwen3, vLLM, LM Studio) need the field sent back on
    assistant turns to stay coherent across turns. The value is capped to
    :data:`REASONING_MAX_CHARS` so a very long chain of thought cannot
    inflate the context window.
    """
    if msg.role != "assistant" or not msg.reasoning:
        return None
    if preserve_thinking is False:
        return None
    return cap_reasoning(msg.reasoning)


def _convert_messages(
    messages: list["Message"], preserve_thinking: bool | None = None
) -> list[_MessageDict]:
    """Converts Phoson messages to OpenAI-compatible format.

    Args:
        messages: Phoson conversation messages.
        preserve_thinking: Whether to re-send captured assistant reasoning as
            ``reasoning_content`` (#134). ``None`` (default) = emit for
            assistant turns that carry reasoning; ``False`` = never emit.
            See :func:`_reasoning_field` for the exact rule.
    """
    from phoson_llm.schemas import TextBlock, ToolUseBlock, ToolResultBlock

    result: list[_MessageDict] = []

    for msg in messages:
        if msg.role == "system":
            content = msg.content if isinstance(msg.content, str) else ""
            result.append({"role": "system", "content": content})
            continue

        if isinstance(msg.content, str):
            d: _MessageDict = {"role": msg.role, "content": msg.content}
            if rc := _reasoning_field(msg, preserve_thinking):
                d["reasoning_content"] = rc
            result.append(d)
            continue

        text_blocks = [b for b in msg.content if isinstance(b, TextBlock)]
        tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
        tool_results = [b for b in msg.content if isinstance(b, ToolResultBlock)]
        multimodal_blocks = [
            b
            for b in msg.content
            if not isinstance(b, (TextBlock, ToolUseBlock, ToolResultBlock))
        ]

        if tool_uses:
            d = {
                "role": "assistant",
                "content": text_blocks[0].text if text_blocks else "",
                "tool_calls": [
                    {
                        "id": b.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": b.tool_name,
                            "arguments": json.dumps(b.args),
                        },
                    }
                    for b in tool_uses
                ],
            }
            if rc := _reasoning_field(msg, preserve_thinking):
                d["reasoning_content"] = rc
            result.append(d)

        for b in tool_results:
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": b.tool_call_id,
                    "content": b.result,
                }
            )

        if multimodal_blocks:
            parts = [_convert_content_block(b) for b in multimodal_blocks]
            if text_blocks:
                parts.insert(
                    0, {"type": "text", "text": " ".join(b.text for b in text_blocks)}
                )
            d = {"role": msg.role, "content": parts}
            if rc := _reasoning_field(msg, preserve_thinking):
                d["reasoning_content"] = rc
            result.append(d)

        elif text_blocks and not tool_uses and not tool_results:
            d = {"role": msg.role, "content": " ".join(b.text for b in text_blocks)}
            if rc := _reasoning_field(msg, preserve_thinking):
                d["reasoning_content"] = rc
            result.append(d)

    return result


def _convert_tools(tools: list["ToolDefinition"]) -> list[JsonObject]:
    """Converts ToolDefinition to OpenAI-compatible tools format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _extract_reasoning_delta(delta: object) -> str | None:
    """Extracts reasoning_content from an OpenAI-style delta.

    Different providers expose chain-of-thought under different attribute
    names: OpenAI's o-series uses ``reasoning_content``, OpenRouter (and
    some upstream providers) use ``reasoning``. We probe both.
    """
    for attr in ("reasoning_content", "reasoning"):
        value = getattr(delta, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_tool_args(raw: str) -> JsonObject:
    """Safe parsing of tool arguments emitted by OpenAI-compatible APIs."""
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        warnings.warn(
            f"Could not parse tool args JSON from OpenAI-compatible stream; "
            f"falling back to raw string. Raw value: {raw!r:.120}",
            UserWarning,
            stacklevel=2,
        )
        return {"command": raw} if raw.strip() else {}

    if isinstance(parsed, dict):
        return parsed

    if isinstance(parsed, str):
        return {"command": parsed}

    warnings.warn(
        f"Unexpected tool args type {type(parsed).__name__!r}"
        f" from OpenAI-compatible stream; stored as _raw.",
        UserWarning,
        stacklevel=2,
    )
    return {"_raw": raw}


# ─── Request construction ───────────────────────────────────────────────────


@dataclass
class ToolCallAccumulator:
    """Accumulates streamed tool-call deltas and finalizes complete calls.

    Single source of truth for delta bookkeeping (id / name / args
    fragments) shared by every OpenAI-compatible adapter, so the
    accumulation logic has exactly one implementation and one test surface.
    """

    tool_args_acc: dict[int, str] = field(default_factory=dict)
    tool_names: dict[int, str] = field(default_factory=dict)
    tool_ids: dict[int, str] = field(default_factory=dict)

    def feed_delta(self, tc: Any) -> list[ToolCallDeltaEvent]:
        """Ingest one ``delta.tool_calls`` entry (SDK object).

        Returns the :class:`ToolCallDeltaEvent` instances to emit for this
        fragment (empty when the fragment carries no args chunk).
        """
        events: list[ToolCallDeltaEvent] = []
        idx = tc.index
        self.tool_args_acc.setdefault(idx, "")
        if tc.id:
            self.tool_ids[idx] = tc.id
        if tc.function and tc.function.name:
            self.tool_names[idx] = tc.function.name

        if tc.function and tc.function.arguments:
            chunk_str = tc.function.arguments
            self.tool_args_acc[idx] += chunk_str
            events.append(
                ToolCallDeltaEvent(
                    index=idx,
                    tool_name=self.tool_names.get(idx, ""),
                    args_chunk=chunk_str,
                )
            )
        return events

    def finalize(self, truncated: bool = False) -> list[ToolCallEvent]:
        """Build :class:`ToolCallEvent` for every accumulated call and reset.

        Normally, calls missing an id or a name are skipped (malformed
        stream). When *truncated* is True (the response hit ``max_tokens``
        mid-tool-call, F-13) we instead emit one event per accumulated
        fragment — synthesizing a missing id/name and tagging the args with
        ``_truncated`` — so the agent loop can answer the partial ``tool_use``
        with an actionable error result instead of invoking the handler on
        incomplete JSON. The buffers are cleared so a subsequent
        ``tool_calls`` finish in the same response starts fresh.
        """
        events: list[ToolCallEvent] = []
        if truncated:
            for idx, raw in self.tool_args_acc.items():
                events.append(
                    ToolCallEvent(
                        index=idx,
                        tool_call_id=self.tool_ids.get(idx, f"truncated_{idx}"),
                        tool_name=self.tool_names.get(idx, "unknown"),
                        args={"_raw": raw, "_truncated": True},
                    )
                )
        else:
            for idx, raw in self.tool_args_acc.items():
                if idx not in self.tool_ids or idx not in self.tool_names:
                    continue
                events.append(
                    ToolCallEvent(
                        index=idx,
                        tool_call_id=self.tool_ids[idx],
                        tool_name=self.tool_names[idx],
                        args=_parse_tool_args(raw),
                    )
                )
        self.tool_args_acc.clear()
        self.tool_ids.clear()
        self.tool_names.clear()
        return events


def _cache_marked_text_content(text: str) -> list[dict]:
    """Wrap a string as a single text part carrying the cache marker.

    Markers are only understood on array-form content, so a message whose
    content is a plain string has to be expanded to its block-list form.
    """
    return [{"type": "text", "text": text, "cache_control": _EPHEMERAL_CACHE_CONTROL}]


def _apply_explicit_cache(messages: list[_MessageDict]) -> None:
    """Tag the stable prefix and the newest message with cache markers.

    Alibaba Model Studio's *explicit* cache is enabled per request by placing
    ``"cache_control": {"type": "ephemeral"}`` on a message content part: the
    provider then caches everything from the start of the array up to that
    part. Two markers cover an agentic conversation:

    - the **system** message — the prefix. Tool definitions travel with the
      system prompt for caching purposes, so this single marker covers both
      and no separate marker is needed on the ``tools`` array, and
    - the **last** message, which advances each turn — the next request
      re-reads the whole prior history from cache while creating a cache that
      extends through the current turn (the multi-turn best practice).

    A marker only takes effect when the cached block reaches 1024 tokens, so
    short prompts simply stay uncached. At most four markers are allowed per
    request; this uses two. The function mutates ``messages`` in place.
    """
    if not messages:
        return

    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content:
                msg["content"] = _cache_marked_text_content(content)
            break

    last = messages[-1]
    if last.get("role") == "system":
        return  # The system message is the only content — already anchored.

    content = last.get("content")
    if isinstance(content, str):
        # An assistant turn that only emitted ``tool_calls`` has "" content:
        # nothing to anchor on, so it is left untouched.
        if content:
            last["content"] = _cache_marked_text_content(content)
    elif isinstance(content, list):
        for part in reversed(content):
            if part.get("type") == "text":
                part["cache_control"] = _EPHEMERAL_CACHE_CONTROL
                break


def _coalesce_tool_messages(messages: list[_MessageDict]) -> None:
    """Fuse each run of consecutive ``tool`` messages into a single message.

    Model Studio's backward cache lookup only examines the 20 content blocks
    that precede a ``cache_control`` marker; anything further back is a cache
    miss. A turn where the model issues parallel tool calls otherwise emits one
    ``tool`` message per result, so a burst of calls can push the previous
    turn's cache beyond that window. Model Studio's OpenAI-compatible endpoint
    documents merging consecutive tool results into one ``tool`` message whose
    ``content`` is a block list — the first block inherits the message's
    ``tool_call_id``, later blocks carry their own — which keeps the block count
    down and the cached prefix within reach.

    This is an Alibaba extension (plain OpenAI rejects a ``tool_call_id`` inside
    a content block and one message per call), so it is applied only on the
    explicit-cache path. Runs of length one are left untouched, and every
    ``tool_call_id`` is preserved. The function mutates ``messages`` in place.
    """
    merged: list[_MessageDict] = []
    i, n = 0, len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") != "tool":
            merged.append(msg)
            i += 1
            continue

        run = [msg]
        j = i + 1
        while j < n and messages[j].get("role") == "tool":
            run.append(messages[j])
            j += 1

        if len(run) > 1:
            head: _MessageDict = {"role": "tool"}
            if "tool_call_id" in run[0]:
                head["tool_call_id"] = run[0]["tool_call_id"]
            head["content"] = [
                _tool_result_part(run[0], include_id=False),
                *(_tool_result_part(m, include_id=True) for m in run[1:]),
            ]
            merged.append(head)
        else:
            merged.append(msg)
        i = j

    messages[:] = merged


def _tool_result_part(msg: _MessageDict, *, include_id: bool) -> dict:
    """Render a tool message's result as a text block for a merged message."""
    content = msg.get("content")
    if isinstance(content, list):
        text = next((p.get("text", "") for p in content if p.get("type") == "text"), "")
    else:
        text = content or ""
    tool_call_id = msg.get("tool_call_id")
    part: dict = {"type": "text", "text": text}
    if include_id and tool_call_id:
        part["tool_call_id"] = tool_call_id
    return part


def _build_request_kwargs(
    *,
    config: ModelConfig,
    messages: list["Message"],
    tools: list["ToolDefinition"] | None,
    max_tokens_key: str,
    extra_kwargs: dict[str, Any] | None = None,
    cache: bool = False,
) -> dict[str, Any]:
    """Build the kwargs dict for ``chat.completions.create``.

    Encapsulates the differences between providers (``max_tokens`` vs.
    ``max_completion_tokens``) and the system-message normalization.

    Args:
        cache: When True, apply explicit prompt-cache markers to the stable
            prefix (system + tool definitions) and the last message. Only
            enabled for providers that document the ``cache_control``
            content-part field (see :func:`_apply_explicit_cache`).
    """
    kwargs: dict[str, Any] = {
        "model": config.model,
        max_tokens_key: config.max_tokens,
        "messages": _convert_messages(messages, config.preserve_thinking),
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    if config.system:
        kwargs["messages"] = [
            m for m in kwargs["messages"] if m.get("role") != "system"
        ]
        kwargs["messages"].insert(
            0,
            {"role": "system", "content": config.system},
        )

    # Applied after the system-message normalization so the marker lands on
    # whichever system message ends up at the front (config.system or the
    # conversation's own). Consecutive tool results are fused first so the
    # last-message marker stays within Model Studio's 20-block lookback window.
    if cache:
        _coalesce_tool_messages(kwargs["messages"])
        _apply_explicit_cache(kwargs["messages"])

    if config.temperature is not None:
        kwargs["temperature"] = config.temperature

    if tools:
        kwargs["tools"] = _convert_tools(tools)

    if config.reasoning_effort:
        kwargs["reasoning_effort"] = config.reasoning_effort
        # Reasoning models reject `temperature`; keep them mutually exclusive.
        kwargs.pop("temperature", None)

    if extra_kwargs:
        kwargs.update(extra_kwargs)

    # Scrub any lone surrogate (e.g. from tool/subprocess output decoded with
    # ``surrogateescape``) before the SDK serializes the body with
    # ``ensure_ascii=False`` + ``.encode("utf-8")``. A single stray surrogate
    # otherwise aborts the whole turn with ``UnicodeEncodeError`` (#270).
    return scrub_surrogates(kwargs)


# ─── Streaming loop ─────────────────────────────────────────────────────────


async def stream_chat_completions(
    client: AsyncOpenAI,
    *,
    messages: list["Message"],
    config: ModelConfig,
    tools: list["ToolDefinition"] | None = None,
    max_tokens_key: str = "max_tokens",
    cost_calculator: CostCalculator = _no_cost,
    extra_kwargs: dict[str, Any] | None = None,
    cache: bool = False,
) -> AsyncIterator[LLMEvent]:
    """Stream a response from an OpenAI-compatible Chat Completions endpoint.

    This is the single source of truth shared by ``OpenAIChat`` and
    ``OpenRouterChat``. The function takes care of:

    - request construction (system normalization, reasoning_effort, tools),
    - chunk dispatch into Phoson's typed event hierarchy,
    - tool-call accumulation across deltas with idempotent emission,
    - reasoning channel detection across vendor variants,
    - usage and cost reporting via the supplied ``cost_calculator``,
    - error mapping to ``ErrorEvent`` with sensible retryability flags.

    Args:
        client: An already-configured ``AsyncOpenAI`` client.
        messages: Phoson conversation messages.
        config: Model configuration.
        tools: Optional tool definitions.
        max_tokens_key: ``"max_tokens"`` for OpenRouter et al.,
            ``"max_completion_tokens"`` for current OpenAI models.
        cost_calculator: Callable that turns token counts into a USD cost
            and a confidence flag. Defaults to "unknown cost".
        extra_kwargs: Provider-specific overrides forwarded to
            ``chat.completions.create``.
        cache: When True, send explicit prompt-cache markers on the stable
            prefix and the last message (providers that document the
            ``cache_control`` content-part field — see
            :func:`_apply_explicit_cache`).

    Yields:
        Phoson ``LLMEvent`` instances in the order documented by
        :class:`phoson_llm.chats.base.BaseLLMChat`.
    """
    kwargs = _build_request_kwargs(
        config=config,
        messages=messages,
        tools=tools,
        max_tokens_key=max_tokens_key,
        extra_kwargs=extra_kwargs,
        cache=cache,
    )

    text_acc = ""
    reasoning_acc = ""
    tool_acc = ToolCallAccumulator()
    has_tool_calls = False
    final_usage: object = None
    tools_emitted = False
    # Last non-None ``finish_reason`` from the stream (F-13). OpenAI-family
    # adapters signal truncation via ``finish_reason == "length"``; keeping it
    # so the final LLMDoneEvent can report the normalized stop_reason.
    finish_reason: str | None = None

    yield LLMStartEvent(model=config.model, message_count=len(messages))

    try:
        async for chunk in await client.chat.completions.create(**kwargs):
            if not chunk.choices:
                if chunk.usage:
                    final_usage = chunk.usage
                continue

            delta = chunk.choices[0].delta

            if delta.content:
                text_acc += delta.content
                yield TokenEvent(content=delta.content)

            reasoning_chunk = _extract_reasoning_delta(delta)
            if reasoning_chunk:
                if not reasoning_acc:
                    yield ReasoningStartEvent()
                reasoning_acc += reasoning_chunk
                yield ReasoningTokenEvent(content=reasoning_chunk)

            if delta.tool_calls:
                has_tool_calls = True
                for tc in delta.tool_calls:
                    for event in tool_acc.feed_delta(tc):
                        yield event

            finish = chunk.choices[0].finish_reason
            if finish is not None:
                finish_reason = finish
            if finish == "tool_calls" and not tools_emitted:
                tools_emitted = True
                for event in tool_acc.finalize():
                    yield event
                tools_emitted = False

    except APIStatusError as e:
        code = map_error_code(e.status_code)
        if is_context_length_error(e.status_code, str(e.message)):
            code = CONTEXT_LENGTH_ERROR_CODE
        yield ErrorEvent(
            message=str(e.message),
            code=code,
            retryable=_is_retryable(code, e.status_code),
        )
        return

    except APIConnectionError as e:
        yield ErrorEvent(
            message=str(e),
            code="connection_error",
            retryable=True,
        )
        return

    # F-13: if the response was cut off at the token budget mid-tool-call,
    # the buffer still holds fragments that never got a ``tool_calls``
    # finish. Without this they'd be silently dropped — the model's intent
    # is lost and the user sees a dead stop. Instead we emit one truncated
    # ToolCallEvent per fragment (tagged ``_truncated``) so the agent loop
    # answers it with an actionable "retry smaller" result rather than
    # leaving an orphaned tool_use or invoking the handler on partial JSON.
    stop_reason = normalize_stop_reason(finish_reason, provider="openai_compat")
    if stop_reason == "max_tokens":
        truncated_calls = tool_acc.finalize(truncated=True)
        for event in truncated_calls:
            yield event
        # A truncated tool call counts as a tool call even though it never
        # got a ``tool_calls`` finish — so the loop dispatches the (refused)
        # call instead of treating the turn as a plain final answer. (In
        # practice the fragments already set ``has_tool_calls`` in the main
        # loop; this keeps it correct even if that ever changes.)
        if truncated_calls:
            has_tool_calls = True

    if reasoning_acc:
        yield ReasoningDoneEvent(content=reasoning_acc)

    if final_usage is not None:
        prompt_details = getattr(final_usage, "prompt_tokens_details", None)
        usage = TokenUsage(
            input=getattr(final_usage, "prompt_tokens", 0) or 0,
            output=getattr(final_usage, "completion_tokens", 0) or 0,
            # OpenAI reports the cached part of the prompt in
            # prompt_tokens_details.cached_tokens; OpenRouter (and some
            # upstreams) additionally report cache_write_tokens for the
            # first request that establishes a cache entry. Alibaba Model
            # Studio reports the explicit-cache write as
            # prompt_tokens_details.cache_creation_input_tokens.
            cache_read=getattr(prompt_details, "cached_tokens", 0) or 0,
            cache_write=(
                getattr(prompt_details, "cache_write_tokens", 0)
                or getattr(prompt_details, "cache_creation_input_tokens", 0)
                or getattr(final_usage, "cache_creation_input_tokens", 0)
                or 0
            ),
        )
        # A provider-reported cost (OpenRouter's ``usage.cost``) is the
        # authoritative number — it is what the provider actually
        # charged. Prefer it over the local price table, which is only a
        # fallback for providers that don't report a cost (OpenAI, ...).
        provider_cost = _extract_provider_cost(final_usage)
        if provider_cost is not None:
            cost_usd, cost_known = provider_cost, True
        else:
            # OpenAI-compatible usage reports ``prompt_tokens`` as the *gross*
            # input: the cached (``cached_tokens``) and explicit-cache-creation
            # (``cache_creation_input_tokens``) tokens are already counted in
            # it (Model Studio bills the remaining "other" tokens at the
            # standard input price). ``calculate_cost`` prices ``input_tokens``
            # at the full rate and *adds* the cache rates, so handing it the
            # gross count would bill the cached tokens twice. Pass the net
            # (uncached) input; ``usage.input`` keeps the gross count for
            # display/metrics.
            net_input = max(0, usage.input - usage.cache_read - usage.cache_write)
            cost_usd, cost_known = cost_calculator(
                model=config.model,
                input_tokens=net_input,
                output_tokens=usage.output,
                cache_read_tokens=usage.cache_read,
                cache_write_tokens=usage.cache_write,
            )
        yield UsageEvent(
            model=config.model,
            usage=usage,
            cost_usd=cost_usd,
            cost_known=cost_known,
        )

    yield LLMDoneEvent(
        content=text_acc,
        has_tool_calls=has_tool_calls,
        stop_reason=stop_reason,
    )


def _is_retryable(code: str, status_code: int) -> bool:
    """Return True for errors a caller should retry."""
    return code == "rate_limit" or 500 <= status_code < 600


def _extract_provider_cost(usage: object) -> float | None:
    """Return the provider-reported USD cost from a usage object, if any.

    OpenRouter (and a growing set of OpenAI-compatible gateways) include
    a ``cost`` field on the ``usage`` object of the final streaming chunk
    — the amount actually charged to the caller's account. The OpenAI
    SDK models it as an *extra* field (``extra='allow'``), so it is
    reachable via ``getattr`` rather than a typed attribute. OpenAI
    itself does not send a ``cost`` field, so this returns ``None`` for
    it and the caller falls back to the local price table.

    Args:
        usage: The final ``CompletionUsage``-like object from the stream.

    Returns:
        The cost in USD (a positive finite number), or ``None`` when the
        provider did not report one or the value is unusable.
    """
    cost = getattr(usage, "cost", None)
    if cost is None:
        return None
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value
