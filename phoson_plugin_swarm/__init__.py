"""phoson_plugin_swarm: multi-agent swarm orchestration (issue #232).

Bundled plugin. Importing the package exposes the module-level ``plugin``
instance (the loader's convention) plus the public symbols for embedding and
testing.
"""

from ._plugin import (
    DEFAULT_TOPOLOGY,
    DEFAULT_MAX_AGENTS,
    DEFAULT_MAX_TOKENS_TOTAL,
    DEFAULT_MAX_TOKENS_PER_AGENT,
    SwarmPlugin,
    create_plugin,
)
from ._agent_role import (
    AgentRole,
    SwarmError,
    AgentResult,
    AgentInstance,
)
from ._blackboard import SharedState, TokenBudget, SwarmMessage
from ._orchestrator import SwarmRuntime

#: Module-level plugin instance (the package loader's convention).
plugin = SwarmPlugin()

__all__ = [
    "AgentInstance",
    "AgentResult",
    "AgentRole",
    "SwarmError",
    "SharedState",
    "SwarmMessage",
    "TokenBudget",
    "SwarmRuntime",
    "SwarmPlugin",
    "create_plugin",
    "plugin",
    "DEFAULT_MAX_AGENTS",
    "DEFAULT_MAX_TOKENS_PER_AGENT",
    "DEFAULT_MAX_TOKENS_TOTAL",
    "DEFAULT_TOPOLOGY",
]
