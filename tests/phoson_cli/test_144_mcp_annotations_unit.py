"""Tests for MCP annotations as permission signal (issue #144, phase 2).

MCP servers publish ``ToolAnnotations`` (read-only / destructive /
idempotent / open-world). Those hints travel
``remote_tool.annotations → AgentTool.metadata → policy.hints`` and can only
*tighten* the gate (read-only → allow, everything else → ask). An explicit
user level in ``permissions.json`` always wins, and tools without hints keep
the allow-by-default behaviour.

No MCP server or network is needed: the plugin module imports without the
``mcp`` SDK, and remote tools are faked with ``SimpleNamespace``.
"""

from types import SimpleNamespace

from phoson_agent.models import AgentTool
from phoson_agent.permissions import (
    LEVEL_ASK,
    LEVEL_DENY,
    LEVEL_ALLOW,
    ToolHints,
    PermissionPolicy,
    collect_tool_hints,
)
from phoson_plugin_mcp._plugin import _extract_annotations
from phoson_cli.permissions_store import apply_tool_hints


def _agent_tool(name: str, metadata: dict | None = None) -> AgentTool:
    return AgentTool(
        name=name,
        description="",
        parameters={},
        handler=lambda args, ctx=None: "",
        metadata=metadata or {},
    )


#: A tool that declared itself unambiguously read-only → derived allow.
_READ_ONLY = ToolHints(annotated=True, read_only=True, destructive=False)


# ── ToolHints.derived_level ───────────────────────────────────────────────────


def test_read_only_tool_derives_allow() -> None:
    hints = ToolHints(annotated=True, read_only=True, destructive=False)
    assert hints.derived_level() == LEVEL_ALLOW


def test_unannotated_tool_derives_ask() -> None:
    assert ToolHints().derived_level() == LEVEL_ASK


def test_destructive_tool_derives_ask() -> None:
    hints = ToolHints(annotated=True, read_only=False, destructive=True)
    assert hints.derived_level() == LEVEL_ASK


def test_read_only_but_destructive_derives_ask() -> None:
    """A contradictory annotation is not trusted → safe default."""
    hints = ToolHints(annotated=True, read_only=True, destructive=True)
    assert hints.derived_level() == LEVEL_ASK


# ── ToolHints.from_metadata ───────────────────────────────────────────────────


def test_from_metadata_missing_key_returns_none() -> None:
    assert ToolHints.from_metadata({}) is None
    assert ToolHints.from_metadata(None) is None
    assert ToolHints.from_metadata({"other": 1}) is None


def test_from_metadata_parses_hints() -> None:
    hints = ToolHints.from_metadata(
        {
            "mcp_annotations": {
                "annotated": True,
                "read_only": True,
                "destructive": False,
                "idempotent": True,
                "open_world": False,
            }
        }
    )
    assert hints == ToolHints(
        annotated=True,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )


# ── PermissionPolicy.check with hints ────────────────────────────────────────


def test_policy_read_only_hint_allows() -> None:
    policy = PermissionPolicy(hints={"mcp_fs_read": _READ_ONLY})
    assert policy.check("mcp_fs_read") == LEVEL_ALLOW


def test_policy_destructive_hint_asks() -> None:
    policy = PermissionPolicy(
        hints={"mcp_fs_write": ToolHints(annotated=True, destructive=True)}
    )
    assert policy.check("mcp_fs_write") == LEVEL_ASK


def test_policy_unannotated_mcp_tool_asks() -> None:
    """The safe default is scoped to hinted (MCP) tools only."""
    policy = PermissionPolicy(hints={"mcp_x_tool": ToolHints(annotated=False)})
    assert policy.check("mcp_x_tool") == LEVEL_ASK


def test_policy_without_hints_keeps_allow_default() -> None:
    assert PermissionPolicy().check("read_file") == LEVEL_ALLOW


def test_explicit_level_wins_over_hints() -> None:
    """A user rule can relax (or harden) an annotated tool."""
    policy = PermissionPolicy(
        levels={"mcp_fs_write": LEVEL_ALLOW},
        hints={"mcp_fs_write": ToolHints(annotated=True, destructive=True)},
    )
    assert policy.check("mcp_fs_write") == LEVEL_ALLOW

    policy = PermissionPolicy(
        levels={"mcp_fs_read": LEVEL_DENY},
        hints={"mcp_fs_read": _READ_ONLY},
    )
    assert policy.check("mcp_fs_read") == LEVEL_DENY


# ── collect_tool_hints / apply_tool_hints ────────────────────────────────────


def test_collect_tool_hints_skips_tools_without_metadata() -> None:
    tools = [
        _agent_tool("bash"),
        _agent_tool(
            "mcp_fs_read",
            {"mcp_annotations": {"annotated": True, "read_only": True}},
        ),
    ]
    hints = collect_tool_hints(tools)
    assert set(hints) == {"mcp_fs_read"}
    assert hints["mcp_fs_read"].read_only is True


def test_apply_tool_hints_replaces_previous_mapping() -> None:
    policy = PermissionPolicy(hints={"stale": ToolHints(annotated=True)})
    apply_tool_hints(
        policy,
        [
            _agent_tool(
                "mcp_new",
                {"mcp_annotations": {"annotated": True, "read_only": True}},
            )
        ],
    )
    assert set(policy.hints) == {"mcp_new"}


# ── MCP plugin: annotation extraction ────────────────────────────────────────


def test_extract_annotations_without_annotations() -> None:
    assert _extract_annotations(SimpleNamespace()) == {"annotated": False}


def test_extract_annotations_read_only() -> None:
    remote = SimpleNamespace(
        annotations=SimpleNamespace(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    assert _extract_annotations(remote) == {
        "annotated": True,
        "read_only": True,
        "destructive": False,
        "idempotent": True,
        "open_world": False,
    }


def test_extract_annotations_defaults_are_conservative() -> None:
    """A partial annotation must never look safe."""
    remote = SimpleNamespace(annotations=SimpleNamespace(readOnlyHint=True))
    assert _extract_annotations(remote) == {
        "annotated": True,
        "read_only": True,
        "destructive": True,  # MCP default
        "idempotent": False,
        "open_world": True,  # MCP default
    }


def test_plugin_attaches_annotations_to_agent_tool() -> None:
    """_agent_tools_from_remote_tools carries hints into AgentTool.metadata."""
    from phoson_plugin_mcp import MCPPlugin

    plugin = MCPPlugin()
    plugin.servers = {"fs": {"command": "node"}}
    remote = [
        SimpleNamespace(
            name="read_file",
            description="read",
            inputSchema={},
            annotations=SimpleNamespace(
                readOnlyHint=True, destructiveHint=False, openWorldHint=False
            ),
        ),
        SimpleNamespace(name="write_file", description="write", inputSchema={}),
    ]
    tools = plugin._agent_tools_from_remote_tools("fs", remote)
    by_name = {t.name: t for t in tools}

    read = by_name["mcp_fs_read_file"]
    assert read.metadata["mcp_annotations"]["read_only"] is True
    assert ToolHints.from_metadata(read.metadata).derived_level() == LEVEL_ALLOW

    write = by_name["mcp_fs_write_file"]
    assert write.metadata["mcp_annotations"] == {"annotated": False}
    assert ToolHints.from_metadata(write.metadata).derived_level() == LEVEL_ASK


def test_plugin_proxy_tool_is_not_trusted() -> None:
    from phoson_plugin_mcp import MCPPlugin

    plugin = MCPPlugin()
    (proxy,) = plugin._create_proxy_tools("fs")
    assert proxy.metadata["mcp_annotations"] == {"annotated": False}
    assert ToolHints.from_metadata(proxy.metadata).derived_level() == LEVEL_ASK
