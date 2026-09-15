"""Unit tests for the bundled SSH plugin (issue #169).

No network: the transport is monkeypatched, so these run in CI without an
``sshd``. The permission guarantee is asserted at the policy level (the
mutating tools must resolve to ``ask`` from their published risk hints).
"""

import asyncio
from typing import Any

import pytest

from phoson_plugin_ssh import SSH_AVAILABLE, SshError, SshPlugin, create_plugin
from phoson_agent.permissions import (
    LEVEL_ASK,
    LEVEL_ALLOW,
    PermissionPolicy,
    collect_tool_hints,
)


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class _FakeSFTP:
    def __init__(self, sink: list[tuple[str, str, str]]) -> None:
        self._sink = sink

    async def put(self, local: str, remote: str) -> None:
        self._sink.append(("put", local, remote))

    async def get(self, remote: str, local: str) -> None:
        self._sink.append(("get", remote, local))

    async def __aenter__(self) -> "_FakeSFTP":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    def __init__(
        self,
        result: _FakeCompleted | None = None,
        *,
        delay: float = 0.0,
        raise_on_run: Exception | None = None,
        sftp_sink: list[tuple[str, str, str]] | None = None,
    ) -> None:
        self.result = result or _FakeCompleted()
        self.delay = delay
        self.raise_on_run = raise_on_run
        self.sftp_sink = sftp_sink if sftp_sink is not None else []
        self.closed = False
        self.commands: list[str] = []

    async def run(self, command: str, check: bool = False, encoding: str = "") -> Any:
        self.commands.append(command)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        return self.result

    def start_sftp_client(self) -> _FakeSFTP:
        return _FakeSFTP(self.sftp_sink)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _plugin_with_conn(conn: _FakeConn) -> SshPlugin:
    plugin = SshPlugin()

    async def fake_connect(alias: str) -> _FakeConn:
        return conn

    plugin._connect = fake_connect  # type: ignore[method-assign]
    return plugin


def _tool(tools: list[Any], name: str) -> Any:
    return next(t for t in tools if t.name == name)


# ── Identity / contract ──────────────────────────────────────────────────────


def test_identity():
    plugin = SshPlugin()
    assert plugin.name == "phoson-plugin-ssh"
    assert plugin.version == "0.1.0"
    assert plugin.description


def test_create_plugin_factory():
    assert isinstance(create_plugin(), SshPlugin)


def test_get_tools_names():
    names = {t.name for t in SshPlugin().get_tools()}
    assert names == {
        "ssh_hosts",
        "ssh_exec",
        "ssh_copy_local_to_remote",
        "ssh_copy_remote_to_local",
    }


def test_initialize_without_transport_raises(monkeypatch):
    import phoson_plugin_ssh._plugin as mod

    monkeypatch.setattr(mod, "SSH_AVAILABLE", False)
    with pytest.raises(SshError, match="asyncssh"):
        SshPlugin().initialize()


def test_initialize_ok_when_transport_present():
    if not SSH_AVAILABLE:
        pytest.skip("asyncssh not installed")
    SshPlugin().initialize()  # must not raise


# ── Permission guarantee (issue #169 decision a) ─────────────────────────────


def test_mutating_tools_resolve_to_ask_via_hints():
    tools = SshPlugin().get_tools()
    policy = PermissionPolicy(hints=collect_tool_hints(tools))
    assert policy.check("ssh_exec") == LEVEL_ASK
    assert policy.check("ssh_copy_local_to_remote") == LEVEL_ASK
    assert policy.check("ssh_copy_remote_to_local") == LEVEL_ASK


def test_read_only_hosts_tool_resolves_to_allow():
    tools = SshPlugin().get_tools()
    policy = PermissionPolicy(hints=collect_tool_hints(tools))
    assert policy.check("ssh_hosts") == LEVEL_ALLOW


def test_auto_mode_wildcard_lets_mutating_tools_run():
    """Auto mode (`"*": allow`) covers annotated plugin tools, not just bash."""
    from phoson_agent.permissions import (
        LEVEL_DENY,
        SOURCE_MODE,
        WILDCARD_TOOL,
    )

    tools = SshPlugin().get_tools()
    policy = PermissionPolicy(
        levels={WILDCARD_TOOL: LEVEL_ALLOW},
        hints=collect_tool_hints(tools),
    )
    level, source, _ = policy.evaluate("ssh_exec")
    assert level == LEVEL_ALLOW
    assert source == SOURCE_MODE
    assert policy.check("ssh_copy_local_to_remote") == LEVEL_ALLOW
    # A per-tool rule the user wrote still wins over auto.
    policy.levels["ssh_exec"] = LEVEL_DENY
    assert policy.check("ssh_exec") == LEVEL_DENY


def test_mcp_annotations_key_is_used_for_hints():
    tools = {t.name: t for t in SshPlugin().get_tools()}
    raw = tools["ssh_exec"].metadata["mcp_annotations"]
    assert raw["destructive"] is True
    assert raw["read_only"] is False


# ── Configuration ────────────────────────────────────────────────────────────


def test_configure_accepts_known_keys():
    plugin = SshPlugin()
    plugin.configure(
        {
            "hosts": {"web": {"host": "10.0.0.1", "username": "deploy", "port": 22}},
            "command_timeout": 30,
            "max_output_chars": 100,
        }
    )
    assert plugin._hosts["web"]["username"] == "deploy"
    assert plugin._command_timeout == 30
    assert plugin._max_output_chars == 100


def test_configure_rejects_unknown_host_key():
    plugin = SshPlugin()
    with pytest.raises(SshError, match="unsupported"):
        plugin.configure({"hosts": {"web": {"hostname": "x"}}})


def test_configure_rejects_empty_known_hosts():
    with pytest.raises(SshError, match="known_hosts"):
        SshPlugin().configure({"known_hosts": "   "})


def test_configure_rejects_non_dict_hosts():
    with pytest.raises(SshError, match="mapping"):
        SshPlugin().configure({"hosts": ["web"]})


# ── ssh_hosts ────────────────────────────────────────────────────────────────


async def test_ssh_hosts_reports_configured():
    plugin = SshPlugin()
    plugin.configure({"hosts": {"web": {"host": "10.0.0.1", "username": "deploy"}}})
    text = await plugin._ssh_hosts()
    assert "web" in text and "deploy@10.0.0.1" in text


async def test_ssh_hosts_empty_mentions_ssh_config():
    text = await SshPlugin()._ssh_hosts()
    assert "~/.ssh/config" in text


# ── ssh_exec ─────────────────────────────────────────────────────────────────


async def test_ssh_exec_success():
    conn = _FakeConn(_FakeCompleted(stdout="ok\n", stderr="", exit_status=0))
    plugin = _plugin_with_conn(conn)
    result = await plugin._ssh_exec("web", "uptime", None, None)
    assert result["ok"] is True
    assert result["stdout"] == "ok\n"
    assert result["exit_status"] == 0


async def test_ssh_exec_nonzero_exit_is_still_ok_true():
    conn = _FakeConn(_FakeCompleted(stdout="", stderr="boom", exit_status=2))
    plugin = _plugin_with_conn(conn)
    result = await plugin._ssh_exec("web", "false", None, None)
    assert result["ok"] is True
    assert result["exit_status"] == 2
    assert result["stderr"] == "boom"


async def test_ssh_exec_truncates_output():
    conn = _FakeConn(_FakeCompleted(stdout="x" * 5000))
    plugin = _plugin_with_conn(conn)
    plugin.configure({"max_output_chars": 100})
    result = await plugin._ssh_exec("web", "cat big", None, None)
    assert result["truncated"] is True
    assert len(result["stdout"]) == 100


async def test_ssh_exec_empty_command_rejected():
    plugin = _plugin_with_conn(_FakeConn())
    result = await plugin._ssh_exec("web", "   ", None, None)
    assert result["ok"] is False
    assert "empty" in result["error"]


async def test_ssh_exec_cwd_is_prefixed_and_quoted():
    conn = _FakeConn(_FakeCompleted(stdout="ok"))
    plugin = _plugin_with_conn(conn)
    await plugin._ssh_exec("web", "pwd", "/tmp/my dir", None)
    assert conn.commands[0] == "cd '/tmp/my dir' && pwd"


async def test_ssh_exec_timeout():
    # A command that outlives its timeout must fail (not hang) and report it.
    slow = _plugin_with_conn(_FakeConn(delay=1.5))
    result = await slow._ssh_exec("web", "sleep 100", None, 1)
    assert result["ok"] is False
    assert "timed out" in result["error"].lower()


async def test_ssh_exec_respects_configured_default_timeout():
    conn = _FakeConn(_FakeCompleted(stdout="ok"))
    plugin = _plugin_with_conn(conn)
    plugin.configure({"command_timeout": 1})
    # No per-call timeout: the configured default (1s) applies, and the fast
    # fake completes well within it.
    result = await plugin._ssh_exec("web", "uptime", None, None)
    assert result["ok"] is True


async def test_ssh_exec_transport_error_is_surfaced_and_invalidates():
    conn = _FakeConn(raise_on_run=RuntimeError("connection lost"))
    plugin = _plugin_with_conn(conn)
    plugin._connections["web"] = conn
    result = await plugin._ssh_exec("web", "uptime", None, None)
    assert result["ok"] is False
    assert "connection lost" in result["error"]
    assert "web" not in plugin._connections


# ── SFTP ─────────────────────────────────────────────────────────────────────


async def test_upload():
    conn = _FakeConn()
    plugin = _plugin_with_conn(conn)
    result = await plugin._ssh_transfer("web", "/local/a", "/remote/a", upload=True)
    assert result["ok"] is True
    assert conn.sftp_sink == [("put", "/local/a", "/remote/a")]


async def test_download():
    conn = _FakeConn()
    plugin = _plugin_with_conn(conn)
    result = await plugin._ssh_transfer("web", "/local/b", "/remote/b", upload=False)
    assert result["ok"] is True
    assert conn.sftp_sink == [("get", "/remote/b", "/local/b")]


async def test_transfer_transport_error():
    conn = _FakeConn()
    conn.start_sftp_client = lambda: _RaisingSFTP()  # type: ignore[method-assign]
    plugin = _plugin_with_conn(conn)
    result = await plugin._ssh_transfer("web", "/a", "/b", upload=True)
    assert result["ok"] is False
    assert "boom" in result["error"]


class _RaisingSFTP:
    async def __aenter__(self):
        raise RuntimeError("boom")

    async def __aexit__(self, *exc: object) -> bool:
        return False


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def test_aclose_closes_pool():
    conn = _FakeConn()
    plugin = _plugin_with_conn(conn)
    plugin._connections["web"] = conn
    await plugin.aclose()
    assert conn.closed is True
    assert plugin._connections == {}


async def test_aclose_is_idempotent():
    plugin = SshPlugin()
    await plugin.aclose()
    await plugin.aclose()


async def test_connection_is_pooled_within_a_lock():
    plugin = SshPlugin()
    calls = {"n": 0}

    async def fake_connect(alias: str) -> _FakeConn:
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return _FakeConn()

    plugin._connect = fake_connect  # type: ignore[method-assign]
    conns = await asyncio.gather(*(plugin._connection("web") for _ in range(5)))
    assert calls["n"] == 1
    assert all(c is conns[0] for c in conns)


async def test_cleanup_closes_without_await():
    conn = _FakeConn()
    plugin = SshPlugin()
    plugin._connections["web"] = conn
    plugin.cleanup()
    assert conn.closed is True


def test_configure_rejects_password_key():
    # A password must never be accepted as config; it is not an allowed host
    # key, so configure() fails closed rather than forwarding it to asyncssh.
    with pytest.raises(SshError, match="unsupported"):
        SshPlugin().configure({"hosts": {"web": {"host": "h", "password": "s"}}})
