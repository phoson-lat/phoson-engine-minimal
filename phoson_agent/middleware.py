"""
Module for agent middlewares.
"""

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
    Base class for agent engine middlewares.
    """

    async def on_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        """Hook executed before calling the LLM."""
        return messages

    async def wrap_llm_call(
        self,
        call_next: LLMCallNext,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[LLMEvent]:
        """Wraps the LLM call to intercept events."""
        async for event in call_next(messages, config):
            yield event

    async def on_before_tool(
        self,
        call: ToolCallEvent,
    ) -> ToolCallEvent | None:
        """Hook executed before executing a tool."""
        return call

    async def on_after_tool(
        self,
        call: ToolCallEvent,
        result: str,
        error: bool,
    ) -> str:
        """Hook executed after executing a tool."""
        return result

    async def on_agent_event(self, event: AgentEvent) -> None:
        """Hook executed on any agent event."""
        return None


class RetryMiddleware(AgentMiddleware):
    """
    Middleware to automatically retry LLM calls on errors.
    """

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
