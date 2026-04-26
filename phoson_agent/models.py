import datetime
from typing import TYPE_CHECKING, Any, Literal
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from phoson_llm.schemas import Message, TokenUsage

if TYPE_CHECKING:
    from phoson_agent.context import AgentContext

ToolReturn = str | dict[str, Any]
ToolHandler = (
    Callable[[dict[str, Any]], ToolReturn | Awaitable[ToolReturn]]
    | Callable[[dict[str, Any], "AgentContext"], ToolReturn | Awaitable[ToolReturn]]
)


@dataclass
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


@dataclass
class RunStep:
    kind: Literal["llm", "tool"]
    started_at: datetime.datetime
    ended_at: datetime.datetime
    duration_ms: int
    model: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    usage: TokenUsage | None = None
    cost_usd: float = 0.0
    credits: float = 0.0
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunResult:
    final_content: str
    history: list[Message]
    input_messages: list[Message]
    steps: list[RunStep] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_credits: float = 0.0


@dataclass
class AgentEvent:
    timestamp: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC),
        init=False,
    )


@dataclass
class AgentStartEvent(AgentEvent):
    model: str = ""
    message_count: int = 0
    max_iterations: int = 0


@dataclass
class AgentTokenEvent(AgentEvent):
    content: str = ""


@dataclass
class AgentReasoningEvent(AgentEvent):
    content: str = ""


@dataclass
class AgentToolStartEvent(AgentEvent):
    index: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentToolDoneEvent(AgentEvent):
    index: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    result: str = ""
    error: str | None = None
    duration_ms: int = 0


@dataclass
class AgentStepDoneEvent(AgentEvent):
    step: RunStep


@dataclass
class AgentDoneEvent(AgentEvent):
    result: AgentRunResult


@dataclass
class AgentErrorEvent(AgentEvent):
    message: str = ""
    code: str | None = None
    retryable: bool = False
