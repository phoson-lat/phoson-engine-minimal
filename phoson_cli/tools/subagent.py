"""Sub-agent tools for Phoson CLI.

These tools spawn fresh ``AgentEngine`` instances to run isolated tasks
either one at a time (``agent``) or in parallel (``agents``). The two
helpers share the same set of injected dependencies (chat client, tool
registry, default model and iteration budget) and emit results as
plain strings so the parent agent can consume them as tool results.
"""

import os
import copy
import asyncio
import logging
from typing import Any

from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_agent.exceptions import PhosonAgentError, PhosonMaxIterationsError
from phoson_llm.exceptions import PhosonProviderError
from phoson_llm.schemas import Message, ModelConfig
from phoson_agent.models import (
    AgentDoneEvent,
    AgentErrorEvent,
    AgentRunResult,
    AgentTool,
)
from phoson_llm.chats.base import BaseLLMChat

from .subagent_panel import format_agent_block, format_metrics_line

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


def _aggregate_tokens(steps: list) -> tuple[int, int]:
    """Aggregate input/output tokens from RunSteps."""
    input_tokens = 0
    output_tokens = 0
    for step in steps:
        if step.usage:
            input_tokens += step.usage.input
            output_tokens += step.usage.output
    return input_tokens, output_tokens


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
) -> AgentRunResult:
    """Drive ``engine.stream()`` and return the terminal outcome.

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
) -> tuple[str, str | None]:
    """Run a single sub-agent; return ``(final_content, fallback_used)``.

    ``fallback_used`` is the fallback model name when the configured
    ``model`` was unavailable and the task completed on ``fallback_model``
    (the main agent's model — known to work); ``None`` otherwise.
    """

    async def _run(model_name: str) -> AgentRunResult:
        sub_engine = AgentEngine(
            chat=_clone_chat(chat),
            tools=selected_tools,
            max_iterations=max_iterations,
        )
        messages = [Message(role="user", content=task)]
        return await _stream_final(sub_engine, messages, ModelConfig(model=model_name))

    async def _attempt(model_name: str) -> AgentRunResult:
        if timeout_seconds is not None and timeout_seconds > 0:
            return await asyncio.wait_for(_run(model_name), timeout=timeout_seconds)
        return await _run(model_name)

    try:
        result = await _attempt(model)
        return result.final_content, None
    except TimeoutError:
        _LOGGER.debug("Sub-agent timed out after %.0fs: %s", timeout_seconds, task[:80])
        return f"Sub-agent timed out after {timeout_seconds:.0f}s.", None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if (
            fallback_model is None
            or fallback_model == model
            or not _is_model_unavailable_failure(exc)
        ):
            _LOGGER.debug("Sub-agent raised: %s", exc, exc_info=True)
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
            return result.final_content, fallback_model
        except TimeoutError:
            _LOGGER.debug(
                "Sub-agent timed out after %.0fs: %s", timeout_seconds, task[:80]
            )
            return f"Sub-agent timed out after {timeout_seconds:.0f}s.", None
        except Exception as fallback_exc:
            _LOGGER.debug("Sub-agent fallback raised: %s", fallback_exc, exc_info=True)
            return f"Sub-agent error: {fallback_exc}", None


# Sub-agent tool injection parameters (per tool: `agents` also gets the
# parallelism limit; the single `agent` tool only needs the timeout).
_AGENT_INJECT = [
    "chat",
    "available_tools",
    "default_model",
    "main_model",
    "max_iterations",
    "safe_mode",
    "subagent_timeout_seconds",
]
_AGENTS_INJECT = _AGENT_INJECT + ["subagent_max_parallel"]


@tool(inject=_AGENT_INJECT)
async def agent(
    task: str,
    tools: list[str] | None = None,
    model: str | None = None,
    *,
    chat: BaseLLMChat,
    available_tools: dict[str, AgentTool],
    default_model: str,
    main_model: str | None = None,
    max_iterations: int,
    safe_mode: bool = False,  # noqa: ARG001 — propagated via context
    subagent_timeout_seconds: float = 300.0,
) -> str:
    """Execute a task using a sub-agent with clean context."""
    selected, err = _select_tools(available_tools, tools)
    if err is not None:
        return err

    content, fallback_used = await _run_one_subagent(
        task=task,
        chat=chat,
        selected_tools=list(selected.values()),
        model=model or default_model,
        max_iterations=max_iterations,
        timeout_seconds=subagent_timeout_seconds,
        fallback_model=main_model,
    )
    if fallback_used:
        return f"[fallback to {fallback_used}] {content}"
    return content


@tool(inject=_AGENTS_INJECT)
async def agents(
    tasks: list[str],
    tools: list[str] | None = None,
    model: str | None = None,
    *,
    chat: BaseLLMChat,
    available_tools: dict[str, AgentTool],
    default_model: str,
    main_model: str | None = None,
    max_iterations: int,
    safe_mode: bool = False,  # noqa: ARG001 — propagated via context
    subagent_max_parallel: int = 4,
    subagent_timeout_seconds: float = 300.0,
) -> str:
    """Execute multiple tasks in parallel using sub-agents."""
    if not tasks:
        return "Error: No tasks provided."

    selected, err = _select_tools(available_tools, tools)
    if err is not None:
        return err

    effective_model = model or default_model
    selected_tools_list = list(selected.values())
    # Bound the concurrency: the parent agent decides how many tasks to
    # spawn, not how many LLM sessions may run at once.
    semaphore = asyncio.Semaphore(max(1, subagent_max_parallel))

    async def run_one(idx: int, task: str) -> dict[str, Any]:
        preview = task[:40] + "..." if len(task) > 40 else task
        async with semaphore:

            async def _attempt(model_name: str) -> dict[str, Any]:
                sub_engine = AgentEngine(
                    chat=_clone_chat(chat),
                    tools=selected_tools_list,
                    max_iterations=max_iterations,
                )
                messages = [Message(role="user", content=task)]
                config = ModelConfig(model=model_name)
                if subagent_timeout_seconds > 0:
                    result = await asyncio.wait_for(
                        _stream_final(sub_engine, messages, config),
                        timeout=subagent_timeout_seconds,
                    )
                else:
                    result = await _stream_final(sub_engine, messages, config)
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
                return await _attempt(effective_model)
            except TimeoutError:
                return {
                    "index": idx,
                    "task": task,
                    "task_preview": preview,
                    "result": "",
                    "error": f"timeout after {subagent_timeout_seconds:g}s",
                }
            except asyncio.CancelledError:
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
                    return payload
                except TimeoutError:
                    return {
                        "index": idx,
                        "task": task,
                        "task_preview": preview,
                        "result": "",
                        "error": f"timeout after {subagent_timeout_seconds:g}s",
                    }
                except Exception as fallback_exc:
                    _LOGGER.debug(
                        "Parallel sub-agent %d fallback raised: %s",
                        idx,
                        fallback_exc,
                        exc_info=True,
                    )
                    return {
                        "index": idx,
                        "task": task,
                        "task_preview": preview,
                        "result": f"Error: {fallback_exc}",
                        "error": str(fallback_exc),
                    }

    results: list[dict[str, Any]] = list(
        await asyncio.gather(*(run_one(idx, task) for idx, task in enumerate(tasks)))
    )
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

    return "\n\n".join(output_parts)
