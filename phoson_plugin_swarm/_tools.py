"""The ``swarm_*`` tool handlers exposed by the plugin.

Each handler is declared with :func:`phoson_agent.tool.tool` and an ``inject``
list: the host (the CLI, or any embedded engine) puts the runtime bindings —
``chat``, ``available_tools``, the default model, the middleware gate and the
``safe_mode``/``bash_confirmation``/``plugin_ui`` services — into the shared
:class:`phoson_agent.context.AgentContext`, and :func:`tool` forwards the
matching values to the handler as keyword arguments. This is *exactly* the
mechanism the built-in ``agent``/``agents`` sub-agent tools use, so a swarm
member engine is built the same way the CLI builds its sub-agents.

All injected parameters default to "absent" so the plugin degrades to a clear,
actionable error when it is loaded into a host that does not inject a chat
client (e.g. an engine-only embedding), rather than raising a ``TypeError``.
"""

from typing import Any

from phoson_agent.tool import tool
from phoson_agent.models import AgentTool

from ._agent_role import SwarmError
from ._orchestrator import SwarmRuntime, build_runtime_from_roles

#: Runtime bindings forwarded from the host context into every handler.
_SWARM_INJECT = [
    "chat",
    "available_tools",
    "default_model",
    "max_iterations",
    "middlewares",
    "safe_mode",
    "plugin_ui",
    "bash_confirmation",
]


def build_swarm_tools(plugin: Any) -> list[AgentTool]:
    """Construct the six ``swarm_*`` tools bound to ``plugin``."""
    from phoson_llm.chats.base import BaseLLMChat  # local import: keep schema light

    def _create(
        *,
        chat: BaseLLMChat | None = None,
        available_tools: dict[str, AgentTool] | None = None,
        default_model: str = "",
        max_iterations: int = 0,
        middlewares: list[Any] | None = None,
        safe_mode: bool = False,
        plugin_ui: Any = None,
        bash_confirmation: Any = None,
    ) -> SwarmRuntime:
        """Return the active runtime after a fresh ``swarm_create``."""
        runtime = plugin.runtime
        if runtime is None:
            raise SwarmError("no active swarm; call swarm_create first")
        runtime.bind_runtime(
            chat=chat,
            available_tools=available_tools,
            default_model=default_model,
            middlewares=middlewares,
            max_iterations=max_iterations,
            safe_mode=safe_mode,
            bash_confirmation=bash_confirmation,
            plugin_ui=plugin_ui,
        )
        return runtime

    @tool(inject=_SWARM_INJECT)
    async def swarm_create(
        agents: list[dict[str, Any]],
        topology: str = "star",
        *,
        chat: BaseLLMChat | None = None,
        available_tools: dict[str, AgentTool] | None = None,
        default_model: str = "",
        max_iterations: int = 0,
        middlewares: list[Any] | None = None,
        safe_mode: bool = False,
        plugin_ui: Any = None,
        bash_confirmation: Any = None,
    ) -> dict[str, Any]:
        """Create a multi-agent swarm from a list of specialized agent roles.

        Each agent is an object with at least ``name`` and ``system_prompt``;
        optionally ``model`` (model override), ``tools_allowlist`` (list of the
        only tools that agent may call; an agent can never call a tool outside
        its allowlist) and ``max_tokens`` (that agent's token budget).
        ``topology`` is one of "star" (default, parallel fan-out), "mesh"
        (parallel, full shared blackboard) or "pipeline" (sequential chain).
        """
        runtime = build_runtime_from_roles(
            agents,
            topology=topology,
            max_tokens_per_agent=plugin.max_tokens_per_agent,
            max_tokens_total=plugin.max_tokens_total,
            max_agents=plugin.max_agents,
        )
        runtime.bind_runtime(
            chat=chat,
            available_tools=available_tools,
            default_model=default_model,
            middlewares=middlewares,
            max_iterations=max_iterations,
            safe_mode=safe_mode,
            bash_confirmation=bash_confirmation,
            plugin_ui=plugin_ui,
        )
        plugin.runtime = runtime
        return {
            "created": True,
            "topology": runtime.topology,
            "agents": [
                {
                    "name": inst.name,
                    "model": inst.role.model or default_model or "(default)",
                    "tools": (
                        inst.role.tools_allowlist
                        if inst.role.tools_allowlist is not None
                        else "all-available"
                    ),
                    "max_tokens": inst.role.max_tokens,
                }
                for inst in runtime.instances
            ],
            "note": "use swarm_assign to give the swarm a task, then swarm_collect.",
        }

    @tool(inject=_SWARM_INJECT)
    async def swarm_assign(
        task: str,
        target: str | None = None,
        *,
        chat: BaseLLMChat | None = None,
        available_tools: dict[str, AgentTool] | None = None,
        default_model: str = "",
        max_iterations: int = 0,
        middlewares: list[Any] | None = None,
        safe_mode: bool = False,
        plugin_ui: Any = None,
        bash_confirmation: Any = None,
    ) -> dict[str, Any]:
        """Give the active swarm (or one named agent) a task to work on.

        With ``target`` omitted the whole swarm runs under its topology (star /
        mesh run in parallel, pipeline runs sequentially). The call is
        non-blocking: it starts the work and returns; call ``swarm_collect`` to
        wait for and gather the results.
        """
        runtime = _create(
            chat=chat,
            available_tools=available_tools,
            default_model=default_model,
            max_iterations=max_iterations,
            middlewares=middlewares,
            safe_mode=safe_mode,
            plugin_ui=plugin_ui,
            bash_confirmation=bash_confirmation,
        )
        return await runtime.assign(task, target)

    @tool
    async def swarm_message(
        sender: str,
        recipient: str,
        content: str,
        topic: str = "",
    ) -> dict[str, Any]:
        """Send a message between agents (or from the orchestrator to one).

        ``recipient`` is an agent name, or ``"*"`` to broadcast to everyone.
        Messages are routed to the recipient's inbox and delivered the next
        time that agent runs. ``topic`` is an optional free-form routing label.
        """
        runtime = plugin.runtime
        if runtime is None:
            raise SwarmError("no active swarm; call swarm_create first")
        if recipient != "*" and recipient not in runtime.agent_names:
            raise SwarmError(
                f"unknown recipient {recipient!r}; known agents: "
                f"{', '.join(runtime.agent_names)} or '*' for a broadcast"
            )
        message = runtime.state.send(sender, recipient, content, topic=topic)
        return {
            "sent": True,
            "seq": message.seq,
            "recipient": recipient,
            "pending_for_recipient": runtime.state.pending_count(recipient),
        }

    @tool
    async def swarm_status() -> dict[str, Any]:
        """Report the active swarm: agents, in-flight tasks, results, tokens."""
        runtime = plugin.runtime
        if runtime is None:
            raise SwarmError("no active swarm; call swarm_create first")
        return runtime.status()

    @tool
    async def swarm_collect(timeout: float | None = None) -> dict[str, Any]:
        """Wait for in-flight swarm work and return every agent's result.

        ``timeout`` (seconds) optionally bounds the wait; results gathered so
        far are returned even if the timeout fires first.
        """
        runtime = plugin.runtime
        if runtime is None:
            raise SwarmError("no active swarm; call swarm_create first")
        results = await runtime.collect(timeout=timeout)
        return {
            "results": [r.to_dict() for r in results],
            "tokens_total_used": runtime.budget.total_used(),
        }

    @tool
    async def swarm_dissolve() -> dict[str, Any]:
        """Cancel in-flight work and tear the swarm down, freeing resources."""
        runtime = plugin.runtime
        if runtime is None:
            raise SwarmError("no active swarm; call swarm_create first")
        return runtime.dissolve()

    return [
        swarm_create,
        swarm_assign,
        swarm_message,
        swarm_status,
        swarm_collect,
        swarm_dissolve,
    ]
