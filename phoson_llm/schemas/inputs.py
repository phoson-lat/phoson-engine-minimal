from typing import Literal
from dataclasses import dataclass
from collections.abc import Sequence

# ─── Content Blocks ──────────────────────────────────────────────────────────


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    """LLM requested tool execution. Goes in assistant role messages."""

    tool_call_id: str
    tool_name: str
    args: dict


@dataclass
class ToolResultBlock:
    """Result of tool execution. Goes in user role messages."""

    tool_call_id: str
    result: str
    error: bool = False


# ─── Multimodal Content Blocks ───────────────────────────────────────────────


@dataclass
class ImageBlock:
    """
    Imagen como input para el LLM (vision).

    source puede ser:
    - URL pública:  "https://example.com/image.png"
    - Base64:      "data:image/png;base64,iVBORw0KGgo..."
    - Archivo local: "file://path/to/image.png"
    """

    source: str
    detail: Literal["low", "high", "auto"] = "auto"  # OpenAI only
    media_type: str | None = None  # e.g. "image/png", "image/jpeg"


@dataclass
class AudioBlock:
    """
    Audio como input para el LLM.

    source puede ser:
    - URL pública:   "https://example.com/audio.wav"
    - Base64:       "data:audio/wav;base64,..."
    - Archivo local: "file://path/to/audio.wav"

    Soportado por: OpenAI (audio input), Gemini
    """

    source: str
    format: str = "wav"  # wav, mp3, ogg, flac
    duration_ms: int | None = None


@dataclass
class VideoBlock:
    """
    Video como input para el LLM.

    Los providers dividen internamente el video en frames muestreados.
    source puede ser:
    - URL pública:   "https://example.com/video.mp4"
    - Archivo local: "file://path/to/video.mp4"

    Soportado por: Gemini, GPT-4o (experimental)
    """

    source: str
    sampling_interval_ms: int = 2000  # sample cada N ms por defecto


@dataclass
class DocumentBlock:
    """
    Documento PDF como input.

    Soportado por: Anthropic Claude 3.5+ (document parsing)
    """

    source: str  # URL, base64://..., o file://...
    pages: int | None = None  # total pages (informational)


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
    role: Literal["system", "user", "assistant"]
    content: str | Sequence[ContentBlock]


# ─── Tool ────────────────────────────────────────────────────────────────────


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict  # JSON Schema object


# ─── Config ──────────────────────────────────────────────────────────────────


@dataclass
class ModelConfig:
    model: str
    temperature: float = 0.7
    max_tokens: int = 32 * 1024
    system: str | None = None
    # Anthropic extended thinking
    thinking_budget: int | None = None
    # OpenAI o1/o3
    reasoning_effort: Literal["low", "medium", "high"] | None = None
