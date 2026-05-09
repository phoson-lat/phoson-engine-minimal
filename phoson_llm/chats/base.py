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
from phoson_llm.exceptions import PhosonProviderError, PhosonLLMProtocolError


def _check_no_running_loop(method_name: str) -> None:
    """Raise RuntimeError if called from within a running event loop.

    Args:
        method_name: The sync method name to include in the error message.

    Raises:
        RuntimeError: If a running event loop is detected.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop running — safe to proceed
    raise RuntimeError(
        f"{method_name}() cannot be called from within a running event loop. "
        f"Use the async version instead."
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
    def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """
        Calls the LLM and returns an AsyncIterator of normalized LLMEvents.

        Implementations should be async generators (functions with `yield`),
        which Python recognizes as returning AsyncIterator.

        Args:
            messages: List of messages.
            config: Model configuration.
            tools: Optional tools.

        Returns:
            AsyncIterator over events from the LLM lifecycle.
        """
        ...

    # ── Async complete ────────────────────────────────────────────────────────

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
            messages: List of messages.
            config: Model configuration.
            tools: Optional tools.

        Returns:
            LLMDoneEvent with the final assistant content.

        Raises:
            PhosonProviderError: If the provider returned an error event.
            PhosonLLMProtocolError: If the stream did not emit LLMDoneEvent.
        """
        async for event in self.stream(messages, config, tools):
            if isinstance(event, LLMDoneEvent):
                return event
            if isinstance(event, ErrorEvent):
                raise PhosonProviderError(
                    event.message,
                    code=event.code,
                    retryable=event.retryable,
                )

        raise PhosonLLMProtocolError(
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
        Sync version of the stream. Uses asyncio.run() internally.

        Cannot be called from inside a running event loop. Use stream() instead
        when in async contexts (Jupyter, FastAPI, etc.).

        Args:
            messages: List of messages.
            config: Model configuration.
            tools: Optional tools.

        Yields:
            LLMEvent objects from the LLM lifecycle.

        Raises:
            RuntimeError: If called from within a running event loop.
        """
        _check_no_running_loop("stream_sync")

        async def _collect() -> list[LLMEvent]:
            return [event async for event in self.stream(messages, config, tools)]

        events = asyncio.run(_collect())
        yield from events

    # ── Sync complete ─────────────────────────────────────────────────────────

    def complete_sync(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> LLMDoneEvent:
        """
        Sync non-streaming version. Uses asyncio.run() internally.

        Cannot be called from inside a running event loop. Use complete()
        instead when in async contexts.

        Args:
            messages: List of messages.
            config: Model configuration.
            tools: Optional tools.

        Returns:
            LLMDoneEvent with the final assistant content.

        Raises:
            PhosonProviderError: If the provider returned an error event.
            PhosonLLMProtocolError: If the stream did not emit LLMDoneEvent.
            RuntimeError: If called from within a running event loop.
        """
        _check_no_running_loop("complete_sync")
        return asyncio.run(self.complete(messages, config, tools))
