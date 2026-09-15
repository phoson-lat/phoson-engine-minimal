"""The Swarm plugin (issue #232).

A bundled :class:`~phoson_agent.plugin.Plugin` that lets the main agent
orchestrate a *swarm* of specialized, concurrently-running sub-agents with
shared state (a blackboard) and message routing — the multi-agent pattern from
AutoGen / CrewAI / Camel, integrated with Phoson.

Each member runs as its own :class:`phoson_agent.agent.AgentEngine` with a small
focused context, a per-agent tool allowlist and a token budget; a shared
orchestrator coordinates them under a star (default), mesh or pipeline topology.

The plugin is opt-in (``enable_swarm = true`` / ``PHOSON_ENABLE_SWARM``). The
``swarm_*`` tools are added to the main engine; they reuse the host's injected
``chat``/``available_tools``/``default_model``/``middlewares`` (see
:mod:`._tools`) so each member engine is built exactly the way the CLI builds
its sub-agents, with the same permission middleware and runtime flags.
"""

import logging
from typing import Any

from phoson_agent.models import AgentTool
from phoson_agent.plugin import Plugin

from ._tools import build_swarm_tools
from ._agent_role import SwarmError  # re-exported for callers/tests
from ._orchestrator import SwarmRuntime

logger = logging.getLogger(__name__)

#: Suggested config defaults (the issue's proposed numbers).
DEFAULT_MAX_AGENTS = 5
DEFAULT_MAX_TOKENS_PER_AGENT = 4096
DEFAULT_MAX_TOKENS_TOTAL = 32768
DEFAULT_TOPOLOGY = "star"


class SwarmPlugin(Plugin):
    """Orchestrate a coordinated multi-agent swarm with roles and shared state."""

    def __init__(self) -> None:
        self._runtime: SwarmRuntime | None = None
        self._max_agents = DEFAULT_MAX_AGENTS
        self._max_tokens_per_agent = DEFAULT_MAX_TOKENS_PER_AGENT
        self._max_tokens_total = DEFAULT_MAX_TOKENS_TOTAL
        self._default_topology = DEFAULT_TOPOLOGY

    # ── Plugin contract ───────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "phoson-plugin-swarm"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return (
            "Orchestrate a swarm of specialized multi-agents with roles, "
            "shared state and star/mesh/pipeline topologies"
        )

    def configure(self, config: dict[str, Any]) -> None:
        """Merge config; absent keys keep their defaults.

        Raises:
            SwarmError: on a non-positive or non-integer limit.
        """
        if not isinstance(config, dict):
            raise SwarmError("swarm plugin config must be a dict")

        if "max_agents" in config:
            value = config["max_agents"]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SwarmError("'max_agents' must be a positive integer")
            self._max_agents = value
        if "default_topology" in config:
            value = config["default_topology"]
            if value not in ("star", "mesh", "pipeline"):
                raise SwarmError(
                    "'default_topology' must be one of star, mesh, pipeline"
                )
            self._default_topology = value
        if "max_tokens_per_agent" in config:
            self._max_tokens_per_agent = _positive_int(
                config["max_tokens_per_agent"], "max_tokens_per_agent"
            )
        if "max_tokens_total" in config:
            self._max_tokens_total = _positive_int(
                config["max_tokens_total"], "max_tokens_total"
            )

    def initialize(self) -> None:
        logger.debug(
            "Swarm plugin initialized: max_agents=%d, topology=%s, "
            "tokens/agent=%d, tokens/total=%d",
            self._max_agents,
            self._default_topology,
            self._max_tokens_per_agent,
            self._max_tokens_total,
        )

    async def aclose(self) -> None:
        """Tear down any active swarm (cancel in-flight members)."""
        runtime = self._runtime
        if runtime is not None:
            await runtime.aclose()
        self._runtime = None

    def cleanup(self) -> None:
        """Sync fallback; a fresh plugin instance starts with no runtime."""
        self._runtime = None

    # ── Tools ─────────────────────────────────────────────────────────────

    def get_tools(self) -> list[AgentTool]:
        return build_swarm_tools(self)

    # ── Accessors used by the tool handlers ───────────────────────────────

    @property
    def runtime(self) -> SwarmRuntime | None:
        return self._runtime

    @runtime.setter
    def runtime(self, value: SwarmRuntime | None) -> None:
        self._runtime = value

    @property
    def max_agents(self) -> int:
        return self._max_agents

    @property
    def max_tokens_per_agent(self) -> int:
        return self._max_tokens_per_agent

    @property
    def max_tokens_total(self) -> int:
        return self._max_tokens_total

    @property
    def default_topology(self) -> str:
        return self._default_topology


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SwarmError(f"'{field_name}' must be a positive integer")
    return value


def create_plugin() -> SwarmPlugin:
    """Factory for the path-based loader (``path:./phoson_plugin_swarm/_plugin.py``)."""
    return SwarmPlugin()


__all__ = [
    "SwarmPlugin",
    "SwarmError",
    "create_plugin",
    "DEFAULT_MAX_AGENTS",
    "DEFAULT_MAX_TOKENS_PER_AGENT",
    "DEFAULT_MAX_TOKENS_TOTAL",
    "DEFAULT_TOPOLOGY",
]
