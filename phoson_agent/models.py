import datetime
from typing import TYPE_CHECKING, Any, Literal
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from phoson_llm.schemas import Message, JsonObject, JsonSchema, TokenUsage

if TYPE_CHECKING:
    from phoson_agent.context import AgentContext

ToolReturn = str | dict[str, Any]
ToolHandler = Callable[[JsonObject, "AgentContext"], ToolReturn | Awaitable[ToolReturn]]


@dataclass
class AgentTool:
    """Definition of an agent tool."""

    name: str
    description: str
    parameters: JsonSchema
    handler: ToolHandler


@dataclass
class RunStep:
    """Represents a step in the agent execution run."""

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
    """Result of an agent run."""

    final_content: str
    history: list[Message]
    input_messages: list[Message]
    steps: list[RunStep] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_credits: float = 0.0


@dataclass(kw_only=True)
class AgentEvent:
    """Base class for agent events.

    All event subclasses use ``kw_only=True`` so call sites are
    self-documenting and field ordering between base and subclass is
    not significant.
    """

    timestamp: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC),
        init=False,
    )


@dataclass(kw_only=True)
class AgentStartEvent(AgentEvent):
    """Event emitted when the agent starts."""

    model: str = ""
    message_count: int = 0
    max_iterations: int = 0


@dataclass(kw_only=True)
class AgentTokenEvent(AgentEvent):
    """Event emitted when a token is generated."""

    content: str = ""


@dataclass(kw_only=True)
class AgentReasoningEvent(AgentEvent):
    """Event emitted when a reasoning token is generated."""

    content: str = ""


@dataclass(kw_only=True)
class AgentToolStartEvent(AgentEvent):
    """Event emitted when a tool call starts."""

    index: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    label: str | None = None


@dataclass(kw_only=True)
class AgentToolDoneEvent(AgentEvent):
    """Event emitted when a tool call completes."""

    index: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    result: str = ""
    error: str | None = None
    duration_ms: int = 0
    label: str | None = None


@dataclass(kw_only=True)
class AgentStepDoneEvent(AgentEvent):
    """Event emitted when a step (LLM or tool) completes."""

    step: RunStep


@dataclass(kw_only=True)
class AgentDoneEvent(AgentEvent):
    """Event emitted when the agent finishes."""

    result: AgentRunResult


@dataclass(kw_only=True)
class AgentErrorEvent(AgentEvent):
    """Event emitted when an error occurs."""

    message: str = ""
    code: str | None = None
    retryable: bool = False


@dataclass(kw_only=True)
class AgentSubagentResult(AgentEvent):
    """Event *intended* to be emitted when a subagent completes with its metrics.

    Note:
        **Experimental** — not yet emitted by :class:`AgentEngine`. Subagent
        results currently travel as ordinary tool results (see the CLI
        subagent tools); this event is reserved for first-class subagent
        orchestration. Consumers should not depend on receiving it.
    """

    index: int = 0
    task: str = ""
    result: str = ""
    cost_usd: float = 0.0
    credits: float = 0.0
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
