"""Sub-agent tools for Phoson CLI.

These tools spawn fresh ``AgentEngine`` instances to run isolated tasks
either one at a time (``agent``) or in parallel (``agents``). The two
helpers share the same set of injected dependencies (chat client, tool
registry, default model and iteration budget) and emit results as
plain strings so the parent agent can consume them as tool results.
"""

import os
import asyncio
import logging
from typing import Any

from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import Message, ModelConfig
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
    config fields. Cloning the dict preserves those without invoking the
    original constructor (which would re-read env vars and might crash if
    the user had injected a key directly).
    """
    cls = type(chat)
    clone = cls.__new__(cls)
    clone.__dict__ = dict(chat.__dict__)
    return clone


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
    available_tools: dict[str, Any],
    requested: list[str] | None,
) -> tuple[dict[str, Any], str | None]:
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


async def _run_one_subagent(
    *,
    task: str,
    chat: BaseLLMChat,
    selected_tools: list[Any],
    model: str,
    max_iterations: int,
) -> str:
    """Run a single sub-agent and return its final string content."""
    sub_engine = AgentEngine(
        chat=_clone_chat(chat),
        tools=selected_tools,
        max_iterations=max_iterations,
    )
    messages = [Message(role="user", content=task)]
    config = ModelConfig(model=model)
    try:
        result = await sub_engine.run(messages, config)
        return result.final_content
    except Exception as exc:
        return f"Sub-agent error: {exc}"


# Sub-agent tool injection parameters
_SUBAGENT_INJECT = [
    "chat",
    "available_tools",
    "default_model",
    "max_iterations",
    "safe_mode",
]


@tool(inject=_SUBAGENT_INJECT)
async def agent(
    task: str,
    tools: list[str] | None = None,
    model: str | None = None,
    *,
    chat: BaseLLMChat,
    available_tools: dict[str, Any],
    default_model: str,
    max_iterations: int,
    safe_mode: bool = False,  # noqa: ARG001 — propagated via context
) -> str:
    """Execute a task using a sub-agent with clean context."""
    selected, err = _select_tools(available_tools, tools)
    if err is not None:
        return err

    return await _run_one_subagent(
        task=task,
        chat=chat,
        selected_tools=list(selected.values()),
        model=model or default_model,
        max_iterations=max_iterations,
    )


@tool(inject=_SUBAGENT_INJECT)
async def agents(
    tasks: list[str],
    tools: list[str] | None = None,
    model: str | None = None,
    *,
    chat: BaseLLMChat,
    available_tools: dict[str, Any],
    default_model: str,
    max_iterations: int,
    safe_mode: bool = False,  # noqa: ARG001 — propagated via context
) -> str:
    """Execute multiple tasks in parallel using sub-agents."""
    if not tasks:
        return "Error: No tasks provided."

    selected, err = _select_tools(available_tools, tools)
    if err is not None:
        return err

    effective_model = model or default_model
    selected_tools_list = list(selected.values())

    async def run_one(idx: int, task: str) -> dict[str, Any]:
        sub_engine = AgentEngine(
            chat=_clone_chat(chat),
            tools=selected_tools_list,
            max_iterations=max_iterations,
        )
        messages = [Message(role="user", content=task)]
        config = ModelConfig(model=effective_model)

        try:
            result = await sub_engine.run(messages, config)
            input_tokens, output_tokens = _aggregate_tokens(result.steps)
            return {
                "index": idx,
                "task": task,
                "task_preview": task[:40] + "..." if len(task) > 40 else task,
                "result": result.final_content,
                "cost_usd": result.total_cost_usd,
                "credits": result.total_credits,
                "duration_ms": sum(s.duration_ms for s in result.steps),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        except Exception as exc:
            return {
                "index": idx,
                "task": task,
                "task_preview": task[:40] + "..." if len(task) > 40 else task,
                "result": f"Error: {exc}",
                "error": str(exc),
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
