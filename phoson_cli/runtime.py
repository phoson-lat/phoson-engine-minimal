"""Shared agent-runtime assembly for every CLI host.

The interactive :class:`~phoson_cli.controller.SessionController` and the
one-shot path (``phoson_cli.__main__._run_oneshot``) must build the engine
the same way: same tool registry, same middleware chain (Offload →
Summarizer → EnvironmentalContext → DoomLoop → Permission), same
permission tool hints, same sub-agent context. Each used to assemble it
independently, and the copies drifted (#174/F-02: one-shot lost the
permission gate; later it also missed the reasoning-sandwich scheduler and
the summarizer's tool-schema accounting). :func:`build_engine_runtime` is
the single place that sequence lives.

What stays with the caller is what genuinely differs per host: building
the chat client and the plugin specs (both patched by tests at the call
site), the long-lived summarizer/offload/permission middlewares (the
controller reuses them across rebuilds), the presentation-bound context
(``plugin_ui``, confirmations, progress callbacks) and plugin teardown.

This module is UI-free: it must not import ``rich`` or ``prompt_toolkit``.
"""

from typing import Any
from dataclasses import dataclass
from collections.abc import Callable, Iterable

from phoson_agent import Plugin, AgentTool, AgentEngine, AgentMiddleware
from phoson_llm.schemas import ToolDefinition
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent.permissions import PermissionMiddleware
from phoson_agent.plugins.offload import OffloadMiddleware
from phoson_agent.reasoning_effort import make_live_scheduler
from phoson_agent.plugins.summarizer import SummarizationMiddleware

from .tools import build_tools, build_tools_dict
from .config import PhosonConfig
from .session_utils import build_middlewares, engine_visible_tools
from .permissions_store import apply_tool_hints

#: Anything ``AgentEngine(plugins=...)`` accepts.
PluginSpec = str | dict[str, Any] | Plugin


@dataclass
class EngineRuntime:
    """The assembled runtime returned by :func:`build_engine_runtime`."""

    engine: AgentEngine
    #: Built-in tools (without host-only extras such as ``compact_context``).
    tools: list[AgentTool]
    #: Built-in tools by name — what sub-agents may pick from.
    tools_dict: dict[str, AgentTool]
    #: The chain handed to the engine *and* to sub-agents (#174/F-01).
    middlewares: list[AgentMiddleware]

    @property
    def loaded_plugins(self) -> list[Plugin]:
        """Plugins the engine actually initialized."""
        return list(getattr(self.engine, "_loaded_plugins", []))


def build_effort_scheduler(config: PhosonConfig):
    """Live per-iteration effort scheduler (#145), or None when off.

    The scheduler re-reads ``config`` on every iteration, so a mid-session
    ``/reasoning-effort`` (which mutates ``config.reasoning_effort``) wins
    over the per-phase profile on the very next step.
    """
    if not config.reasoning_sandwich:
        return None
    return make_live_scheduler(
        get_override=lambda: config.reasoning_effort,
        get_profile=lambda: config.reasoning_effort_profile,
    )


def build_engine_runtime(
    config: PhosonConfig,
    *,
    chat: BaseLLMChat,
    plugins: list[PluginSpec],
    offload: OffloadMiddleware | None,
    summarizer: SummarizationMiddleware | None,
    permission: PermissionMiddleware,
    main_model: str,
    subagent_model: str,
    extra_tools: Iterable[AgentTool] = (),
    extra_middlewares: Iterable[AgentMiddleware] = (),
    engine_factory: Callable[..., AgentEngine] = AgentEngine,
) -> EngineRuntime:
    """Build the engine and its host-independent context.

    Args:
        config: Active configuration (gating flags, limits, safe_mode).
        chat: Chat client for the engine; the summarizer's internal summary
            call is routed through it too (tool-free, F-11 / #176).
        plugins: Plugin specs for ``AgentEngine(plugins=...)``.
        offload / summarizer / permission: The long-lived middlewares; the
            chain order and gating come from
            :func:`~phoson_cli.session_utils.build_middlewares`.
        main_model: Fallback model for sub-agents.
        subagent_model: Default sub-agent model.
        extra_tools: Main-engine-only tools (e.g. ``compact_context``, #147).
            They are *not* added to ``tools_dict``, so sub-agents never get
            them.
        extra_middlewares: Appended after the shared chain (e.g. the
            one-shot ``TraceMiddleware``, which only observes events).
        engine_factory: Engine constructor; callers pass their module-level
            ``AgentEngine`` so tests patching it at the call site still work.
    """
    tools = build_tools()
    tools_dict = build_tools_dict()

    if summarizer is not None:
        summarizer.chat = chat

    middlewares = build_middlewares(
        config=config,
        offload=offload,
        summarizer=summarizer,
        permission=permission,
    )
    middlewares.extend(extra_middlewares)

    engine = engine_factory(
        chat=chat,
        tools=[*tools, *extra_tools],
        middlewares=middlewares,
        plugins=plugins,
        max_iterations=config.max_iterations,
        effort_scheduler=build_effort_scheduler(config),
        tool_budget_tokens=config.tool_budget_tokens or None,
    )
    # #144 phase 2: fold MCP tool annotations (read-only/destructive/
    # open-world) into the permission policy now that plugins have loaded.
    # Replaces the hint map each rebuild so a changed tool set cannot leave
    # stale hints behind.
    apply_tool_hints(permission.policy, engine.tools)

    # Mirror the engine's tool schemas (built-in + plugin/MCP tools) into the
    # summarizer so the auto-compact gate counts the schema weight of every
    # request (IMPROVEMENTS.md I-91).
    if summarizer is not None:
        summarizer.tool_definitions = [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in engine_visible_tools(engine)
        ]

    extra = engine.context.extra
    extra["safe_mode"] = config.safe_mode
    extra["chat"] = chat
    extra["available_tools"] = tools_dict
    extra["default_model"] = subagent_model
    extra["main_model"] = main_model
    extra["max_iterations"] = config.max_iterations
    extra["subagent_max_parallel"] = config.subagent_max_parallel
    extra["subagent_timeout_seconds"] = config.subagent_timeout_seconds
    # The middleware chain, handed to sub-agents so the permission gate
    # applies to their tool calls too (#174/F-01).
    extra["middlewares"] = middlewares

    return EngineRuntime(
        engine=engine,
        tools=tools,
        tools_dict=tools_dict,
        middlewares=middlewares,
    )


__all__ = ["EngineRuntime", "build_effort_scheduler", "build_engine_runtime"]
