"""The main agent engine.

:class:`AgentEngine` is the public entry point. It composes:

  * a :class:`phoson_llm.chats.base.BaseLLMChat` for actually talking
    to a model,
  * a list of :class:`phoson_agent.tool.AgentTool` registered tools,
  * a list of :class:`AgentMiddleware` instances that wrap LLM calls,
    tool calls and event flow,
  * a list of :class:`Plugin` specifications loaded at construction
    time (which may contribute additional tools and middlewares),
  * an :class:`AgentContext` shared with every tool handler.

The actual ReAct loop is implemented in :class:`._loop.AgentLoop`,
tool dispatch in :class:`._tool_runner.ToolRunner`, and helper
sentinels live in :mod:`._internals`. ``AgentEngine`` owns the
public API, the running-flag concurrency guard, the plugin lifecycle,
and the middleware chain construction; everything else is delegated.
"""

import asyncio
from typing import Any
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from phoson_agent._loop import AgentLoop
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ModelConfig,
    ToolCallEvent,
    ToolDefinition,
)
from phoson_agent.models import (
    RunStep,
    AgentTool,
    AgentEvent,
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_agent.plugin import Plugin
from phoson_agent.context import AgentContext
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent._internals import (
    IterationCost,
    IterationFinal,
    IterationFailed,
    check_no_running_loop,
)
from phoson_agent.exceptions import (
    PhosonAgentError,
    PhosonAgentRunningError,
    PhosonMaxIterationsError,
    PhosonPluginCleanupError,
)
from phoson_agent.middleware import LLMCallNext, AgentMiddleware
from phoson_agent._tool_runner import ToolRunner
from phoson_agent.plugin_loader import load_plugin

# Re-export tool events on the engine module for backwards compatibility
# with the agent's public surface.
__all__ = [
    "AgentEngine",
    "AgentToolStartEvent",
    "AgentToolDoneEvent",
]


@dataclass
class AgentEngine:
    """Main engine for running LLM-based agents.

    Supports tools, middlewares and plugins. Each instance is
    *single-flight*: ``stream()`` and ``run()`` cannot be invoked
    concurrently from the same instance. An ``asyncio.Lock`` guards the
    running flag so simultaneous calls in one event loop fail fast with
    :class:`PhosonAgentRunningError`. For true parallelism, instantiate
    one engine per concurrent run.

    Args:
        chat: The LLM chat adapter to drive.
        tools: Registered tools. Plugin tools are appended at
            construction time.
        middlewares: Middlewares that wrap LLM/tool calls and observe
            events. Plugin middlewares are appended at construction time.
        plugins: Plugin specs (package strings, path strings, dicts or
            already-instantiated :class:`Plugin` objects). Resolved by
            :func:`phoson_agent.plugin_loader.load_plugin`.
        context: Shared :class:`AgentContext` injected into every tool
            handler call.
        phoson_weight: Multiplier applied to ``cost_usd`` to derive the
            ``credits`` field on :class:`RunStep`. Defaults to 1.0.
        max_iterations: Maximum ReAct iterations before the engine
            gives up with ``code="max_iterations"``.
    """

    chat: BaseLLMChat
    tools: list[AgentTool] = field(default_factory=list)
    middlewares: list[AgentMiddleware] = field(default_factory=list)
    plugins: list[str | dict[str, Any] | Plugin] = field(default_factory=list)
    context: AgentContext = field(default_factory=AgentContext)
    phoson_weight: float = 1.0
    max_iterations: int = 12

    # Internal state
    _history: list[Message] = field(default_factory=list, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _running_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _loaded_plugins: list[Plugin] = field(
        default_factory=list, init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Load plugins and build the tool index."""
        self._loaded_plugins = []
        for plugin_spec in self.plugins:
            plugin = load_plugin(plugin_spec)
            self._loaded_plugins.append(plugin)
            self.tools.extend(plugin.get_tools())
            self.middlewares.extend(plugin.get_middlewares())

        self._tools_by_name: dict[str, AgentTool] = {
            tool.name: tool for tool in self.tools
        }

        self._tool_runner = ToolRunner(
            tools_by_name=self._tools_by_name,
            context=self.context,
            apply_before_tool=self._apply_before_tool,
            apply_after_tool=self._apply_after_tool,
            prepare_event=self._prepare_event,
        )
        self._loop = AgentLoop(
            tool_runner=self._tool_runner,
            prepare_event=self._prepare_event,
            phoson_weight=self.phoson_weight,
        )

    # ── Public API ──────────────────────────────────────────────────────

    def get_partial_history(self) -> list[Message]:
        """Return a snapshot of the current history (post-stream or in-flight)."""
        return list(self._history)

    def is_running(self) -> bool:
        """Whether ``stream()`` is currently active on this engine."""
        return self._running

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[AgentEvent]:
        """Execute the agent and stream events as they occur.

        Raises:
            PhosonAgentRunningError: If this engine instance is already running.
        """
        async with self._running_lock:
            if self._running:
                raise PhosonAgentRunningError("AgentEngine is already running.")
            self._running = True

        try:
            async for event in self._stream_impl(messages, config):
                yield event
        finally:
            self._running = False

    async def run(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AgentRunResult:
        """Execute the agent until completion and return the result.

        Raises:
            PhosonMaxIterationsError: If the agent exhausts its
                ``max_iterations`` budget without producing a final answer.
            PhosonAgentError: For any other agent-level failure surfaced
                as an :class:`AgentErrorEvent` during the stream.
            RuntimeError: Only if the stream ends without ever yielding a
                terminal :class:`AgentDoneEvent` or :class:`AgentErrorEvent`,
                which indicates a programming bug rather than an expected
                error condition.
        """
        async for event in self.stream(messages, config):
            if isinstance(event, AgentDoneEvent):
                return event.result
            if isinstance(event, AgentErrorEvent):
                code = event.code or "unknown"
                if code == "max_iterations":
                    raise PhosonMaxIterationsError(
                        event.message,
                        max_iterations=self.max_iterations,
                    )
                raise PhosonAgentError(f"Agent error ({code}): {event.message}")

        raise RuntimeError("Agent stream finished without AgentDoneEvent.")

    def run_sync(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AgentRunResult:
        """Execute the agent synchronously.

        Cannot be called from within a running event loop. Use ``run()`` in
        async contexts (Jupyter, FastAPI, etc.).

        Raises:
            RuntimeError: If called from within a running event loop, or if
                the agent fails to produce a final result.
        """
        check_no_running_loop("run_sync")
        return asyncio.run(self.run(messages, config))

    def cleanup(self) -> None:
        """Clean up all loaded plugins.

        Raises:
            PhosonPluginCleanupError: If one or more plugins fail to cleanup.
                The exception's ``failures`` attribute lists every failure so
                the caller can decide how to react.
        """
        failures: list[tuple[str, BaseException]] = []
        for plugin in self._loaded_plugins:
            try:
                plugin.cleanup()
            except Exception as exc:
                failures.append((plugin.name, exc))

        if failures:
            names = ", ".join(name for name, _ in failures)
            raise PhosonPluginCleanupError(
                f"Cleanup failed for plugins: {names}",
                failures=failures,
            )

    def __enter__(self) -> "AgentEngine":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Suppress :class:`PhosonPluginCleanupError` to honour the contextmanager
        protocol. Use :meth:`cleanup` explicitly if you need to handle failures.
        """
        try:
            self.cleanup()
        except PhosonPluginCleanupError:
            pass

    # ── Middleware orchestration ────────────────────────────────────────

    async def _notify_middlewares(self, event: AgentEvent) -> None:
        for middleware in self.middlewares:
            await middleware.on_agent_event(event)

    async def _prepare_event(self, event: AgentEvent) -> AgentEvent:
        await self._notify_middlewares(event)
        return event

    async def _apply_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        updated = messages
        for middleware in self.middlewares:
            updated = await middleware.on_before_llm(updated, config)
        return updated

    def _build_llm_call_chain(
        self,
        tool_definitions: list[ToolDefinition],
    ) -> LLMCallNext:
        """Build the middleware execution chain for the LLM call.

        The middlewares form an "onion": each ``wrap_llm_call`` wraps the
        next call in the chain, with the chat client at the centre.
        Iterating in reverse order builds the chain from the innermost
        layer outwards.
        """

        async def base_call(
            messages: list[Message],
            config: ModelConfig,
        ) -> AsyncIterator[LLMEvent]:
            async for event in self.chat.stream(messages, config, tool_definitions):
                yield event

        call_next: LLMCallNext = base_call
        for middleware in reversed(self.middlewares):
            previous = call_next

            def make_wrapped(
                mw: AgentMiddleware,
                nxt: LLMCallNext,
            ) -> LLMCallNext:
                async def wrapped(
                    messages: list[Message],
                    config: ModelConfig,
                ) -> AsyncIterator[LLMEvent]:
                    async for event in mw.wrap_llm_call(nxt, messages, config):
                        yield event

                return wrapped

            call_next = make_wrapped(middleware, previous)

        return call_next

    async def _apply_before_tool(
        self,
        call: ToolCallEvent,
    ) -> ToolCallEvent | None:
        current: ToolCallEvent | None = call
        for middleware in self.middlewares:
            if current is None:
                return None
            current = await middleware.on_before_tool(current)
        return current

    async def _apply_after_tool(
        self,
        call: ToolCallEvent,
        result: str,
        error: bool,
    ) -> str:
        updated = result
        for middleware in self.middlewares:
            updated = await middleware.on_after_tool(call, updated, error)
        return updated

    def _get_tool_definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in self.tools
        ]

    # ── Outer loop ──────────────────────────────────────────────────────

    async def _stream_impl(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[AgentEvent]:
        """The outer ReAct loop.

        Iterates up to ``max_iterations`` times, delegating each
        iteration to :meth:`AgentLoop.run_iteration` and translating its
        internal sentinels into the public event stream.
        """
        input_snapshot = list(messages)
        history = list(messages)
        self._history = history
        steps: list[RunStep] = []
        total_cost_usd = 0.0
        total_credits = 0.0

        tool_definitions = self._get_tool_definitions()
        llm_call = self._build_llm_call_chain(tool_definitions)

        yield await self._prepare_event(
            AgentStartEvent(
                model=config.model,
                message_count=len(messages),
                max_iterations=self.max_iterations,
            )
        )

        for _ in range(self.max_iterations):
            history = await self._apply_before_llm(history, config)
            self._history = history

            iteration_done = False
            async for event in self._loop.run_iteration(
                history=history,
                config=config,
                llm_call=llm_call,
                steps=steps,
            ):
                if isinstance(event, IterationCost):
                    total_cost_usd += event.cost_usd
                    total_credits += event.credits
                    continue

                if isinstance(event, IterationFinal):
                    iteration_done = True
                    result = AgentRunResult(
                        final_content=event.final_content,
                        history=history,
                        input_messages=input_snapshot,
                        steps=steps,
                        total_cost_usd=total_cost_usd,
                        total_credits=total_credits,
                    )
                    yield await self._prepare_event(AgentDoneEvent(result=result))
                    return

                if isinstance(event, IterationFailed):
                    iteration_done = True
                    yield await self._prepare_event(event.error_event)
                    return

                yield event

            if iteration_done:
                return

        yield await self._prepare_event(
            AgentErrorEvent(
                message=(
                    "Agent reached "
                    f"max_iterations={self.max_iterations} without a final answer."
                ),
                code="max_iterations",
                retryable=False,
            )
        )
