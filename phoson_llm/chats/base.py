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
    Contrato base para todos los adapters de providers (Anthropic, OpenAI, Google, ...).

    Solo stream() es abstracto — los adapters implementan únicamente ese método.
    Los otros tres (complete, stream_sync, complete_sync) se heredan gratis.

    Orden garantizado de eventos en stream():
        LLMStartEvent
        (ReasoningStartEvent → ReasoningTokenEvent* → ReasoningDoneEvent)?
        (TokenEvent | ToolCallDeltaEvent | ToolCallEvent)*
        UsageEvent
        LLMDoneEvent | ErrorEvent
    """

    # ── Abstracto: único método que cada adapter debe implementar ─────────────

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """
        Llama al LLM y devuelve un AsyncIterator de LLMEvents normalizados.

        Args:
            messages (list[Message]): Lista de mensajes.
            config (ModelConfig): Configuración del modelo.
            tools (list[ToolDefinition] | None): Herramientas opcionales.

        Returns:
            AsyncIterator[LLMEvent]: Eventos del ciclo de vida del LLM.
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
        Versión async no-streaming. Consume el stream internamente y
        retorna solo el LLMDoneEvent final.

        Args:
            messages (list[Message]): Lista de mensajes.
            config (ModelConfig): Configuración del modelo.
            tools (list[ToolDefinition] | None): Herramientas opcionales.

        Returns:
            LLMDoneEvent: Evento final del LLM.

        Raises:
            RuntimeError: Si ocurre un error o el stream no emite LLMDoneEvent.
        """
        async for event in self.stream(messages, config, tools):
            if isinstance(event, LLMDoneEvent):
                return event
            if isinstance(event, ErrorEvent):
                raise RuntimeError(f"[{event.code}] {event.message}")

        raise RuntimeError("El stream terminó sin emitir LLMDoneEvent ni ErrorEvent.")

    # ── Sync streaming ────────────────────────────────────────────────────────

    def stream_sync(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> Iterator[LLMEvent]:
        """
        Versión sync del stream. Crea un event loop aislado.

        Args:
            messages (list[Message]): Lista de mensajes.
            config (ModelConfig): Configuración del modelo.
            tools (list[ToolDefinition] | None): Herramientas opcionales.

        Yields:
            LLMEvent: Eventos del ciclo de vida del LLM.
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
        Versión sync no-streaming. Consume stream_sync.

        Args:
            messages (list[Message]): Lista de mensajes.
            config (ModelConfig): Configuración del modelo.
            tools (list[ToolDefinition] | None): Herramientas opcionales.

        Returns:
            LLMDoneEvent: Evento final del LLM.

        Raises:
            RuntimeError: Si ocurre un error o el stream no emite LLMDoneEvent.
        """
        for event in self.stream_sync(messages, config, tools):
            if isinstance(event, LLMDoneEvent):
                return event
            if isinstance(event, ErrorEvent):
                raise RuntimeError(f"[{event.code}] {event.message}")

        raise RuntimeError("El stream terminó sin emitir LLMDoneEvent ni ErrorEvent.")
