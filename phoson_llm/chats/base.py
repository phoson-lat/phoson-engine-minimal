import asyncio
from abc import ABC, abstractmethod
from collections.abc import Iterator, AsyncIterator

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolDefinition,
)


class BaseLLMChat(ABC):
    """
    Base contract for all provider adapters (Anthropic, OpenAI, Google, ...).

    Only stream() is abstract — adapters implement only that method.
    The other three (complete, stream_sync, complete_sync) are inherited for free.

    Guaranteed order of events in stream():
        LLMStartEvent
        (ReasoningStartEvent → ReasoningTokenEvent* → ReasoningDoneEvent)?
        (TokenEvent | ToolCallDeltaEvent | ToolCallEvent)*
        UsageEvent
        LLMDoneEvent | ErrorEvent
    """

    # ── Abstract: the only method each adapter must implement ─────────────

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """
        Calls the LLM and returns an AsyncIterator of normalized LLMEvents.

        Args:
            messages (list[Message]): List of messages.
            config (ModelConfig): Model configuration.
            tools (list[ToolDefinition] | None): Optional tools.

        Returns:
            AsyncIterator[LLMEvent]: Events from the LLM lifecycle.
        """
        ...

    # ── Async completo ────────────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> LLMDoneEvent:
        """
        Async non-streaming version. Consumes the stream internally and
        returns only the final LLMDoneEvent.

        Args:
            messages (list[Message]): List of messages.
            config (ModelConfig): Model configuration.
            tools (list[ToolDefinition] | None): Optional tools.

        Returns:
            LLMDoneEvent: Final LLM event.

        Raises:
            RuntimeError: If an error occurs or the stream does not emit LLMDoneEvent.
        """
        async for event in self.stream(messages, config, tools):
            if isinstance(event, LLMDoneEvent):
                return event
            if isinstance(event, ErrorEvent):
                raise RuntimeError(f"[{event.code}] {event.message}")

        raise RuntimeError(
            "The stream finished without emitting LLMDoneEvent or ErrorEvent."
        )

    # ── Sync streaming ────────────────────────────────────────────────────────

    def stream_sync(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> Iterator[LLMEvent]:
        """
        Sync version of the stream. Creates an isolated event loop.

        Args:
            messages (list[Message]): List of messages.
            config (ModelConfig): Model configuration.
            tools (list[ToolDefinition] | None): Optional tools.

        Yields:
            LLMEvent: Events from the LLM lifecycle.
        """
        loop = asyncio.new_event_loop()
        try:
            aiter = self.stream(messages, config, tools)
            while True:
                try:
                    yield loop.run_until_complete(aiter.__anext__())
                except StopAsyncIteration:
                    break
        finally:
            loop.close()

    # ── Sync completo ─────────────────────────────────────────────────────────

    def complete_sync(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> LLMDoneEvent:
        """
        Sync non-streaming version. Consumes stream_sync.

        Args:
            messages (list[Message]): List of messages.
            config (ModelConfig): Model configuration.
            tools (list[ToolDefinition] | None): Optional tools.

        Returns:
            LLMDoneEvent: Final LLM event.

        Raises:
            RuntimeError: If an error occurs or the stream does not emit LLMDoneEvent.
        """
        for event in self.stream_sync(messages, config, tools):
            if isinstance(event, LLMDoneEvent):
                return event
            if isinstance(event, ErrorEvent):
                raise RuntimeError(f"[{event.code}] {event.message}")

        raise RuntimeError(
            "The stream finished without emitting LLMDoneEvent or ErrorEvent."
        )
