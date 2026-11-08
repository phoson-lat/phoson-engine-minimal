"""Manifest capabilities feed the permission gate for user-installed plugins."""

from types import SimpleNamespace

import pytest

from phoson_agent.models import AgentTool
from phoson_agent.permissions import (
    ToolHints,
    PermissionPolicy,
    capability_hints,
    capability_intents,
    apply_plugin_capabilities,
)
from phoson_agent.plugin_manifest import Capabilities


def _tool(name: str) -> AgentTool:
    async def handler() -> str:
        return "ok"

    return AgentTool(name=name, description=name, parameters={}, handler=handler)


def _plugin(tools: list[str], **caps) -> SimpleNamespace:
    manifest = SimpleNamespace(capabilities=Capabilities(**caps))
    built = [_tool(name) for name in tools]
    return SimpleNamespace(manifest=manifest, get_tools=lambda: built)


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        ({}, set()),
        ({"network": True}, {"network_outbound"}),
        ({"shell": True}, {"process_spawn"}),
        ({"filesystem": "read"}, {"filesystem_read"}),
        ({"filesystem": "write"}, {"filesystem_read", "filesystem_write"}),
    ],
)
def test_capability_intents(caps, expected) -> None:
    assert capability_intents(Capabilities(**caps)) == expected


@pytest.mark.parametrize(
    ("caps", "risky"),
    [
        ({}, False),
        ({"network": True}, False),
        ({"filesystem": "read"}, False),
        ({"shell": True}, True),
        ({"filesystem": "write"}, True),
    ],
)
def test_capability_hints(caps, risky) -> None:
    hint = capability_hints(Capabilities(**caps))
    assert (hint is not None) is risky
    if hint is not None:
        assert hint.derived_level() == "ask"


def test_risky_plugin_tools_ask_by_default_others_allow() -> None:
    policy = PermissionPolicy()
    apply_plugin_capabilities(
        policy,
        [
            _plugin(["run_it"], shell=True),
            _plugin(["fetch_it"], network=True),
            SimpleNamespace(manifest=None, get_tools=lambda: [_tool("bare")]),
        ],
    )
    assert policy.evaluate("run_it")[:2] == ("ask", "hint")
    assert policy.evaluate("run_it")[2] == ("process_spawn",)
    assert policy.evaluate("fetch_it") == ("allow", "default", ("network_outbound",))
    assert policy.evaluate("bare") == ("allow", "default", ())


def test_tool_annotations_win_over_capability_hint() -> None:
    read_only = ToolHints(annotated=True, read_only=True, destructive=False)
    policy = PermissionPolicy(hints={"list_jobs": read_only})
    apply_plugin_capabilities(policy, [_plugin(["list_jobs", "start_job"], shell=True)])
    assert policy.evaluate("list_jobs")[0] == "allow"
    assert policy.evaluate("start_job")[0] == "ask"


def test_user_intent_rules_cover_plugin_tools() -> None:
    policy = PermissionPolicy(intent_levels={"network_outbound": "deny"})
    apply_plugin_capabilities(policy, [_plugin(["fetch_it"], network=True)])
    level, source, intents = policy.evaluate("fetch_it")
    assert (level, source) == ("deny", "intent")
    assert "network_outbound" in intents


def test_auto_mode_and_explicit_levels_override_the_default() -> None:
    auto = PermissionPolicy(levels={"*": "allow"})
    apply_plugin_capabilities(auto, [_plugin(["run_it"], shell=True)])
    assert auto.evaluate("run_it")[0] == "allow"

    explicit = PermissionPolicy(levels={"run_it": "allow"})
    apply_plugin_capabilities(explicit, [_plugin(["run_it"], shell=True)])
    assert explicit.evaluate("run_it")[0] == "allow"


def test_reapplying_replaces_tool_intents() -> None:
    policy = PermissionPolicy()
    apply_plugin_capabilities(policy, [_plugin(["fetch_it"], network=True)])
    apply_plugin_capabilities(policy, [])
    assert policy.tool_intents == {}


def test_broken_plugin_is_skipped() -> None:
    def boom():
        raise RuntimeError("bad plugin")

    policy = PermissionPolicy()
    broken = SimpleNamespace(
        manifest=SimpleNamespace(capabilities=Capabilities(shell=True)),
        get_tools=boom,
    )
    apply_plugin_capabilities(policy, [broken, _plugin(["ok_tool"], shell=True)])
    assert policy.evaluate("ok_tool")[0] == "ask"


def test_cli_applies_capabilities_only_to_community_plugins() -> None:
    from phoson_cli.permissions_store import apply_tool_hints, community_plugins

    community = _plugin(["acme_run"], shell=True)
    official = type("Bg", (), {"__module__": "phoson_plugin_bgjobs._plugin"})()
    official.manifest = SimpleNamespace(capabilities=Capabilities(shell=True))
    official.get_tools = lambda: [_tool("job_list")]

    assert community_plugins([community, official]) == [community]

    policy = PermissionPolicy()
    apply_tool_hints(policy, [], [community, official])
    assert policy.evaluate("acme_run")[0] == "ask"
    assert policy.evaluate("job_list") == ("allow", "default", ())
