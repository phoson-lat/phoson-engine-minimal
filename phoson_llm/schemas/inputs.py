from typing import Any, Literal
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


@dataclass
class Message:
    """Represents a conversation message with role and content.

    Args:
        role: Sender type - system, user, or assistant.
        content: Text content or list of content blocks for multimodal messages.
    """

    role: Literal["system", "user", "assistant"]
    content: str | Sequence[ContentBlock]


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


@dataclass
class ModelConfig:
    """Configuration for an LLM inference request.

    Args:
        model: Model identifier (e.g., "gpt-4o", "claude-3-5-sonnet-20241022").
        temperature: Sampling temperature for generation (0.0 to 2.0).
        max_tokens: Maximum tokens to generate (up to 32,768).
        system: Optional system prompt to prepend.
        thinking_budget: Token budget for extended thinking (Anthropic).
        reasoning_effort: Reasoning effort level (OpenAI o1/o3).
    """

    model: str
    temperature: float = 0.7
    max_tokens: int = 32 * 1024
    system: str | None = None
    thinking_budget: int | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
