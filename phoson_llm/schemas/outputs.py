import datetime
from dataclasses import field, dataclass

from phoson_llm.schemas.inputs import JsonObject

# ─── Base ────────────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class LLMEvent:
    """Base class for all LLM events.

    All event subclasses inherit ``kw_only=True`` so that their fields are
    keyword-only. This avoids ordering issues between inherited defaults
    (timestamp) and new fields, and makes call sites self-documenting.

    Contains a timestamp of when the event was emitted.
    """

    timestamp: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )


# ─── Lifecycle ───────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class LLMStartEvent(LLMEvent):
    """Event emitted when an LLM call starts."""

    model: str = ""
    message_count: int = 0


@dataclass(kw_only=True)
class LLMDoneEvent(LLMEvent):
    """Event emitted when an LLM call completes successfully."""

    content: str = ""
    has_tool_calls: bool = False


# ─── Text ───────────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class TokenEvent(LLMEvent):
    """Event emitted when a text token is generated."""

    content: str = ""


# ─── Reasoning ───────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class ReasoningStartEvent(LLMEvent):
    """Event emitted when extended reasoning/thinking starts (Anthropic, OpenAI o1)."""


@dataclass(kw_only=True)
class ReasoningTokenEvent(LLMEvent):
    """Event emitted for each reasoning/thinking token."""

    content: str = ""


@dataclass(kw_only=True)
class ReasoningDoneEvent(LLMEvent):
    """Event emitted when extended reasoning/thinking completes."""

    content: str = ""


# ─── Tool calls ──────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class ToolCallDeltaEvent(LLMEvent):
    """Event emitted for incremental chunks of tool call arguments during streaming."""

    index: int = 0
    tool_name: str = ""
    args_chunk: str = ""


@dataclass(kw_only=True)
class ToolCallEvent(LLMEvent):
    """Event emitted when a complete tool call is ready to execute."""

    index: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    args: JsonObject = field(default_factory=dict)


# ─── Usage ───────────────────────────────────────────────────────────────────


@dataclass
class TokenUsage:
    """Tracks token consumption for an LLM call."""

    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0


@dataclass(kw_only=True)
class UsageEvent(LLMEvent):
    """Event emitted with token usage statistics and cost after an LLM call."""

    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    cost_known: bool = True


# ─── Modalities ─────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class LLMModalitiesEvent(LLMEvent):
    """Event that *would* indicate which input modalities a model supports.

    Supported modalities vary by provider/model (e.g., ["text", "vision",
    "audio"]).

    Note:
        **Experimental / reserved** — no adapter currently emits this event.
        It is kept in the schema for the planned modality discovery feature;
        consumers should not depend on receiving it.
    """

    supported: list[str] = field(default_factory=list)


# ─── Error ───────────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class ErrorEvent(LLMEvent):
    """Event emitted when an error occurs during LLM interaction."""

    message: str = ""
    code: str | None = None
    retryable: bool = False
