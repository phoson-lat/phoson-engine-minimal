from typing import Any, Final, Literal
from dataclasses import dataclass
from collections.abc import Sequence

# ─── JSON type aliases ───────────────────────────────────────────────────────
# Shared aliases for JSON payloads (tool arguments, JSON Schema documents).
# They document intent without locking us into a validation library.

type JsonValue = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
type JsonObject = dict[str, JsonValue]
# A JSON Schema object describing a tool's parameters (each provider
# validates it server-side; we only promise it's a JSON object).
type JsonSchema = dict[str, Any]

# ─── Content Blocks ──────────────────────────────────────────────────────────


@dataclass
class TextBlock:
    """Plain text content block for messages."""

    text: str


@dataclass
class ToolUseBlock:
    """LLM requested tool execution. Goes in assistant role messages."""

    tool_call_id: str
    tool_name: str
    args: JsonObject


@dataclass
class ToolResultBlock:
    """Result of tool execution. Goes in user role messages."""

    tool_call_id: str
    result: str
    error: bool = False


# ─── Multimodal Content Blocks ───────────────────────────────────────────────


@dataclass
class ImageBlock:
    """Image content block for multimodal LLM input (vision).

    Source can be:
    - Public URL: "https://example.com/image.png"
    - Base64: "data:image/png;base64,iVBORw0KGgo..."
    - Local file: "file://path/to/image.png"

    The detail level is OpenAI-specific (low, high, auto).
    """

    source: str
    detail: Literal["low", "high", "auto"] = "auto"
    media_type: str | None = None


@dataclass
class AudioBlock:
    """Audio content block for multimodal LLM input.

    Source can be:
    - Public URL: "https://example.com/audio.wav"
    - Base64: "data:audio/wav;base64,..."
    - Local file: "file://path/to/audio.wav"

    Supported by: OpenAI (audio input), Gemini.
    """

    source: str
    format: str = "wav"
    duration_ms: int | None = None


@dataclass
class VideoBlock:
    """Video content block for multimodal LLM input.

    Providers internally sample the video into frames at regular intervals.
    Source can be:
    - Public URL: "https://example.com/video.mp4"
    - Local file: "file://path/to/video.mp4"

    Supported by: Gemini, GPT-4o (experimental).
    """

    source: str
    sampling_interval_ms: int = 2000


@dataclass
class DocumentBlock:
    """PDF document content block for multimodal LLM input.

    Supported by: Anthropic Claude 3.5+ (document parsing).

    Source can be: URL, base64://..., or file://...
    """

    source: str
    pages: int | None = None


# ─── Union ───────────────────────────────────────────────────────────────────


ContentBlock = (
    TextBlock
    | ToolUseBlock
    | ToolResultBlock
    | ImageBlock
    | AudioBlock
    | VideoBlock
    | DocumentBlock
)


# ─── Message ─────────────────────────────────────────────────────────────────


#: Maximum number of characters of an assistant's reasoning that are re-sent
#: to the model in a historical message (#134). Reasoning is a best-effort
#: hint: truncating it does not invalidate the request, but may degrade the
#: model's coherence on long chains of thought. The marker makes the cut
#: visible to the model so it does not mistake the tail for the full thought.
REASONING_MAX_CHARS: Final = 10_000
REASONING_TRUNCATION_MARKER: Final = "...[truncated]"


def cap_reasoning(reasoning: str) -> str:
    """Truncate *reasoning* to :data:`REASONING_MAX_CHARS`, appending a marker.

    The full reasoning is kept in the in-memory history and session
    persistence; the cap is applied only when the text is serialized into an
    outgoing request body, so the context window is not inflated by very long
    chains of thought (#134).
    """
    if len(reasoning) <= REASONING_MAX_CHARS:
        return reasoning
    return reasoning[:REASONING_MAX_CHARS] + REASONING_TRUNCATION_MARKER


@dataclass
class Message:
    """Represents a conversation message with role and content.

    Args:
        role: Sender type - system, user, or assistant.
        content: Text content or list of content blocks for multimodal messages.
        reasoning: The model's chain-of-thought for this turn, captured from
            the stream (``ReasoningDoneEvent``). Adapters that support
            re-sending it (OpenAI-compatible ``reasoning_content``, Anthropic
            ``thinking`` blocks) fold it back into the request so multi-turn
            reasoning models stay coherent (#134). Adapters without such a
            mechanism ignore it (the ``session_id`` pattern). ``None`` when the
            turn produced no reasoning.
        reasoning_signature: Anthropic's opaque per-thinking-block signature.
            Required to re-send a ``thinking`` block; when absent the block is
            dropped (degradation, not an error). Ignored by other adapters.
    """

    role: Literal["system", "user", "assistant"]
    content: str | Sequence[ContentBlock]
    reasoning: str | None = None
    reasoning_signature: str | None = None


# ─── Tool ────────────────────────────────────────────────────────────────────


@dataclass
class ToolDefinition:
    """Definition of a tool that the LLM can call.

    Args:
        name: Unique identifier for the tool.
        description: Human-readable description of what the tool does.
        parameters: JSON Schema object describing the tool's parameters.
    """

    name: str
    description: str
    parameters: JsonSchema


# ─── Config ──────────────────────────────────────────────────────────────────


#: Supported ``reasoning_effort`` levels, in ascending order.
#:
#: ``low``/``medium``/``high`` are the canonical OpenAI levels; ``xhigh`` and
#: ``max`` are Phoson's extended levels for its own reasoning models.
#: OpenAI-compatible backends forward the value as-is (e.g. o1/o3's
#: ``reasoning_effort`` request parameter), so backends that only know the
#: canonical levels may reject the extended ones — that is a per-backend
#: concern, not a schema error.
type ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]

REASONING_EFFORTS: Final = ("low", "medium", "high", "xhigh", "max")


@dataclass
class ModelConfig:
    """Configuration for an LLM inference request.

    Args:
        model: Model identifier (e.g., "gpt-4o", "claude-3-5-sonnet-20241022").
        temperature: Sampling temperature for generation (0.0 to 2.0).
        max_tokens: Maximum tokens to generate (up to 32,768).
        system: Optional system prompt to prepend.
        thinking_budget: Token budget for extended thinking (Anthropic).
        reasoning_effort: Reasoning effort level (OpenAI o1/o3, plus
            Phoson's extended ``xhigh``/``max`` levels).
        session_id: Optional stable identifier for the conversation.
            Adapters that support it (OpenRouter) send it as the
            sticky-routing key so repeated requests land on the same
            upstream provider, keeping its prompt cache warm
            (IMPROVEMENTS.md G2 / #69). Ignored by adapters without
            such a mechanism.
        preserve_thinking: Whether to re-send captured reasoning
            (``Message.reasoning``) to the model on subsequent turns (#134).
            ``None`` (default) = the adapter decides: OpenAI-compatible
            adapters emit ``reasoning_content`` for assistant turns that carry
            reasoning, Anthropic emits signed ``thinking`` blocks, and adapters
            without a reasoning channel ignore it. ``True`` forces emission
            where the adapter supports it; ``False`` never emits it (the
            ``PHOSON_PRESERVE_THINKING`` env var maps to this).
    """

    model: str
    temperature: float = 0.7
    max_tokens: int = 32 * 1024
    system: str | None = None
    thinking_budget: int | None = None
    reasoning_effort: ReasoningEffort | None = None
    session_id: str | None = None
    preserve_thinking: bool | None = None
