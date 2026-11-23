"""Exercise memory tools through the assembled engine runtime."""

from pathlib import Path

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.runtime import build_engine_runtime
from phoson_agent.permissions import PermissionPolicy
from phoson_cli.session_utils import build_offload, build_summarizer
from phoson_cli.permissions_store import build_permission_middleware


class _Chat:
    async def aclose(self):
        pass


def _runtime(tmp_path):
    config = PhosonConfig(sessions_dir=tmp_path / "sessions")
    runtime = build_engine_runtime(
        config,
        chat=_Chat(),
        plugins=[],
        offload=build_offload(config),
        summarizer=build_summarizer(config),
        permission=build_permission_middleware(on_ask=None),
        main_model="main",
        subagent_model="sub",
    )
    return runtime


@pytest.mark.asyncio
async def test_memory_tools_execute_via_runtime_and_use_injected_workspace(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _runtime(tmp_path)
    tools = {tool.name: tool for tool in runtime.engine.tools}
    assert {
        "memory_list",
        "memory_read",
        "memory_write",
        "memory_delete",
    } <= tools.keys()
    context = runtime.engine.context
    context.extra["workspace"] = str(workspace)

    assert (
        tools["memory_write"].handler({"name": "Plan", "content": "Ship it"}, context)
        == "Saved memory 'Plan' (project)."
    )
    assert tools["memory_list"].handler({}, context) == ["plan"]
    assert tools["memory_read"].handler({"name": "PLAN"}, context) == "Ship it\n"
    assert (
        tools["memory_delete"].handler({"name": "Plan"}, context)
        == "Deleted memory 'Plan'."
    )
    assert tools["memory_list"].handler({}, context) == []
    await runtime.engine.chat.aclose()


@pytest.mark.asyncio
async def test_runtime_memory_write_rejects_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = _runtime(tmp_path)
    tool = next(t for t in runtime.engine.tools if t.name == "memory_write")
    runtime.engine.context.extra["workspace"] = str(workspace)
    with pytest.raises(ValueError, match="credential"):
        tool.handler(
            {"name": "secrets", "content": "api_key=abc123"}, runtime.engine.context
        )
    await runtime.engine.chat.aclose()


def test_memory_permissions_are_allowed_without_approval_and_explicit_denials_survive():
    policy = PermissionPolicy()
    build_permission_middleware(on_ask=None, policy_path=None)
    for name in ("memory_list", "memory_read", "memory_write", "memory_delete"):
        assert policy.check(name, {}) == "allow"
    denied = PermissionPolicy(levels={"memory_write": "deny", "memory_delete": "deny"})
    for name in ("memory_write", "memory_delete"):
        assert denied.check(name, {}) == "deny"
