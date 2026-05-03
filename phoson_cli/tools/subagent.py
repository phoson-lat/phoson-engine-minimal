"""Sub-agent tools for Phoson CLI."""

import os
import asyncio
import logging
from typing import Any

from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import Message, ModelConfig
from phoson_llm.chats.base import BaseLLMChat

from .base import BaseTool

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


class SubAgentTool(BaseTool):
    """Tool to execute sub-agents."""

    def run(self, *args: Any, **kwargs: Any) -> Any:
        # This class acts as a wrapper; actual execution happens in the @tool methods.
        pass

    async def execute_single(
        self,
        task: str,
        tools: list[str] | None,
        model: str | None,
        chat: BaseLLMChat,
        available_tools: dict[str, Any],
        default_model: str,
        max_iterations: int,
    ) -> str:
        """Logic for single sub-agent execution."""
        allowed_tools = {k: v for k, v in available_tools.items() if k != "agent"}
        if tools is not None:
            selected = {
                name: tool for name, tool in allowed_tools.items() if name in tools
            }
            missing = set(tools) - set(allowed_tools)
            if missing:
                return f"Error: Tools not found: {missing}"
        else:
            selected = allowed_tools

        if not selected:
            return "Error: No tools available for sub-agent."

        sub_engine = AgentEngine(
            chat=_clone_chat(chat),
            tools=list(selected.values()),
            max_iterations=max_iterations,
        )

        messages = [Message(role="user", content=task)]
        config = ModelConfig(model=model or default_model)

        try:
            result = await sub_engine.run(messages, config)
            return result.final_content
        except Exception as e:
            return f"Sub-agent error: {e}"


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
    """Execute a task using a sub-agent with clean context."""
    tool_inst = SubAgentTool()
    return await tool_inst.execute_single(
        task, tools, model, chat, available_tools, default_model, max_iterations
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
    safe_mode: bool = False,
) -> str:
    """Execute multiple tasks in parallel using sub-agents."""
    if not tasks:
        return "Error: No tasks provided."

    allowed_tools = {k: v for k, v in available_tools.items() if k != "agent"}
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
        except Exception as e:
            return {
                "index": idx,
                "task": task,
                "task_preview": task[:40] + "..." if len(task) > 40 else task,
                "result": f"Error: {e}",
                "error": str(e),
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
            output_parts.append(f"=== Agent {idx}: {task_preview} === Error: {error}")
        else:
            metrics_line = (
                "--- METRICS: "
                f"duration_ms={r['duration_ms']} "
                f"input_tokens={r['input_tokens']} "
                f"output_tokens={r['output_tokens']} "
                f"cost_usd={r['cost_usd']} "
                f"credits={r['credits']} "
                "---"
            )
            output_parts.append(
                f"=== Agent {idx}: {task_preview} ===\n"
                f"{r['result']}\n"
                f"{metrics_line}"
            )

    return "\n\n".join(output_parts)
