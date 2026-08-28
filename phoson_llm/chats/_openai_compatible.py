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
import base64
import warnings
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, NotRequired
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from openai import AsyncOpenAI, APIStatusError, APIConnectionError

from phoson_llm.utils import (
    CONTEXT_LENGTH_ERROR_CODE,
    map_error_code,
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
)

if TYPE_CHECKING:
    from phoson_llm.schemas import Message, ContentBlock, ToolDefinition


# ─── Cost callback contract ──────────────────────────────────────────────────


class CostCalculator(Protocol):
    """Compute the USD cost and confidence flag for a finished call.

    Implementations may return ``(0.0, False)`` to signal that the cost is
    not knowable (e.g. OpenRouter, where the provider's price varies).
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


# ─── Message / tool conversion ──────────────────────────────────────────────


def _convert_content_block(block: "ContentBlock") -> JsonObject:
    """Converts a Phoson ContentBlock to OpenAI-compatible API format."""
    from phoson_llm.utils import load_file_as_base64
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
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
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


def _convert_messages(messages: list["Message"]) -> list[_MessageDict]:
    """Converts Phoson messages to OpenAI-compatible format."""
    from phoson_llm.schemas import TextBlock, ToolUseBlock, ToolResultBlock

    result: list[_MessageDict] = []

    for msg in messages:
        if msg.role == "system":
            content = msg.content if isinstance(msg.content, str) else ""
            result.append({"role": "system", "content": content})
            continue

        if isinstance(msg.content, str):
            result.append({"role": msg.role, "content": msg.content})
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
            result.append(
                {
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
            )

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
            result.append({"role": msg.role, "content": parts})

        elif text_blocks and not tool_uses and not tool_results:
            result.append(
                {
                    "role": msg.role,
                    "content": " ".join(b.text for b in text_blocks),
                }
            )

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

    def finalize(self) -> list[ToolCallEvent]:
        """Build :class:`ToolCallEvent` for every accumulated call and reset.

        Calls missing an id or a name are skipped (malformed stream). The
        buffers are cleared so a subsequent ``tool_calls`` finish in the
        same response starts fresh.
        """
        events: list[ToolCallEvent] = []
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


def _build_request_kwargs(
    *,
    config: ModelConfig,
    messages: list["Message"],
    tools: list["ToolDefinition"] | None,
    max_tokens_key: str,
    extra_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the kwargs dict for ``chat.completions.create``.

    Encapsulates the differences between providers (``max_tokens`` vs.
    ``max_completion_tokens``) and the system-message normalization.
    """
    kwargs: dict[str, Any] = {
        "model": config.model,
        max_tokens_key: config.max_tokens,
        "messages": _convert_messages(messages),
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

    return kwargs


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
    )

    text_acc = ""
    reasoning_acc = ""
    tool_acc = ToolCallAccumulator()
    has_tool_calls = False
    final_usage: object = None
    tools_emitted = False

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
            # first request that establishes a cache entry.
            cache_read=getattr(prompt_details, "cached_tokens", 0) or 0,
            cache_write=getattr(prompt_details, "cache_write_tokens", 0) or 0,
        )
        # A provider-reported cost (OpenRouter's ``usage.cost``) is the
        # authoritative number — it is what the provider actually
        # charged. Prefer it over the local price table, which is only a
        # fallback for providers that don't report a cost (OpenAI, ...).
        provider_cost = _extract_provider_cost(final_usage)
        if provider_cost is not None:
            cost_usd, cost_known = provider_cost, True
        else:
            cost_usd, cost_known = cost_calculator(
                model=config.model,
                input_tokens=usage.input,
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

    yield LLMDoneEvent(content=text_acc, has_tool_calls=has_tool_calls)


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
