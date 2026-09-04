"""Sub-agent tools for Phoson CLI.

These tools spawn fresh ``AgentEngine`` instances to run isolated tasks
either one at a time (``agent``) or in parallel (``agents``). The two
helpers share the same set of injected dependencies (chat client, tool
registry, default model and iteration budget) and emit results as
plain strings so the parent agent can consume them as tool results.
"""

import os
import copy
import time
import asyncio
import logging
from typing import Any, Annotated
from collections.abc import Callable

from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import Message, ModelConfig
from phoson_agent.models import (
    AgentTool,
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStepDoneEvent,
)
from phoson_agent.context import AgentContext
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.exceptions import PhosonProviderError
from phoson_agent.exceptions import PhosonAgentError, PhosonMaxIterationsError
from phoson_agent.middleware import AgentMiddleware

from ._timeouts import sanitize_timeout
from .subagent_panel import (
    AgentStatus,
    SubagentProgress,
    format_agent_block,
    format_metrics_line,
)

_LOGGER = logging.getLogger("phoson_cli.subagent")


def _debug_enabled() -> bool:
    return os.environ.get("PHOSON_SUBAGENT_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _log_debug(message: str, **fields: Any) -> None:
    if not _debug_enabled():
        return

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

    extra = " ".join(f"{key}={value!r}" for key, value in fields.items())
    _LOGGER.debug(f"{message}{' ' + extra if extra else ''}")


def _clone_chat(chat: BaseLLMChat) -> BaseLLMChat:
    """Return a shallow copy of ``chat`` so concurrent runs do not share state.

    Most ``BaseLLMChat`` implementations hold an HTTP client and a few
    config fields. ``copy.copy`` preserves those without bypassing
    ``__init__`` or any dataclass post-init logic.
    """
    return copy.copy(chat)


def _subagent_context(
    *,
    safe_mode: bool,
    bash_confirmation: Any,
    plugin_ui: Any,
) -> AgentContext:
    """Fresh :class:`AgentContext` for a sub-agent engine (#174/F-01).

    Carries the runtime flags a sub-agent's tools need so they behave
    identically to the parent's:

    - ``safe_mode`` + the interactive ``bash_confirmation`` — F-01: before
      this the sub-engine ran with an *empty* context, so ``bash`` saw
      ``safe_mode=False`` and no confirmation even when the parent had them
      set (the old ``# noqa: ARG001 — propagated via context`` claim was
      false).
    - ``plugin_ui`` — so plugin tools inside a sub-agent work.

    The sub-agent *parameters* (``chat``, ``available_tools``, ...) are
    deliberately NOT forwarded: nested sub-agents are a separate concern
    (#184) and must not silently reuse the parent's chat client or tool
    registry.
    """
    ctx = AgentContext()
    ctx.extra["safe_mode"] = safe_mode
    ctx.extra["bash_confirmation"] = bash_confirmation
    if plugin_ui is not None:
        ctx.extra["plugin_ui"] = plugin_ui
    return ctx


def _aggregate_tokens(steps: list) -> tuple[int, int]:
    """Aggregate input/output tokens from RunSteps."""
    input_tokens = 0
    output_tokens = 0
    for step in steps:
        if step.usage:
            input_tokens += step.usage.input
            output_tokens += step.usage.output
    return input_tokens, output_tokens


def _progress_notify(
    on_progress: Any, tracker: "SubagentProgressTracker | None"
) -> None:
    """Tell the UI the tracker for the active sub-agent call (E2).

    ``on_progress`` is the sink callback injected through the engine
    context (``None`` in front ends without a live panel). Called with
    the fresh tracker when a call starts and with ``None`` when it ends,
    so the panel always renders the metrics of the *current* call only.
    """
    if on_progress is None:
        return
    try:
        on_progress(tracker)
    except Exception:  # noqa: BLE001 — UI plumbing never breaks the run
        _LOGGER.debug("subagent progress notify failed", exc_info=True)


class SubagentProgressTracker:
    """Collects live sub-agent metrics for the running panel (E2).

    One tracker per sub-agent *tool call*: the ``agent``/``agents``
    tools create a fresh instance, register their tasks, and push it to
    the front end through the injected ``on_subagent_progress``
    callback (see :func:`_progress_notify`). It is a plain,
    engine-agnostic bag of per-task
    :class:`~phoson_cli.tools.subagent_panel.SubagentProgress` — no
    Rich, no prompt_toolkit — which the front ends render from however
    they like (fullscreen sink, classic ``SubagentSpinner``, ...).

    Usage:

    1. ``tracker.register(task)`` / :meth:`register_many` → tasks join
       the batch as *queued* rows (index = position in the batch);
    2. ``tracker.start(index)`` when a task actually begins (starts
       its clock);
    3. feed the inner run's ``AgentStepDoneEvent`` stream through
       :meth:`update_from_step`;
    4. :meth:`finalize` on success, :meth:`mark_error` on
       timeout/error/cancellation.

    All methods are safe to call from the single asyncio event loop the
    tools run on; no locks are needed.
    """

    def __init__(self) -> None:
        self.tasks: list[SubagentProgress] = []
        # Index (position in ``tasks``) → per-task running totals.
        self._input: dict[int, int] = {}
        self._output: dict[int, int] = {}
        self._cost: dict[int, float] = {}

    def register(self, task: str) -> int:
        """Record a task as *queued*; returns its index for this batch.

        Queued tasks (``started_at == 0``) render as "waiting" in the
        Time column until :meth:`start` fires — the parallel ``agents``
        tool registers every task up front and starts each one when it
        actually acquires the parallelism slot, so a queued row never
        pretends to be running.
        """
        index = len(self.tasks)
        self.tasks.append(
            SubagentProgress(
                index=index,
                task=task,
                status=AgentStatus.RUNNING,
            )
        )
        return index

    def register_many(self, tasks: list[str]) -> list[int]:
        """Record several tasks at once; returns their indexes in order.

        Used by the parallel ``agents`` tool so all rows of a batch exist
        in the panel before any of them starts running (a task waiting on
        the parallelism semaphore still shows a live row).
        """
        return [self.register(task) for task in tasks]

    def start(self, index: int) -> None:
        """Mark a queued task as actually running (starts its clock)."""
        if 0 <= index < len(self.tasks) and not self.tasks[index].started_at:
            now = time.monotonic()
            self.tasks[index].started_at = now
            self.tasks[index].last_update = now

    def mark_error(self, index: int, error: str | None = None) -> None:
        """Mark a task as failed (timeout / error / cancellation)."""
        if 0 <= index < len(self.tasks):
            self.tasks[index].status = AgentStatus.ERROR
            self.tasks[index].last_update = time.monotonic()

    def update_from_step(self, index: int, step: Any) -> None:
        """Fold one inner-run ``RunStep`` into the task's live totals.

        Only LLM steps carry usage/cost; tool steps are ignored. The
        live panel shows the same USD cost the summary panel computes,
        so the two stay consistent by construction.
        """
        if step is None or not (0 <= index < len(self.tasks)):
            return
        if getattr(step, "kind", None) != "llm":
            return
        progress = self.tasks[index]
        usage = getattr(step, "usage", None)
        if usage is not None:
            self._input[index] = self._input.get(index, 0) + int(
                getattr(usage, "input", 0) or 0
            )
            self._output[index] = self._output.get(index, 0) + int(
                getattr(usage, "output", 0) or 0
            )
        cost = float(getattr(step, "cost_usd", 0.0) or 0.0)
        self._cost[index] = self._cost.get(index, 0.0) + cost
        progress.input_tokens = self._input.get(index, 0)
        progress.output_tokens = self._output.get(index, 0)
        progress.cost_usd = self._cost[index]
        progress.last_update = time.monotonic()

    def finalize(
        self,
        index: int,
        *,
        duration_ms: int | None = None,
        result: AgentRunResult | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Mark a task done and snap its final metrics.

        Final values come from ``result`` when available (the
        sequential path), or from the explicit ``*_tokens`` / ``cost_usd``
        keyword arguments (the parallel path, which aggregates the same
        values into its own payload). When neither is given, whatever
        accumulated while running stays.
        """
        if not 0 <= index < len(self.tasks):
            return
        progress = self.tasks[index]
        progress.status = AgentStatus.DONE
        progress.done = True
        if result is not None:
            tokens_in, tokens_out = _aggregate_tokens(result.steps)
            progress.input_tokens = tokens_in
            progress.output_tokens = tokens_out
            progress.cost_usd = result.total_cost_usd
        else:
            if input_tokens is not None:
                progress.input_tokens = input_tokens
            if output_tokens is not None:
                progress.output_tokens = output_tokens
            if cost_usd is not None:
                progress.cost_usd = cost_usd
        if duration_ms is not None:
            progress.last_update = progress.started_at + max(0, duration_ms) / 1000.0
        else:
            progress.last_update = time.monotonic()


def _select_tools(
    available_tools: dict[str, AgentTool],
    requested: list[str] | None,
) -> tuple[dict[str, AgentTool], str | None]:
    """Resolve the tool subset for a sub-agent.

    Returns a ``(selected, error)`` pair. ``error`` is non-None when the
    request cannot be satisfied; in that case the caller should short-
    circuit and surface the error to the parent agent.
    """
    allowed = {k: v for k, v in available_tools.items() if k != "agent"}
    if requested is None:
        if not allowed:
            return ({}, "Error: No tools available for sub-agent.")
        return (allowed, None)

    selected = {name: t for name, t in allowed.items() if name in requested}
    missing = set(requested) - set(allowed)
    if missing:
        return ({}, f"Error: Tools not found: {missing}")
    if not selected:
        return ({}, "Error: No tools available for sub-agent.")
    return (selected, None)


# AgentErrorEvent codes that unambiguously mean "this model is not
# available" (as opposed to auth, rate limit, context overflow, etc.).
_MODEL_UNAVAILABLE_CODES = frozenset(
    {
        "model_not_found",
        "not_found",
        "invalid_model",
        "unsupported_model",
        "deprecated",
        "no_endpoints",
    }
)

_MESSAGE_MARKERS = (
    "404",
    "no endpoints found",
    "not a valid model",
    "model not found",
    "does not exist",
    "is not found",
    "deprecat",
    "unsupported model",
    "invalid model",
    "unknown model",
)


def _is_model_unavailable_error(exc: BaseException) -> bool:
    """Decide whether ``exc`` means "model not available".

    Recognizes :class:`PhosonProviderError` by its ``code``/``status_code``
    attributes first (the structured, reliable path), then falls back to a
    message heuristic for provider SDKs that raise bare exceptions.
    Deliberately excludes auth (401/403) and rate-limit (429) errors —
    falling back would not help there and could mask real problems.
    """
    if isinstance(exc, PhosonProviderError):
        if exc.status_code in {400, 404, 410}:
            return True
        if exc.code in _MODEL_UNAVAILABLE_CODES:
            return True
    else:
        # Bare exception carrying an attached status (some SDKs).
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status in {400, 404, 410}:
            return True

    text = str(exc).lower()
    return any(marker in text for marker in _MESSAGE_MARKERS)


def _error_event_is_model_unavailable(event: AgentErrorEvent) -> bool:
    """Same classification for the terminal ``AgentErrorEvent`` path.

    The engine surfaces provider failures as an ``AgentErrorEvent`` whose
    ``message`` is the wrapped error string and whose ``code`` mirrors the
    LLM-level error code — so both are checked here.
    """
    if event.code in _MODEL_UNAVAILABLE_CODES:
        return True
    text = (event.message or "").lower()
    return any(marker in text for marker in _MESSAGE_MARKERS)


async def _stream_final(
    engine: AgentEngine,
    messages: list[Message],
    config: ModelConfig,
    *,
    on_event: Callable[[Any], None] | None = None,
) -> AgentRunResult:
    """Drive ``engine.stream()`` and return the terminal outcome.

    ``on_event`` (optional) is called synchronously for every event as
    it is emitted — that is how the live-metrics path (E2) folds the
    inner run's ``AgentStepDoneEvent`` steps into the running panel
    *while the sub-agent is still executing*, not just at the end.

    Returns the full ``AgentRunResult`` on success. Raises on failure:
    re-raises the underlying provider exception when one propagated
    (the reliable, structured path), or raises a
    :class:`PhosonAgentError` carrying the terminal event's
    ``code``/``message`` when the stream ended in an
    ``AgentErrorEvent`` — so callers can classify either shape.
    """
    terminal_error: AgentErrorEvent | None = None
    try:
        async for event in engine.stream(messages, config):
            if on_event is not None:
                on_event(event)
            if isinstance(event, AgentDoneEvent):
                return event.result
            if isinstance(event, AgentErrorEvent):
                terminal_error = event
    except asyncio.CancelledError:
        raise
    except (PhosonProviderError, Exception) as exc:
        # Provider exceptions propagate with their structured attributes
        # (status_code / code) intact — the best classification signal.
        _LOGGER.debug("Sub-agent stream raised: %s", exc, exc_info=True)
        raise

    if terminal_error is not None:
        raise PhosonAgentError(
            f"Agent error ({terminal_error.code}): {terminal_error.message}"
        )
    raise RuntimeError("Sub-agent stream finished without a terminal event.")


def _is_model_unavailable_failure(exc: BaseException) -> bool:
    """Classify any failure raised by ``_stream_final``."""
    if isinstance(exc, PhosonMaxIterationsError):
        return False
    if isinstance(exc, PhosonProviderError):
        return _is_model_unavailable_error(exc)
    if isinstance(exc, PhosonAgentError):
        # Reconstruct what the terminal event looked like so the code /
        # message heuristics apply to it directly.
        synthetic = AgentErrorEvent(message=str(exc))
        return _error_event_is_model_unavailable(synthetic)
    return _is_model_unavailable_error(exc)


async def _run_one_subagent(
    *,
    task: str,
    chat: BaseLLMChat,
    selected_tools: list[AgentTool],
    model: str,
    max_iterations: int,
    timeout_seconds: float | None = None,
    fallback_model: str | None = None,
    progress: SubagentProgressTracker | None = None,
    index: int = 0,
    middlewares: list[AgentMiddleware] | None = None,
    safe_mode: bool = False,
    bash_confirmation: Any = None,
    plugin_ui: Any = None,
) -> tuple[str, str | None]:
    """Run a single sub-agent; return ``(final_content, fallback_used)``.

    ``fallback_used`` is the fallback model name when the configured
    ``model`` was unavailable and the task completed on ``fallback_model``
    (the main agent's model — known to work); ``None`` otherwise.

    The sub-engine is built with the parent's **middleware chain**
    (``middlewares``, #174/F-01) and a fresh context carrying the runtime
    flags (``safe_mode``, ``bash_confirmation``, ``plugin_ui``) so the
    permission gate, safe-mode bash and plugin tools apply to sub-agent
    calls exactly as they do to the parent's.

    When ``progress`` is given (E2), the inner run's LLM steps are
    folded into ``progress`` as they complete so the parent's live
    panel shows tokens/cost in real time, not only at the end.
    """

    def _on_event(event: Any) -> None:
        if progress is not None and isinstance(event, AgentStepDoneEvent):
            progress.update_from_step(index, event.step)

    async def _run(model_name: str) -> AgentRunResult:
        sub_engine = AgentEngine(
            chat=_clone_chat(chat),
            tools=selected_tools,
            middlewares=list(middlewares) if middlewares else [],
            context=_subagent_context(
                safe_mode=safe_mode,
                bash_confirmation=bash_confirmation,
                plugin_ui=plugin_ui,
            ),
            max_iterations=max_iterations,
        )
        messages = [Message(role="user", content=task)]
        return await _stream_final(
            sub_engine, messages, ModelConfig(model=model_name), on_event=_on_event
        )

    async def _attempt(model_name: str) -> AgentRunResult:
        if timeout_seconds is not None and timeout_seconds > 0:
            return await asyncio.wait_for(_run(model_name), timeout=timeout_seconds)
        return await _run(model_name)

    try:
        result = await _attempt(model)
        if progress is not None:
            progress.finalize(
                index,
                duration_ms=int(sum(s.duration_ms for s in result.steps)),
                result=result,
            )
        return result.final_content, None
    except TimeoutError:
        _LOGGER.debug("Sub-agent timed out after %.0fs: %s", timeout_seconds, task[:80])
        if progress is not None:
            progress.mark_error(index, "timeout")
        return f"Sub-agent timed out after {timeout_seconds:.0f}s.", None
    except asyncio.CancelledError:
        if progress is not None:
            progress.mark_error(index, "cancelled")
        raise
    except Exception as exc:
        if (
            fallback_model is None
            or fallback_model == model
            or not _is_model_unavailable_failure(exc)
        ):
            _LOGGER.debug("Sub-agent raised: %s", exc, exc_info=True)
            if progress is not None:
                progress.mark_error(index, str(exc))
            return f"Sub-agent error: {exc}", None

        # Debug, not warning: the fallback is already surfaced to the user
        # in the UI (panel caption / [fallback to ...] note). A warning
        # here leaks the raw provider error to stderr via the logging
        # last-resort handler and mangles the full-screen TUI.
        _LOGGER.debug(
            "Sub-agent model %s unavailable (%s) — falling back to %s",
            model,
            exc,
            fallback_model,
        )
        try:
            result = await _attempt(fallback_model)
            if progress is not None:
                progress.finalize(index, result=result)
            return result.final_content, fallback_model
        except TimeoutError:
            _LOGGER.debug(
                "Sub-agent timed out after %.0fs: %s", timeout_seconds, task[:80]
            )
            if progress is not None:
                progress.mark_error(index, "timeout")
            return f"Sub-agent timed out after {timeout_seconds:.0f}s.", None
        except Exception as fallback_exc:
            _LOGGER.debug("Sub-agent fallback raised: %s", fallback_exc, exc_info=True)
            if progress is not None:
                progress.mark_error(index, str(fallback_exc))
            return f"Sub-agent error: {fallback_exc}", None


# Sub-agent tool injection parameters (per tool: `agents` also gets the
# parallelism limit; the single `agent` tool only needs the timeout).
# ``on_subagent_progress`` feeds the live metrics panel (E2) —
# optional on the engine side, so pre-E2 callers keep working.
_AGENT_INJECT = [
    "chat",
    "available_tools",
    "default_model",
    "main_model",
    "max_iterations",
    "safe_mode",
    "subagent_timeout_seconds",
    "on_subagent_progress",
    # #174/F-01: the parent's middleware gate (permission, at minimum) is
    # handed to each sub-engine so a `deny`-level tool is refused from a
    # sub-agent exactly as it is from the parent, and the runtime
    # `bash_confirmation` / `plugin_ui` services are forwarded so `bash`
    # (safe_mode) and plugin tools behave identically to the parent's.
    "middlewares",
    "bash_confirmation",
    "plugin_ui",
]
_AGENTS_INJECT = _AGENT_INJECT + ["subagent_max_parallel"]

#: Description the LLM sees for the ``timeout`` parameter (the @tool schema
#: is built from the annotations, not the docstring).
SUBAGENT_TIMEOUT_DESCRIPTION = (
    "Optional hard timeout in seconds for the sub-agent run. Omit to use "
    "the configured default (300s). Raise it for long-running tasks (no "
    "maximum). 0 disables the timeout entirely."
)


def _resolve_subagent_timeout(
    timeout: float | None, configured_default: float
) -> tuple[float, str | None]:
    """Resolve the effective per-invocation sub-agent timeout.

    ``None`` (omitted by the model) keeps the configured default from
    ``config.toml``/env — backward compatible. ``0`` is a valid override:
    it disables the timeout entirely (the pre-existing config semantics
    of ``subagent_timeout_seconds = 0``). Other invalid values fall back
    to the configured default with a note for the model.
    """
    if timeout is None:
        return configured_default, None
    return sanitize_timeout(timeout, configured_default, allow_zero=True)


@tool(inject=_AGENT_INJECT)
async def agent(
    task: str,
    tools: list[str] | None = None,
    model: str | None = None,
    timeout: Annotated[float | None, SUBAGENT_TIMEOUT_DESCRIPTION] = None,
    *,
    chat: BaseLLMChat,
    available_tools: dict[str, AgentTool],
    default_model: str,
    main_model: str | None = None,
    max_iterations: int,
    safe_mode: bool = False,
    subagent_timeout_seconds: float = 300.0,
    on_subagent_progress: Any = None,
    middlewares: list[AgentMiddleware] | None = None,
    bash_confirmation: Any = None,
    plugin_ui: Any = None,
) -> str:
    """Delegate a self-contained task to a fresh sub-agent (clean context).

    Use for work that would otherwise bloat the main context or benefits
    from an isolated scratchpad: large explorations (find and read many
    files), a well-bounded subtask with a clear deliverable, or anything
    that can be stated in one prompt and judged from its final answer. The
    sub-agent gets a fresh context, inherits your permission gate (deny/ask
    levels apply the same), and returns only its final answer — so it is a
    good fit for "go look into X and report back", not for steps that need
    to share intermediate state with the main loop. Do not use it for a
    single quick edit or read (do that directly); parallel independent
    tasks belong in the agents tool instead.

    Args:
        task: The task to run in a sub-agent with clean context.
        tools: Optional subset of tool names to grant the sub-agent.
        model: Optional model override for the sub-agent.
        timeout: Optional hard timeout in seconds for the run. Omit to
            use the configured default (``subagent_timeout_seconds``);
            ``0`` disables the timeout; no upper bound.
    """
    selected, err = _select_tools(available_tools, tools)
    if err is not None:
        return err

    effective_timeout, timeout_note = _resolve_subagent_timeout(
        timeout, subagent_timeout_seconds
    )

    # Live-metrics panel (E2): every call owns a fresh tracker — a run
    # may call this tool several times, and the panel must always show
    # the metrics of the *current* call only.
    tracker = SubagentProgressTracker()
    index = tracker.register(task)
    tracker.start(index)
    _progress_notify(on_subagent_progress, tracker)
    try:
        content, fallback_used = await _run_one_subagent(
            task=task,
            chat=chat,
            selected_tools=list(selected.values()),
            model=model or default_model,
            max_iterations=max_iterations,
            timeout_seconds=effective_timeout,
            fallback_model=main_model,
            progress=tracker,
            index=index,
            middlewares=middlewares,
            safe_mode=safe_mode,
            bash_confirmation=bash_confirmation,
            plugin_ui=plugin_ui,
        )
    finally:
        _progress_notify(on_subagent_progress, None)
    if fallback_used:
        content = f"[fallback to {fallback_used}] {content}"
    if timeout_note:
        return f"{timeout_note}\n{content}"
    return content


@tool(inject=_AGENTS_INJECT)
async def agents(
    tasks: list[str],
    tools: list[str] | None = None,
    model: str | None = None,
    timeout: Annotated[float | None, SUBAGENT_TIMEOUT_DESCRIPTION] = None,
    *,
    chat: BaseLLMChat,
    available_tools: dict[str, AgentTool],
    default_model: str,
    main_model: str | None = None,
    max_iterations: int,
    safe_mode: bool = False,
    subagent_max_parallel: int = 4,
    subagent_timeout_seconds: float = 300.0,
    on_subagent_progress: Any = None,
    middlewares: list[AgentMiddleware] | None = None,
    bash_confirmation: Any = None,
    plugin_ui: Any = None,
) -> str:
    """Delegate several independent tasks to parallel sub-agents.

    Use when you have multiple *independent* subtasks that can run at the
    same time (research different areas, fix unrelated files, prepare
    several deliverables). Each task gets its own fresh-context sub-agent
    that inherits your permission gate, and the results are returned
    together. Tasks in one call must not depend on each other's output —
    if they do, run them sequentially (or in one agent) instead, since a
    later task cannot see an earlier one's result within the same batch.

    Args:
        tasks: The tasks to run in parallel sub-agents.
        tools: Optional subset of tool names to grant the sub-agents.
        model: Optional model override for the sub-agents.
        timeout: Optional hard timeout in seconds applied to **every**
            task in the batch. Omit to use the configured default
            (``subagent_timeout_seconds``); ``0`` disables the timeout;
            no upper bound.
    """
    if not tasks:
        return "Error: No tasks provided."

    selected, err = _select_tools(available_tools, tools)
    if err is not None:
        return err

    effective_timeout, timeout_note = _resolve_subagent_timeout(
        timeout, subagent_timeout_seconds
    )

    effective_model = model or default_model
    selected_tools_list = list(selected.values())
    # Bound the concurrency: the parent agent decides how many tasks to
    # spawn, not how many LLM sessions may run at once.
    semaphore = asyncio.Semaphore(max(1, subagent_max_parallel))

    # Live-metrics panel (E2): every call owns a fresh tracker. All
    # tasks are registered up front (queued rows) so the panel shows
    # every row immediately; each one starts its clock when it actually
    # acquires the parallelism slot.
    tracker = SubagentProgressTracker()
    live_indexes = tracker.register_many(tasks)
    _progress_notify(on_subagent_progress, tracker)

    async def run_one(idx: int, task: str) -> dict[str, Any]:
        preview = task[:40] + "..." if len(task) > 40 else task
        live_index = live_indexes[idx]
        async with semaphore:
            tracker.start(live_index)

            def _on_event(event: Any) -> None:
                if isinstance(event, AgentStepDoneEvent):
                    tracker.update_from_step(live_index, event.step)

            async def _attempt(model_name: str) -> dict[str, Any]:
                sub_engine = AgentEngine(
                    chat=_clone_chat(chat),
                    tools=selected_tools_list,
                    middlewares=list(middlewares) if middlewares else [],
                    context=_subagent_context(
                        safe_mode=safe_mode,
                        bash_confirmation=bash_confirmation,
                        plugin_ui=plugin_ui,
                    ),
                    max_iterations=max_iterations,
                )
                messages = [Message(role="user", content=task)]
                config = ModelConfig(model=model_name)
                if effective_timeout > 0:
                    result = await asyncio.wait_for(
                        _stream_final(sub_engine, messages, config, on_event=_on_event),
                        timeout=effective_timeout,
                    )
                else:
                    result = await _stream_final(
                        sub_engine, messages, config, on_event=_on_event
                    )
                input_tokens, output_tokens = _aggregate_tokens(result.steps)
                return {
                    "index": idx,
                    "task": task,
                    "task_preview": preview,
                    "result": result.final_content,
                    "cost_usd": result.total_cost_usd,
                    "credits": result.total_credits,
                    "duration_ms": sum(s.duration_ms for s in result.steps),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "fallback_model": None,
                }

            try:
                payload = await _attempt(effective_model)
                tracker.finalize(
                    live_index,
                    duration_ms=int(payload["duration_ms"]),
                    input_tokens=int(payload["input_tokens"]),
                    output_tokens=int(payload["output_tokens"]),
                    cost_usd=float(payload["cost_usd"]),
                )
                return payload
            except TimeoutError:
                tracker.mark_error(live_index, "timeout")
                return {
                    "index": idx,
                    "task": task,
                    "task_preview": preview,
                    "result": "",
                    "error": f"timeout after {effective_timeout:g}s",
                }
            except asyncio.CancelledError:
                tracker.mark_error(live_index, "cancelled")
                raise
            except Exception as exc:
                if (
                    main_model is None
                    or main_model == effective_model
                    or not _is_model_unavailable_failure(exc)
                ):
                    _LOGGER.debug(
                        "Parallel sub-agent %d raised: %s", idx, exc, exc_info=True
                    )
                    tracker.mark_error(live_index, str(exc))
                    return {
                        "index": idx,
                        "task": task,
                        "task_preview": preview,
                        "result": f"Error: {exc}",
                        "error": str(exc),
                    }

                # Debug, not warning — see the sequential path above:
                # the UI already surfaces the fallback; a warning here
                # prints the raw provider error over the TUI.
                _LOGGER.debug(
                    "Parallel sub-agent %d: model %s unavailable (%s) — "
                    "falling back to %s",
                    idx,
                    effective_model,
                    exc,
                    main_model,
                )
                try:
                    payload = await _attempt(main_model)
                    payload["fallback_model"] = main_model
                    tracker.finalize(
                        live_index,
                        duration_ms=int(payload["duration_ms"]),
                        input_tokens=int(payload["input_tokens"]),
                        output_tokens=int(payload["output_tokens"]),
                        cost_usd=float(payload["cost_usd"]),
                    )
                    return payload
                except TimeoutError:
                    tracker.mark_error(live_index, "timeout")
                    return {
                        "index": idx,
                        "task": task,
                        "task_preview": preview,
                        "result": "",
                        "error": f"timeout after {effective_timeout:g}s",
                    }
                except Exception as fallback_exc:
                    _LOGGER.debug(
                        "Parallel sub-agent %d fallback raised: %s",
                        idx,
                        fallback_exc,
                        exc_info=True,
                    )
                    tracker.mark_error(live_index, str(fallback_exc))
                    return {
                        "index": idx,
                        "task": task,
                        "task_preview": preview,
                        "result": f"Error: {fallback_exc}",
                        "error": str(fallback_exc),
                    }

    try:
        results: list[dict[str, Any]] = list(
            await asyncio.gather(
                *(run_one(idx, task) for idx, task in enumerate(tasks))
            )
        )
    finally:
        _progress_notify(on_subagent_progress, None)
    results.sort(key=lambda x: x["index"])

    output_parts: list[str] = []
    for r in results:
        idx = r["index"]
        task_preview = r["task_preview"]
        error = r.get("error")
        if error:
            output_parts.append(
                format_agent_block(
                    index=idx,
                    task_preview=task_preview,
                    body="",
                    error=error,
                )
            )
        else:
            metrics_line = format_metrics_line(
                duration_ms=int(r["duration_ms"]),
                input_tokens=int(r["input_tokens"]),
                output_tokens=int(r["output_tokens"]),
                cost_usd=float(r["cost_usd"]),
                credits=r.get("credits", 0),
                fallback_model=r.get("fallback_model"),
            )
            output_parts.append(
                format_agent_block(
                    index=idx,
                    task_preview=task_preview,
                    body=str(r["result"]),
                    metrics_line=metrics_line,
                )
            )

    output = "\n\n".join(output_parts)
    if timeout_note:
        return f"{timeout_note}\n{output}"
    return output
