"""
Module for agent middlewares.
"""

import warnings
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
    """Middleware to automatically retry LLM calls on errors.

    .. deprecated::
        Use :class:`phoson_llm.retry.RetryingChat` instead. It has the
        correct streaming semantics (it only retries *before* any token has
        been emitted, so a committed stream is never re-run and output is
        never duplicated) and it is the layer the CLI actually wires up via
        :func:`phoson_cli.config.build_chat`. This middleware marks a call
        as "visible" on the very first ``LLMStartEvent`` every adapter
        emits, so a retryable error that arrives after the start is
        re-sent instead of retried — it effectively never retries. It is
        kept (and only emits a deprecation warning on construction) so
        existing code that imports it keeps working; do not add it to a
        new middleware chain.
    """

    def __init__(
        self,
        max_retries: int = 2,
        base_delay_seconds: float = 0.5,
        backoff_multiplier: float = 2.0,
    ) -> None:
        warnings.warn(
            "RetryMiddleware is deprecated and does not reliably retry "
            "(a stream is marked visible on LLMStartEvent, which every "
            "adapter emits first). Wrap your chat in "
            "phoson_llm.retry.RetryingChat instead — the CLI does this "
            "automatically via build_chat (config.llm_max_attempts).",
            DeprecationWarning,
            stacklevel=2,
        )
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
