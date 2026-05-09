"""
Module for the main agent engine.
"""

import json
import asyncio
import datetime
from typing import Any
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    ErrorEvent,
    TokenEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ReasoningTokenEvent,
)
from phoson_agent.models import (
    RunStep,
    AgentTool,
    AgentEvent,
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)
from phoson_agent.plugin import Plugin
from phoson_agent.context import AgentContext
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent.exceptions import (
    PhosonAgentError,
    PhosonAgentRunningError,
    PhosonMaxIterationsError,
    PhosonPluginCleanupError,
)
from phoson_agent.middleware import LLMCallNext, AgentMiddleware
from phoson_agent.plugin_loader import load_plugin


def _now_utc() -> datetime.datetime:
    """Returns the current date and time in UTC."""
    return datetime.datetime.now(datetime.UTC)


def _duration_ms(started_at: datetime.datetime, ended_at: datetime.datetime) -> int:
    """Calculates the duration in milliseconds between two timestamps."""
    return int((ended_at - started_at).total_seconds() * 1000)


def _to_result_text(value: str | dict[str, Any]) -> str:
    """Converts a tool result to a text string."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True)


def _subagent_label(tool_name: str) -> str | None:
    """Returns the UI label for subagent-like tools, or None."""
    if tool_name == "agent":
        return "subagent"
    if tool_name == "agents":
        return "subagents"
    return None


@dataclass
class _LLMStepOutcome:
    """Aggregated output of consuming a single LLM stream iteration."""

    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    usage_event: UsageEvent | None = None
    done_event: LLMDoneEvent | None = None
    error_event: ErrorEvent | None = None


@dataclass
class AgentEngine:
    """
    Main engine for running LLM-based agents
    with support for tools, middleware, and plugins.

    Note:
        Each AgentEngine instance is single-flight: ``stream()`` and ``run()``
        cannot be invoked concurrently from the same instance. An asyncio.Lock
        guards the running flag to prevent races inside a single event loop;
        for true parallelism, instantiate one engine per concurrent run.
    """

    chat: BaseLLMChat
    tools: list[AgentTool] = field(default_factory=list)
    middlewares: list[AgentMiddleware] = field(default_factory=list)
    plugins: list[str | dict[str, Any] | Plugin] = field(default_factory=list)
    context: AgentContext = field(default_factory=AgentContext)
    phoson_weight: float = 1.0
    max_iterations: int = 12
    _history: list[Message] = field(default_factory=list, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _running_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _loaded_plugins: list[Plugin] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initializes plugins, tools, and middlewares."""
        # Load plugins
        self._loaded_plugins = []
        for plugin_spec in self.plugins:
            plugin = load_plugin(plugin_spec)
            self._loaded_plugins.append(plugin)

            # Add plugin tools and middlewares
            self.tools.extend(plugin.get_tools())
            self.middlewares.extend(plugin.get_middlewares())

        # Build tool map
        self._tools_by_name: dict[str, AgentTool] = {
            tool.name: tool for tool in self.tools
        }

    # ── Public API ──────────────────────────────────────────────────────

    def get_partial_history(self) -> list[Message]:
        """Returns the current message history."""
        return list(self._history)

    def is_running(self) -> bool:
        """Checks if the agent is currently running."""
        return self._running

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[AgentEvent]:
        """
        Executes the agent and streams events as they occur.

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
        """Executes the agent until completion and returns the result.

        Raises:
            PhosonMaxIterationsError: If the agent exhausts its
                ``max_iterations`` budget without producing a final answer.
            PhosonAgentError: For any other agent-level failure surfaced as
                an :class:`AgentErrorEvent` during the stream.
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

    def run_sync(self, messages: list[Message], config: ModelConfig) -> AgentRunResult:
        """Executes the agent synchronously.

        Cannot be called from within a running event loop. Use ``run()`` in
        async contexts (Jupyter, FastAPI, etc.).

        Raises:
            RuntimeError: If called from within a running event loop, or if
                the agent fails to produce a final result.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "run_sync() cannot be called from within a running event loop. "
                "Use run() instead."
            )

        return asyncio.run(self.run(messages, config))

    def cleanup(self) -> None:
        """Cleanup all loaded plugins.

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
        """Context manager support."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Context manager cleanup.

        Suppresses PhosonPluginCleanupError to honor the contextmanager
        protocol. Use ``cleanup()`` explicitly if you need to handle failures.
        """
        try:
            self.cleanup()
        except PhosonPluginCleanupError:
            pass

    # ── Middleware orchestration ────────────────────────────────────────

    async def _notify_middlewares(self, event: AgentEvent) -> None:
        """Notifies all middlewares about an agent event."""
        for middleware in self.middlewares:
            await middleware.on_agent_event(event)

    async def _prepare_event(self, event: AgentEvent) -> AgentEvent:
        """Prepares an event, notifying the middlewares."""
        await self._notify_middlewares(event)
        return event

    async def _apply_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        """Applies middlewares before calling the LLM."""
        updated = messages
        for middleware in self.middlewares:
            updated = await middleware.on_before_llm(updated, config)
        return updated

    def _build_llm_call_chain(
        self,
        tool_definitions: list[ToolDefinition],
    ) -> LLMCallNext:
        """Builds the middleware execution chain for the LLM call."""

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
        """Applies middlewares before executing a tool."""
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
        """Applies middlewares after executing a tool."""
        updated = result
        for middleware in self.middlewares:
            updated = await middleware.on_after_tool(call, updated, error)
        return updated

    def _get_tool_definitions(self) -> list[ToolDefinition]:
        """Builds tool definitions from registered tools."""
        return [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in self.tools
        ]

    # ── Core loop ───────────────────────────────────────────────────────

    async def _stream_impl(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[AgentEvent]:
        """The actual streaming logic, separated from the locking concern."""
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
            async for event in self._run_iteration(
                history=history,
                config=config,
                llm_call=llm_call,
                steps=steps,
            ):
                if isinstance(event, _IterationCost):
                    total_cost_usd += event.cost_usd
                    total_credits += event.credits
                    continue

                if isinstance(event, _IterationFinal):
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

                if isinstance(event, _IterationFailed):
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

    async def _run_iteration(
        self,
        history: list[Message],
        config: ModelConfig,
        llm_call: LLMCallNext,
        steps: list[RunStep],
    ) -> AsyncIterator[AgentEvent]:
        """Runs one iteration: LLM call + optional tool execution.

        Yields a mix of public AgentEvents and internal control sentinels
        (_IterationCost, _IterationFinal, _IterationFailed) that the outer
        loop interprets.
        """
        llm_started = _now_utc()
        outcome = _LLMStepOutcome()

        async for agent_event in self._consume_llm_stream(
            llm_call=llm_call,
            history=history,
            config=config,
            outcome=outcome,
        ):
            yield agent_event

        llm_ended = _now_utc()
        llm_step = self._build_llm_step(outcome, config, llm_started, llm_ended)
        steps.append(llm_step)

        yield _IterationCost(
            cost_usd=llm_step.cost_usd,
            credits=llm_step.credits,
        )
        yield await self._prepare_event(AgentStepDoneEvent(step=llm_step))

        if outcome.error_event is not None:
            yield _IterationFailed(
                error_event=AgentErrorEvent(
                    message=outcome.error_event.message,
                    code=outcome.error_event.code,
                    retryable=outcome.error_event.retryable,
                )
            )
            return

        if outcome.done_event is None:
            yield _IterationFailed(
                error_event=AgentErrorEvent(
                    message="LLM stream finished without LLMDoneEvent.",
                    code="llm_protocol",
                    retryable=False,
                )
            )
            return

        if not outcome.done_event.has_tool_calls:
            history.append(
                Message(role="assistant", content=outcome.done_event.content)
            )
            yield _IterationFinal(final_content=outcome.done_event.content)
            return

        if not outcome.tool_calls:
            yield _IterationFailed(
                error_event=AgentErrorEvent(
                    message="LLM indicated tool calls but emitted none.",
                    code="llm_protocol",
                    retryable=False,
                )
            )
            return

        # Append assistant message with tool_use blocks
        history.append(self._build_assistant_message(outcome))

        # Execute every tool call in order
        async for agent_event in self._execute_tool_calls(
            tool_calls=outcome.tool_calls,
            history=history,
            steps=steps,
        ):
            yield agent_event

    async def _consume_llm_stream(
        self,
        llm_call: LLMCallNext,
        history: list[Message],
        config: ModelConfig,
        outcome: _LLMStepOutcome,
    ) -> AsyncIterator[AgentEvent]:
        """Consumes the LLM event stream, populating ``outcome`` and yielding
        the public-facing AgentEvents (tokens, reasoning).
        """
        async for event in llm_call(history, config):
            if isinstance(event, TokenEvent):
                yield await self._prepare_event(
                    AgentTokenEvent(content=event.content)
                )
            elif isinstance(event, ReasoningTokenEvent):
                yield await self._prepare_event(
                    AgentReasoningEvent(content=event.content)
                )
            elif isinstance(event, ToolCallEvent):
                outcome.tool_calls.append(event)
            elif isinstance(event, UsageEvent):
                outcome.usage_event = event
            elif isinstance(event, LLMDoneEvent):
                outcome.done_event = event
            elif isinstance(event, ErrorEvent):
                outcome.error_event = event
            elif isinstance(event, LLMStartEvent):
                continue

    def _build_llm_step(
        self,
        outcome: _LLMStepOutcome,
        config: ModelConfig,
        started_at: datetime.datetime,
        ended_at: datetime.datetime,
    ) -> RunStep:
        """Builds a RunStep summarizing the LLM call."""
        usage = outcome.usage_event
        error = outcome.error_event
        cost_usd = usage.cost_usd if usage else 0.0
        credits = cost_usd * self.phoson_weight

        if error is not None and error.code:
            error_text: str | None = f"[{error.code}] {error.message}"
        elif error is not None:
            error_text = error.message
        else:
            error_text = None

        return RunStep(
            kind="llm",
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=_duration_ms(started_at, ended_at),
            model=config.model,
            usage=usage.usage if usage else None,
            cost_usd=cost_usd,
            credits=credits,
            error=error_text,
            payload={
                "input_tokens": usage.usage.input if usage else 0,
                "output_tokens": usage.usage.output if usage else 0,
            },
        )

    def _build_assistant_message(self, outcome: _LLMStepOutcome) -> Message:
        """Builds the assistant message containing text + tool_use blocks."""
        assert outcome.done_event is not None  # checked by caller
        blocks: list[TextBlock | ToolUseBlock] = []
        if outcome.done_event.content:
            blocks.append(TextBlock(text=outcome.done_event.content))
        for call in outcome.tool_calls:
            blocks.append(
                ToolUseBlock(
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    args=call.args,
                )
            )
        return Message(role="assistant", content=blocks)

    # ── Tool execution ──────────────────────────────────────────────────

    async def _execute_tool_calls(
        self,
        tool_calls: list[ToolCallEvent],
        history: list[Message],
        steps: list[RunStep],
    ) -> AsyncIterator[AgentEvent]:
        """Executes every tool call in order, with cancellation handling."""
        for call_idx, original_call in enumerate(tool_calls):
            committed = False
            try:
                async for agent_event, did_commit in self._execute_single_tool_call(
                    original_call, history, steps
                ):
                    yield agent_event
                    if did_commit:
                        committed = True
            except asyncio.CancelledError:
                start_idx = call_idx + 1 if committed else call_idx
                self._fill_cancelled_results(history, tool_calls[start_idx:])
                raise

    async def _execute_single_tool_call(
        self,
        original_call: ToolCallEvent,
        history: list[Message],
        steps: list[RunStep],
    ) -> AsyncIterator[tuple[AgentEvent, bool]]:
        """Executes a single tool call and yields (event, committed) pairs.

        ``committed`` is True for the event that follows a successful append
        to ``history``; the cancellation handler uses this flag to know
        whether to re-emit a cancellation result for this tool.
        """
        call = await self._apply_before_tool(original_call)

        if call is None:
            async for event in self._handle_blocked_tool(original_call, history, steps):
                yield event, True
            return

        yield (
            await self._prepare_event(
                AgentToolStartEvent(
                    index=call.index,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    args=call.args,
                    label=_subagent_label(call.tool_name),
                )
            ),
            False,
        )

        tool_started = _now_utc()
        result_text, error_text, error_flag = await self._invoke_tool_handler(call)

        result_text = await self._apply_after_tool(
            call=call,
            result=result_text,
            error=error_flag,
        )

        tool_ended = _now_utc()
        tool_step = RunStep(
            kind="tool",
            started_at=tool_started,
            ended_at=tool_ended,
            duration_ms=_duration_ms(tool_started, tool_ended),
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            error=error_text,
            payload={
                "args": call.args,
                "result": result_text,
            },
        )
        steps.append(tool_step)

        history.append(
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id=call.tool_call_id,
                        result=result_text,
                        error=error_flag,
                    )
                ],
            )
        )

        yield (
            await self._prepare_event(
                AgentToolDoneEvent(
                    index=call.index,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    result=result_text,
                    error=error_text,
                    duration_ms=tool_step.duration_ms,
                    label=_subagent_label(call.tool_name),
                )
            ),
            True,
        )
        yield (
            await self._prepare_event(AgentStepDoneEvent(step=tool_step)),
            False,
        )

    async def _invoke_tool_handler(
        self,
        call: ToolCallEvent,
    ) -> tuple[str, str | None, bool]:
        """Invokes a tool handler and returns (result_text, error_text, error_flag).

        Catches all handler exceptions and surfaces them as tool errors so the
        agent loop can inform the LLM and continue.
        """
        tool = self._tools_by_name.get(call.tool_name)
        if tool is None:
            error_text = f"Tool '{call.tool_name}' is not registered."
            return error_text, error_text, True

        try:
            tool_result = tool.handler(call.args, self.context)
            if asyncio.iscoroutine(tool_result):
                tool_result = await tool_result

            if not isinstance(tool_result, (str, dict)):
                raise TypeError(
                    "Tool handler must return str, dict, "
                    "or awaitable of those types."
                )

            return _to_result_text(tool_result), None, False
        except Exception as exc:
            error_text = str(exc)
            return error_text, error_text, True

    async def _handle_blocked_tool(
        self,
        original_call: ToolCallEvent,
        history: list[Message],
        steps: list[RunStep],
    ) -> AsyncIterator[AgentEvent]:
        """Handles a tool call that was rejected by middleware."""
        cancelled_result = "Tool execution blocked by middleware."
        history.append(
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_call_id=original_call.tool_call_id,
                        result=cancelled_result,
                        error=True,
                    )
                ],
            )
        )

        now = _now_utc()
        blocked_step = RunStep(
            kind="tool",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            tool_name=original_call.tool_name,
            tool_call_id=original_call.tool_call_id,
            error="blocked_by_middleware",
            payload={
                "args": original_call.args,
                "result": cancelled_result,
            },
        )
        steps.append(blocked_step)

        yield await self._prepare_event(
            AgentToolDoneEvent(
                index=original_call.index,
                tool_call_id=original_call.tool_call_id,
                tool_name=original_call.tool_name,
                result=cancelled_result,
                error="blocked_by_middleware",
                duration_ms=0,
            )
        )
        yield await self._prepare_event(AgentStepDoneEvent(step=blocked_step))

    def _fill_cancelled_results(
        self,
        history: list[Message],
        pending_calls: list[ToolCallEvent],
    ) -> None:
        """Appends synthetic 'cancelled' results so the LLM history stays valid.

        When the agent task is cancelled mid-flight, every pending tool call
        still needs a matching result block, otherwise the next LLM call would
        complain about orphaned tool_use blocks.
        """
        for pending_call in pending_calls:
            history.append(
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_call_id=pending_call.tool_call_id,
                            result="Tool execution cancelled by user.",
                            error=True,
                        )
                    ],
                )
            )


# ── Internal control sentinels for _stream_impl ────────────────────────


@dataclass(kw_only=True)
class _IterationCost(AgentEvent):
    """Internal: signals incremental cost from one LLM call."""

    cost_usd: float = 0.0
    credits: float = 0.0


@dataclass(kw_only=True)
class _IterationFinal(AgentEvent):
    """Internal: signals the iteration produced a final assistant answer."""

    final_content: str = ""


@dataclass(kw_only=True)
class _IterationFailed(AgentEvent):
    """Internal: signals the iteration failed, carrying the public error."""

    error_event: AgentErrorEvent = field(default_factory=AgentErrorEvent)
