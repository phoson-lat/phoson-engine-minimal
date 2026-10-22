"""Wiring for the official in-tree plugins (MCP, monitors, OTel, ...).

Every official plugin is resolved the same way, so each one is described by
an :class:`OfficialPlugin` entry in :data:`OFFICIAL_PLUGINS` and loaded by
:func:`load_official_plugin`:

1. Skip it unless the configuration enables it.
2. Import the in-tree package and return a *pre-configured, fresh* instance
   (the direct-``Plugin`` form, so the config is honored). Never the
   module-level ``plugin`` singleton: engine rebuilds close the old instance
   first, and a singleton would be double-configured and leak state between
   hosts.
3. When an optional third-party dependency is missing (``mcp``,
   ``asyncssh``), log an actionable install hint and skip the plugin — the
   engine would otherwise report a confusing "Skipping plugin" error.
4. When the package cannot be imported at all (e.g. an editable install
   without the sibling folder on ``sys.path``), fall back to an *absolute*
   ``path:`` spec pointing at the in-tree ``_plugin.py``; if that file is
   missing too, warn and skip, so a missing optional plugin never crashes
   the engine.
5. Any other construction error warns and skips.

The list order is the load order (and therefore tool/middleware order).
"""

import logging
import warnings
import importlib
from typing import Any
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable

from phoson_agent import Plugin

from .config import PhosonConfig

_LOGGER = logging.getLogger("phoson_cli.session_utils")

#: Anything ``AgentEngine(plugins=...)`` accepts.
PluginSpec = str | dict[str, Any] | Plugin


@dataclass(frozen=True)
class OfficialPlugin:
    """How to enable, configure and construct one official plugin."""

    #: Import name of the in-tree package, e.g. ``"phoson_plugin_monitor"``.
    package: str
    #: Plugin class exported by ``package``.
    class_name: str
    #: Whether this configuration turns the plugin on.
    enabled: Callable[[PhosonConfig], bool]
    #: The ``configure()`` payload (also used by the path fallback).
    settings: Callable[[PhosonConfig], dict[str, Any]]
    #: Suffix of the "not importable" warning, e.g. ``"monitors disabled"``.
    disabled_msg: str
    #: Warning for any other construction error (``{exc}`` is substituted).
    failure_msg: str
    #: ``(module, flag)`` naming a bool that is False when an optional
    #: dependency is missing; paired with :attr:`missing_dependency_hint`.
    availability: tuple[str, str] | None = None
    missing_dependency_hint: str = ""
    #: Call ``initialize()`` right away (peers claim their name up front so a
    #: clash fails early and clearly instead of at first use).
    initialize_now: bool = False


def _peer_name(config: PhosonConfig) -> str:
    return (getattr(config, "peer_name", "") or "").strip()


def _user_specified_otel(plugins: list[str | dict[str, Any]]) -> bool:
    """True when the user explicitly enabled otel via a ``[plugins]`` spec."""
    for spec in plugins:
        name = (
            spec
            if isinstance(spec, str)
            else (spec.get("name", "") if isinstance(spec, dict) else "")
        )
        if name in ("phoson-plugin-otel", "phoson_plugin_otel"):
            return True
    return False


MCP = OfficialPlugin(
    package="phoson_plugin_mcp",
    class_name="MCPPlugin",
    enabled=lambda c: c.enable_mcp,
    settings=lambda c: {
        "config_file": str(c.mcp_config_file),
        "tool_name_prefix": "mcp",
    },
    disabled_msg="MCP disabled",
    failure_msg="Failed to initialise MCP plugin: {exc}",
    # The in-tree plugin imports without the SDK; ``initialize()`` is what
    # raises.
    availability=("phoson_plugin_mcp._plugin", "MCP_AVAILABLE"),
    missing_dependency_hint=(
        "MCP is enabled but the `mcp` package is not installed. "
        "Install with: uv sync --dev  (or: uv sync --extra mcp / "
        "pip install 'phoson-engine-minimal[mcp]'). "
        "Set enable_mcp = false in [defaults] to silence this."
    ),
)

#: I-126.
MONITOR = OfficialPlugin(
    package="phoson_plugin_monitor",
    class_name="MonitorPlugin",
    enabled=lambda c: c.enable_monitors,
    settings=lambda c: {"data_dir": str(c.monitors_data_dir)},
    disabled_msg="monitors disabled",
    failure_msg="Failed to initialise monitor plugin: {exc}",
)

#: #217.
BGJOBS = OfficialPlugin(
    package="phoson_plugin_bgjobs",
    class_name="BgJobsPlugin",
    enabled=lambda c: c.enable_bgjobs,
    settings=lambda c: {"data_dir": str(c.bgjobs_data_dir)},
    disabled_msg="background jobs disabled",
    failure_msg="Failed to initialise background-jobs plugin: {exc}",
)

#: Named peers (``--name``). An invalid or taken name degrades to a warning
#: (the CLI still starts, just without peer messaging).
PEERS = OfficialPlugin(
    package="phoson_plugin_peers",
    class_name="PeersPlugin",
    enabled=lambda c: bool(_peer_name(c)),
    settings=lambda c: {
        "name": _peer_name(c),
        "team": getattr(c, "peer_team", "") or "default",
        "data_dir": str(c.peers_data_dir),
        "max_hops": c.peers_max_hops,
    },
    disabled_msg="peer messaging disabled",
    failure_msg="Peer messaging disabled: {exc}",
    initialize_now=True,
)

#: #169.
SSH = OfficialPlugin(
    package="phoson_plugin_ssh",
    class_name="SshPlugin",
    enabled=lambda c: c.enable_ssh,
    settings=lambda c: {
        "known_hosts": str(c.ssh_known_hosts),
        "command_timeout": c.ssh_command_timeout,
    },
    disabled_msg="SSH disabled",
    failure_msg="Failed to initialise SSH plugin: {exc}",
    availability=("phoson_plugin_ssh", "SSH_AVAILABLE"),
    missing_dependency_hint=(
        "SSH is enabled but the `asyncssh` package is not installed. "
        "Install with: uv sync --extra ssh  (or: "
        "pip install 'phoson-engine-minimal[ssh]'). "
        "Set enable_ssh = false in [defaults] to silence this."
    ),
)

#: #223. Off by default: it operates the real desktop.
COMPUTERUSE = OfficialPlugin(
    package="phoson_plugin_computeruse",
    class_name="ComputerUsePlugin",
    enabled=lambda c: c.enable_computeruse,
    settings=lambda c: {
        "backend": c.computeruse_backend,
        "require_confirmation": c.computeruse_require_confirmation,
    },
    disabled_msg="Computer Use disabled",
    failure_msg="Failed to initialise Computer Use plugin: {exc}",
)

#: #140. A user-listed ``phoson-plugin-otel`` spec wins (no double-tracing).
OTEL = OfficialPlugin(
    package="phoson_plugin_otel",
    class_name="PhosonOtelPlugin",
    enabled=lambda c: c.enable_otel and not _user_specified_otel(c.plugins),
    settings=lambda c: {
        "service_name": c.otel_service_name,
        "file_path": str(c.otel_file_path),
        "otlp_endpoint": c.otel_endpoint,
    },
    disabled_msg="otel tracing disabled",
    failure_msg="Failed to initialise otel plugin: {exc}",
)

#: #232.
SWARM = OfficialPlugin(
    package="phoson_plugin_swarm",
    class_name="SwarmPlugin",
    enabled=lambda c: c.enable_swarm,
    settings=lambda c: {
        "max_agents": c.swarm_max_agents,
        "default_topology": c.swarm_default_topology,
        "max_tokens_per_agent": c.swarm_max_tokens_per_agent,
        "max_tokens_total": c.swarm_max_tokens_total,
    },
    disabled_msg="swarm disabled",
    failure_msg="Failed to initialise swarm plugin: {exc}",
)

#: Opt-in (``enable_questions``).
QUESTIONS = OfficialPlugin(
    package="phoson_plugin_questions",
    class_name="QuestionsPlugin",
    enabled=lambda c: c.enable_questions,
    settings=lambda c: {"title": c.questions_title},
    disabled_msg="questions disabled",
    failure_msg="Failed to initialise Questions plugin: {exc}",
)

#: Load order of the official plugins (after the user's ``[plugins]``).
OFFICIAL_PLUGINS: tuple[OfficialPlugin, ...] = (
    MCP,
    MONITOR,
    BGJOBS,
    PEERS,
    SSH,
    COMPUTERUSE,
    OTEL,
    SWARM,
    QUESTIONS,
)


def _in_tree_plugin_path(package: str) -> Path:
    """Absolute path of an in-tree plugin's ``_plugin.py`` (fallback target).

    Anchored on this file (not the CWD) so it resolves the same no matter
    where the CLI is launched from.
    """
    root = Path(__file__).resolve().parent.parent
    return root / package / "_plugin.py"


def _in_tree_fallback_spec(
    package: str, config: dict[str, Any], disabled_msg: str
) -> list[PluginSpec]:
    """Build a path-based plugin spec for an in-tree package (ImportError).

    Returns an *absolute* (CWD-independent) ``path:`` spec; if the in-tree
    file does not exist either, a warning is emitted and an empty list is
    returned so the engine never crashes on a missing optional plugin.
    """
    candidate = _in_tree_plugin_path(package)
    if not candidate.exists():
        warnings.warn(
            f"{package} not importable and in-tree file not found "
            f"at {candidate}; {disabled_msg}.",
            UserWarning,
            stacklevel=3,
        )
        return []
    return [{"name": f"path:{candidate}", "config": config}]


def load_official_plugin(
    spec: OfficialPlugin, config: PhosonConfig
) -> list[PluginSpec]:
    """Resolve one official plugin for *config* (see the module docstring)."""
    if not spec.enabled(config):
        return []
    settings = spec.settings(config)
    try:
        module = importlib.import_module(spec.package)
        if spec.availability is not None:
            flag_module, flag = spec.availability
            if not getattr(importlib.import_module(flag_module), flag):
                _LOGGER.warning(spec.missing_dependency_hint)
                return []
        instance = getattr(module, spec.class_name)()
        instance.configure(settings)
        if spec.initialize_now:
            instance.initialize()
        return [instance]
    except ImportError:
        return _in_tree_fallback_spec(spec.package, settings, spec.disabled_msg)
    except Exception as exc:
        warnings.warn(spec.failure_msg.format(exc=exc), UserWarning, stacklevel=3)
        return []


def load_official_plugins(config: PhosonConfig) -> list[PluginSpec]:
    """Every enabled official plugin, in :data:`OFFICIAL_PLUGINS` order."""
    return [
        plugin
        for spec in OFFICIAL_PLUGINS
        for plugin in load_official_plugin(spec, config)
    ]


__all__ = [
    "OFFICIAL_PLUGINS",
    "OfficialPlugin",
    "load_official_plugin",
    "load_official_plugins",
]
