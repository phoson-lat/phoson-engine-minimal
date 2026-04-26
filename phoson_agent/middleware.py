from abc import ABC

from phoson_llm.schemas import Message, ModelConfig, ToolCallEvent
from phoson_agent.models import AgentEvent


class AgentMiddleware(ABC):
    """
    Middleware hooks for AgentEngine.

    Known limitation (v1): `on_before_tool` can return a modified ToolCallEvent,
    but the assistant-side ToolUseBlock already persisted in history comes from
    the original LLM call. If a middleware mutates tool args/name/id, assistant
    ToolUseBlock and tool execution/result payload may diverge.
    """

    async def on_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        return messages

    async def on_before_tool(
        self,
        call: ToolCallEvent,
    ) -> ToolCallEvent | None:
        return call

    async def on_after_tool(
        self,
        call: ToolCallEvent,
        result: str,
        error: bool,
    ) -> str:
        return result

    async def on_agent_event(self, event: AgentEvent) -> None:
        return None
