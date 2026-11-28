"""Project MCP servers (``.phoson/mcps.json``) in ``/mcp status|toggle|approve``.

Project servers only load after a per-project approval. These tests pin that
the CLI surfaces pending servers, that approving/revoking from ``/mcp`` (and
the toggle menu) writes a valid trust store and rebuilds the engine, and that
the trust store survives an approve → revoke → approve cycle (it used to be
corrupted by a literal ``\\n`` written on revoke).
"""

import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from phoson_plugin_mcp import _plugin
from phoson_cli.commands import Command
from phoson_cli._mcp_commands import _MCPSubcommands

SPEC = {"transport": "http", "url": "https://example.com/mcp"}


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A project with one ``.phoson/mcps.json`` server; cwd inside it."""
    root = tmp_path / "proj"
    (root / ".phoson").mkdir(parents=True)
    (root / ".phoson" / "mcps.json").write_text(
        json.dumps({"mcpServers": {"overleaf": SPEC}})
    )
    (root / "src").mkdir()
    monkeypatch.chdir(root / "src")
    return root


class _Host:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []
        self.errors: list[str] = []

    def print_info(self, m: str) -> None:
        self.infos.append(m)

    def print_warn(self, m: str) -> None:
        self.warns.append(m)

    def print_error(self, m: str) -> None:
        self.errors.append(m)


def _make(tmp_path: Path, *, picker=None):
    user_config = tmp_path / "user-mcps.json"
    user_config.write_text(json.dumps({"mcpServers": {}}))
    host = _Host()
    if picker is not None:
        host.pick_mcp = picker  # type: ignore[attr-defined]
    plugin = SimpleNamespace(name="phoson-plugin-mcp", tool_name_prefix="mcp")
    plugin.servers = {}
    session = SimpleNamespace(
        config=SimpleNamespace(enable_mcp=True, mcp_config_file=user_config),
        current_model="m",
        set_model=AsyncMock(),
        engine=SimpleNamespace(_loaded_plugins=[plugin], tools=[]),
    )
    return _MCPSubcommands(SimpleNamespace(session=session, host=host)), host


def _trust() -> dict:
    # Strict parse: the store must always be valid JSON.
    return json.loads(_plugin._project_mcp_trust_path().read_text())


# ─── Plugin helpers ──────────────────────────────────────────────────────────


def test_list_project_mcps_walks_up_from_cwd(project):
    root, servers = _plugin.list_project_mcps()
    assert root == project.resolve()
    assert [(s.name, s.approved) for s in servers] == [("overleaf", False)]


def test_find_project_config_skips_user_config(tmp_path, monkeypatch):
    user = tmp_path / ".phoson" / "mcps.json"
    user.parent.mkdir()
    user.write_text("{}")
    monkeypatch.chdir(tmp_path)
    assert _plugin.find_project_mcp_config(exclude=user) is None
    assert _plugin.find_project_mcp_config() == (tmp_path.resolve(), user)


def test_approve_revoke_approve_keeps_store_valid(project):
    _plugin.approve_project_mcp(project, "overleaf", SPEC)
    assert _plugin.revoke_project_mcp(project, "overleaf") is True
    assert _trust() == {}
    _plugin.approve_project_mcp(project, "overleaf", SPEC)
    assert _trust()[str(project.resolve())]["overleaf"]
    assert _plugin._project_mcp_trust_path().read_text().endswith("}\n")


def test_approve_on_corrupt_store_names_the_file(project):
    path = _plugin._project_mcp_trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"x": {}}\\n')
    with pytest.raises(ValueError, match="trusted-project-mcps.json"):
        _plugin.approve_project_mcp(project, "overleaf", SPEC)


# ─── /mcp status ─────────────────────────────────────────────────────────────


async def test_status_lists_pending_project_server(project, tmp_path):
    sub, host = _make(tmp_path)
    await sub.dispatch(Command(name="/mcp", args="status"))
    assert any("pending approval" in w for w in host.warns)
    assert any("overleaf [http] → https://example.com/mcp" in i for i in host.infos)


async def test_status_tags_approved_project_server(project, tmp_path):
    _plugin.approve_project_mcp(project, "overleaf", SPEC)
    sub, host = _make(tmp_path)
    sub.session.engine._loaded_plugins[0].servers = {"overleaf": SPEC}
    await sub.dispatch(Command(name="/mcp", args="status"))
    assert host.warns == []
    assert any("overleaf [http]" in i and "(project)" in i for i in host.infos)


# ─── /mcp approve | revoke ───────────────────────────────────────────────────


async def test_approve_then_revoke(project, tmp_path):
    sub, host = _make(tmp_path)

    await sub.dispatch(Command(name="/mcp", args="approve overleaf"))
    assert host.errors == []
    assert _plugin._project_mcp_is_trusted(project, "overleaf", SPEC)
    sub.session.set_model.assert_awaited_once()

    await sub.dispatch(Command(name="/mcp", args="revoke overleaf"))
    assert not _plugin._project_mcp_is_trusted(project, "overleaf", SPEC)
    assert sub.session.set_model.await_count == 2


async def test_approve_unknown_server(project, tmp_path):
    sub, host = _make(tmp_path)
    await sub.dispatch(Command(name="/mcp", args="approve nope"))
    assert any("No project MCP server 'nope'" in e for e in host.errors)
    sub.session.set_model.assert_not_awaited()


async def test_approve_reports_corrupt_store_without_crashing(project, tmp_path):
    path = _plugin._project_mcp_trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    sub, host = _make(tmp_path)
    await sub.dispatch(Command(name="/mcp", args="approve overleaf"))
    assert any("Could not approve overleaf" in e for e in host.errors)


# ─── /mcp toggle ─────────────────────────────────────────────────────────────


async def test_toggle_by_name_approves_project_server(project, tmp_path):
    sub, host = _make(tmp_path)
    await sub.dispatch(Command(name="/mcp", args="toggle overleaf"))
    assert _plugin._project_mcp_is_trusted(project, "overleaf", SPEC)


async def test_toggle_menu_lists_and_approves_project_server(project, tmp_path):
    seen = {}

    async def picker(servers, *, on_toggle):
        (view,) = servers
        seen["view"] = (view.name, view.scope, view.pending, view.enabled)
        seen["first"] = on_toggle("overleaf", None)
        return SimpleNamespace(changes=1)

    sub, host = _make(tmp_path, picker=picker)
    await sub.dispatch(Command(name="/mcp", args="toggle"))

    assert seen["view"] == ("overleaf", "project", True, False)
    assert seen["first"] is True
    assert _plugin._project_mcp_is_trusted(project, "overleaf", SPEC)
    sub.session.set_model.assert_awaited_once()


def test_picker_renders_project_state():
    from phoson_cli.mcp_picker import McpServerView, build_mcp_picker

    view = McpServerView(name="overleaf", scope="project", pending=True, enabled=False)
    picker = build_mcp_picker([view], on_toggle=lambda s, t: True)
    text = "".join(chunk for _style, chunk in picker._render())  # type: ignore[attr-defined]
    assert "(project · pending approval)" in text


# ─── Name conflicts ──────────────────────────────────────────────────────────


def _with_user_server(sub, name: str) -> None:
    sub.session.config.mcp_config_file.write_text(
        json.dumps({"mcpServers": {name: {"command": "echo"}}})
    )


async def test_status_warns_on_name_conflict(project, tmp_path):
    sub, host = _make(tmp_path)
    _with_user_server(sub, "overleaf")
    await sub.dispatch(Command(name="/mcp", args="status"))
    (conflict,) = [w for w in host.warns if "name conflict" in w]
    assert "'overleaf'" in conflict and "will replace yours" in conflict


async def test_toggle_menu_warns_and_hides_conflicting_project_server(
    project, tmp_path
):
    seen = {}

    async def picker(servers, *, on_toggle):
        seen["names"] = [(v.name, v.scope) for v in servers]
        return SimpleNamespace(changes=0)

    sub, host = _make(tmp_path, picker=picker)
    _with_user_server(sub, "overleaf")
    await sub.dispatch(Command(name="/mcp", args="toggle"))
    assert seen["names"] == [("overleaf", "user")]
    assert any("name conflict" in w for w in host.warns)


async def test_approve_conflicting_server_warns_then_approves(project, tmp_path):
    sub, host = _make(tmp_path)
    _with_user_server(sub, "overleaf")
    await sub.dispatch(Command(name="/mcp", args="approve overleaf"))
    assert any("name conflict" in w for w in host.warns)
    assert _plugin._project_mcp_is_trusted(project, "overleaf", SPEC)


async def test_no_conflict_warning_without_clash(project, tmp_path):
    sub, host = _make(tmp_path)
    _with_user_server(sub, "other")
    await sub.dispatch(Command(name="/mcp", args="status"))
    assert not any("name conflict" in w for w in host.warns)
