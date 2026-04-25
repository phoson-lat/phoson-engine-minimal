from typing import Literal
from dataclasses import dataclass

# ─── Content Blocks ──────────────────────────────────────────────────────────


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    """El LLM pidió ejecutar esta tool. Va en mensajes role=assistant."""

    tool_call_id: str
    tool_name: str
    args: dict


@dataclass
class ToolResultBlock:
    """Resultado de ejecutar una tool. Va en mensajes role=user."""

    tool_call_id: str
    result: str
    error: bool = False


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


# ─── Mensaje ─────────────────────────────────────────────────────────────────


@dataclass
class Message:
    role: Literal["system", "user", "assistant"]
    content: str | list[ContentBlock]


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
    max_tokens: int = 4096
    system: str | None = None
    # Anthropic extended thinking
    thinking_budget: int | None = None
    # OpenAI o1/o3
    reasoning_effort: Literal["low", "medium", "high"] | None = None
