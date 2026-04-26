from abc import ABC
from collections.abc import Callable, AsyncIterator

from phoson_llm.schemas import Message, LLMEvent, ErrorEvent, ModelConfig, ToolCallEvent
from phoson_agent.models import AgentEvent

LLMCallNext = Callable[
    [list[Message], ModelConfig],
    AsyncIterator[LLMEvent],
]


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

    async def wrap_llm_call(
        self,
        call_next: LLMCallNext,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[LLMEvent]:
        async for event in call_next(messages, config):
            yield event

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


class RetryMiddleware(AgentMiddleware):
    def __init__(
        self,
        max_retries: int = 2,
        base_delay_seconds: float = 0.5,
        backoff_multiplier: float = 2.0,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay_seconds = base_delay_seconds
        self.backoff_multiplier = backoff_multiplier

    async def wrap_llm_call(
        self,
        call_next: LLMCallNext,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[LLMEvent]:
        import asyncio

        attempt = 0

        while True:
            visible_event_seen = False
            retryable_error: ErrorEvent | None = None

            async for event in call_next(messages, config):
                if (
                    isinstance(event, ErrorEvent)
                    and event.retryable
                    and not visible_event_seen
                ):
                    retryable_error = event
                    break

                if not isinstance(event, (ErrorEvent,)):
                    visible_event_seen = True

                yield event

            if retryable_error is None:
                return

            attempt += 1
            if attempt > self.max_retries:
                yield retryable_error
                return

            delay = self.base_delay_seconds * (self.backoff_multiplier ** (attempt - 1))
            await asyncio.sleep(delay)
