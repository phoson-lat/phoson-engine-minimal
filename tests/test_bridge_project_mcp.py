import json
from types import SimpleNamespace

import pytest

from phoson_bridge.server import Bridge


@pytest.fixture
def server(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = workspace / ".phoson" / "mcps.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcpServers": {"sample": {"command": "run", "args": ["one"], "env": {"TOKEN": "secret-value"}, "url": "https://user:password@example.test/mcp?key=private"}}})
    )
    trust = tmp_path / "trusted-project-mcps.json"
    monkeypatch.setattr("phoson_plugin_mcp._plugin._project_mcp_trust_path", lambda: trust)
    monkeypatch.setattr("phoson_bridge.server.load_config", lambda: SimpleNamespace(mcp_config_file=str(tmp_path / "global.json")))
    monkeypatch.setattr(
        "phoson_bridge.server.load_config",
        lambda: SimpleNamespace(mcp_config_file=str(tmp_path / "global.json")),
    )
    instance = object.__new__(Bridge)
    return instance, workspace


@pytest.mark.asyncio
async def test_get_and_approve_project_mcp(server):
    instance, workspace = server
    result = await instance._mcp_project_get({"workspace": str(workspace)})
    entry = result["mcps"][0]
    assert entry["name"] == "sample" and entry["approved"] is False
    rendered = json.dumps(result)
    assert "secret-value" not in rendered and "password" not in rendered and "private" not in rendered
    assert entry["envKeys"] == ["TOKEN"]
    assert entry["url"] == "https://example.test/mcp?[redacted]"
    assert (
        await instance._mcp_project_approve(
            {"workspace": str(workspace), "name": "sample", "hash": entry["hash"]}
        )
    )["ok"]
    assert (await instance._mcp_project_get({"workspace": str(workspace)}))["mcps"][0][
        "approved"
    ] is True


@pytest.mark.asyncio
async def test_stale_hash_cannot_approve(server, tmp_path):
    instance, workspace = server
    config = workspace / ".phoson" / "mcps.json"
    entry = (await instance._mcp_project_get({"workspace": str(workspace)}))["mcps"][0]
    config.write_text(json.dumps({"mcpServers": {"sample": {"command": "changed"}}}))
    with pytest.raises(ValueError, match="cambió"):
        await instance._mcp_project_approve(
            {"workspace": str(workspace), "name": "sample", "hash": entry["hash"]}
        )


@pytest.mark.asyncio
async def test_revoke_project_mcp(server):
    instance, workspace = server
    entry = (await instance._mcp_project_get({"workspace": str(workspace)}))["mcps"][0]
    params = {"workspace": str(workspace), "name": "sample", "hash": entry["hash"]}
    await instance._mcp_project_approve(params)
    await instance._mcp_project_revoke(params)
    assert (await instance._mcp_project_get({"workspace": str(workspace)}))["mcps"][0][
        "approved"
    ] is False
