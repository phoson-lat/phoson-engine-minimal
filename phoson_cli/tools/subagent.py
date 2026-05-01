"""Sub-agent tools for Phoson CLI."""
import asyncio
import logging
import os
from typing import Any

from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.schemas import Message, ModelConfig

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


def _build_subagent_output(idx: int, task: str, result: str) -> dict[str, Any]:
    """Build structured output with metrics for the renderer."""
    task_preview = task[:40] + "..." if len(task) > 40 else task
    return {
        "index": idx,
        "task": task,
        "task_preview": task_preview,
        "result": result,
    }


def _clone_chat(chat: BaseLLMChat) -> BaseLLMChat:
    cls = type(chat)
    clone = cls.__new__(cls)
    clone.__dict__ = dict(chat.__dict__)
    _log_debug(
        "cloned chat instance",
        chat_type=cls.__name__,
        source_id=id(chat),
        clone_id=id(clone),
    )
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
    safe_mode: bool = False,
) -> str:
    """Execute a task using a sub-agent with clean context.

    Args:
        task: The task/prompt for the sub-agent to execute.
        tools: Optional list of tool names to enable. If None, uses all except 'agent'.
        model: Optional model override (default: from config).

    The sub-agent runs with a clean context - only the task is passed.
    The sub-agent cannot spawn other sub-agents (no recursion).
    """
    # Filter out 'agent' tool to prevent recursion
    allowed_tools = {k: v for k, v in available_tools.items() if k != "agent"}

    # Select tools
    if tools is not None:
        selected = {name: tool for name, tool in allowed_tools.items() if name in tools}
        missing = set(tools) - set(allowed_tools)
        if missing:
            return f"Error: Tools not found: {missing}"
    else:
        selected = allowed_tools

    if not selected:
        return "Error: No tools available for sub-agent."

    # Create sub-agent engine
    _log_debug(
        "starting single subagent",
        task_preview=task[:80],
        requested_tools=tools,
        model=model or default_model,
    )
    sub_engine = AgentEngine(
        chat=_clone_chat(chat),
        tools=list(selected.values()),
        max_iterations=max_iterations,
    )

    # Execute with clean context
    messages = [Message(role="user", content=task)]
    config = ModelConfig(model=model or default_model)

    try:
        result = await sub_engine.run(messages, config)

        # Build output with metrics (renderer will extract these)
        input_tokens, output_tokens = _aggregate_tokens(result.steps)
        _log_debug(
            "single subagent finished",
            task_preview=task[:80],
            cost=result.total_cost_usd,
            tokens=input_tokens + output_tokens,
        )
        return result.final_content
    except Exception as e:
        _log_debug(
            "single subagent failed",
            task_preview=task[:80],
            error=str(e),
        )
        return f"Sub-agent error: {e}"


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
    safe_mode: bool = False,
) -> str:
    """Execute multiple tasks in parallel using sub-agents.

    Args:
        tasks: List of tasks/prompts to execute in parallel.
        tools: Optional list of tool names to enable for each sub-agent.
        model: Optional model override (default: from config).

    Each sub-agent runs independently with clean context.
    Results are returned with task index and result for each.
    """
    if not tasks:
        return "Error: No tasks provided."

    # Filter out 'agent' tool to prevent recursion
    allowed_tools = {k: v for k, v in available_tools.items() if k != "agent"}

    # Select tools
    if tools is not None:
        selected = {name: tool for name, tool in allowed_tools.items() if name in tools}
        missing = set(tools) - set(allowed_tools)
        if missing:
            return f"Error: Tools not found: {missing}"
    else:
        selected = allowed_tools

    if not selected:
        return "Error: No tools available for sub-agents."

    effective_model = model or default_model
    selected_tools_list = list(selected.values())

    _log_debug(
        "starting parallel subagents",
        task_count=len(tasks),
        requested_tools=tools,
        model=effective_model,
    )

    async def run_one(idx: int, task: str) -> dict[str, Any]:
        _log_debug(
            "subagent task prepared",
            idx=idx,
            task_preview=task[:80],
        )
        sub_engine = AgentEngine(
            chat=_clone_chat(chat),
            tools=selected_tools_list,
            max_iterations=max_iterations,
        )
        messages = [Message(role="user", content=task)]
        config = ModelConfig(model=effective_model)

        try:
            _log_debug(
                "subagent task running",
                idx=idx,
                task_preview=task[:80],
                engine_id=id(sub_engine),
                chat_type=type(sub_engine.chat).__name__,
            )
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
        except Exception as e:
            _log_debug(
                "subagent task failed",
                idx=idx,
                task_preview=task[:80],
                engine_id=id(sub_engine),
                error=str(e),
            )
            return {
                "index": idx,
                "task": task,
                "task_preview": task[:40] + "..." if len(task) > 40 else task,
                "result": f"Error: {e}",
                "error": str(e),
                "cost_usd": 0.0,
                "credits": 0.0,
                "duration_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }

    results: list[dict[str, Any]] = list(
        await asyncio.gather(*(run_one(idx, task) for idx, task in enumerate(tasks)))
    )

    _log_debug("parallel subagents gathered", task_count=len(results))

    results.sort(key=lambda x: x["index"])

    # Build output with all metrics (renderer will format this)
    output_parts: list[str] = []

    total_cost = 0.0
    total_credits = 0.0
    total_duration = 0
    total_input = 0
    total_output = 0

    for r in results:
        total_cost += r.get("cost_usd", 0.0)
        total_credits += r.get("credits", 0.0)
        total_duration += r.get("duration_ms", 0)
        total_input += r.get("input_tokens", 0)
        total_output += r.get("output_tokens", 0)

    # Format: each agent result + summary
    for r in results:
        idx = r["index"]
        task_preview = r["task_preview"]
        cost = r.get("cost_usd", 0.0)
        input_tok = r.get("input_tokens", 0)
        output_tok = r.get("output_tokens", 0)
        duration = r.get("duration_ms", 0)
        error = r.get("error")

        if error:
            output_parts.append(f"=== Agent {idx}: {task_preview} === Error: {error}")
        else:
            metrics_line = f"--- METRICS: {duration}ms | {input_tok}in/{output_tok}out | ${cost:.5f} ---"
            output_parts.append(
                f"=== Agent {idx}: {task_preview} ===\n"
                f"{r['result']}\n"
                f"{metrics_line}"
            )

    output_parts.append(
        f"=== SUMMARY ===\n"
        f"Total: {len(tasks)} agents | {total_duration}ms | {total_input}in/{total_output}out | ${total_cost:.5f}"
    )

    return "\n\n".join(output_parts)
