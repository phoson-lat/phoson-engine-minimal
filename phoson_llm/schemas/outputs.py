import datetime
from dataclasses import field, dataclass

# ─── Base ────────────────────────────────────────────────────────────────────


@dataclass
class LLMEvent:
    timestamp: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )


# ─── Ciclo de vida ───────────────────────────────────────────────────────────


@dataclass
class LLMStartEvent(LLMEvent):
    model: str = ""
    message_count: int = 0


@dataclass
class LLMDoneEvent(LLMEvent):
    content: str = ""
    has_tool_calls: bool = False


# ─── Texto ───────────────────────────────────────────────────────────────────


@dataclass
class TokenEvent(LLMEvent):
    content: str = ""


# ─── Reasoning ───────────────────────────────────────────────────────────────


@dataclass
class ReasoningStartEvent(LLMEvent):
    pass


@dataclass
class ReasoningTokenEvent(LLMEvent):
    content: str = ""


@dataclass
class ReasoningDoneEvent(LLMEvent):
    content: str = ""


# ─── Tool calls ──────────────────────────────────────────────────────────────


@dataclass
class ToolCallDeltaEvent(LLMEvent):
    index: int = 0
    tool_name: str = ""
    args_chunk: str = ""


@dataclass
class ToolCallEvent(LLMEvent):
    index: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    args: dict = field(default_factory=dict)


# ─── Ejecución de tools ──────────────────────────────────────────────────────


@dataclass
class ToolStartEvent(LLMEvent):
    index: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    args: dict = field(default_factory=dict)


@dataclass
class ToolDoneEvent(LLMEvent):
    index: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    result: str = ""
    duration_ms: int = 0
    error: str | None = None


# ─── Usage ───────────────────────────────────────────────────────────────────


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0


@dataclass
class UsageEvent(LLMEvent):
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    cost_known: bool = True


# ─── Error ───────────────────────────────────────────────────────────────────


@dataclass
class ErrorEvent(LLMEvent):
    message: str = ""
    code: str | None = None
    retryable: bool = False
