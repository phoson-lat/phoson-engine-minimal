"""SSH plugin: run commands and move files on remote hosts over SSH.

Bundled plugin (issue #169), mirroring the layout of the other bundled
plugins (``phoson_plugin_bgjobs``): a :class:`Plugin` subclass here, a
``create_plugin()`` factory for the path-based loader, and a module-level
``plugin`` instance exported from ``__init__.py``.

Design
------
- **Transport: ``asyncssh``.** Native async, so tool handlers need no thread
  pool; key/agent auth, strict host-key verification and SFTP come for free.
  The dependency is *optional at import time* — this module imports without
  it and ``initialize()`` raises an actionable error, exactly like
  ``phoson_plugin_mcp`` without the ``mcp`` SDK.
- **Hosts are aliases.** Resolution order is plugin config ``hosts`` over
  ``~/.ssh/config`` (read-only), so users keep their normal alias names,
  ``IdentityFile`` and ``ProxyJump``.
- **Security (fail closed).**
  - Strict host-key verification against ``~/.ssh/known_hosts``; the plugin
    never auto-adds a key and never sets ``StrictHostKeyChecking=no``.
  - Key/agent auth only — a password is **never** accepted as a tool
    argument (it would land in the transcript and the audit digest).
  - No PTY (``request_pty=False``), per-command timeout, bounded output.
- **Permissions.** Every mutating tool publishes ``mcp_annotations`` risk
  hints (destructive + open-world) so a tool the user has not explicitly
  configured resolves to ``ask`` and fails closed in one-shot mode. The
  read-only ``ssh_hosts`` tool publishes a read-only hint (``allow``). See
  :mod:`phoson_agent.permissions` and ``docs/cli/permissions.md``.

Per-*host* allow-patterns (``ssh_exec:prod-*``) are **not** supported yet:
allow-patterns only apply to tools declared in the host's match table, which
plugin tools cannot extend. That gap is tracked separately (plugin-declared
permission match-args); the hint-based ``ask`` below is the v1 guarantee.
"""

import shlex
import asyncio
import logging
from typing import Any

from phoson_agent.tool import tool
from phoson_agent.models import AgentTool
from phoson_agent.plugin import Plugin

logger = logging.getLogger(__name__)

try:  # optional dependency, like the `mcp` SDK for phoson_plugin_mcp
    import asyncssh
except ImportError:  # pragma: no cover - exercised via the flag
    asyncssh = None  # type: ignore[assignment]

#: Whether the optional ``asyncssh`` transport is importable.
SSH_AVAILABLE: bool = asyncssh is not None

_DEFAULT_MAX_OUTPUT_CHARS = 8000
_DEFAULT_CONNECT_TIMEOUT = 15.0
_DEFAULT_COMMAND_TIMEOUT = 60.0
_DEFAULT_MAX_TRANSFER_BYTES = 64 * 1024 * 1024

#: Keys accepted in a per-alias ``hosts`` config entry. Anything else is a
#: configuration error rather than blindly forwarded to ``asyncssh.connect``.
_ALLOWED_HOST_KEYS = frozenset(
    {
        "host",
        "port",
        "username",
        "client_keys",
        "known_hosts",
        "config",
        "proxy_command",
        "agent_forwarding",
        "connect_timeout",
        "keepalive_interval",
    }
)

#: Risk hints published on every tool (issue #144 / #227 vocabulary).
_READ_ONLY_HINT: dict[str, Any] = {
    "mcp_annotations": {
        "annotated": True,
        "read_only": True,
        "destructive": False,
        "idempotent": True,
        "open_world": True,
    }
}
_MUTATING_HINT: dict[str, Any] = {
    "mcp_annotations": {
        "annotated": True,
        "read_only": False,
        "destructive": True,
        "idempotent": False,
        "open_world": True,
    }
}


class SshError(Exception):
    """Raised when the SSH transport is unavailable or misconfigured."""


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Return ``(text, truncated)`` capped at ``limit`` characters."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def _decode(value: Any) -> str:
    """Coerce an asyncssh stream result to text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class SshPlugin(Plugin):
    """Run commands and move files on remote SSH hosts.

    Configuration (via ``configure`` / the plugin config dict):

    - ``hosts``: mapping of alias → connection options. Recognised keys are
      ``host``, ``port``, ``username``, ``client_keys``, ``known_hosts``,
      ``config``, ``proxy_command``, ``agent_forwarding``,
      ``connect_timeout`` and ``keepalive_interval``. Aliases not listed here
      are resolved through ``~/.ssh/config`` (and the ``ssh`` defaults).
    - ``known_hosts``: path to the known-hosts file used when an alias does
      not override it (default ``~/.ssh/known_hosts``). Verification is
      always strict; ``None``/empty is rejected.
    - ``connect_timeout``: seconds for a connection attempt (default 15).
    - ``command_timeout``: default seconds for a command (default 60).
    - ``max_output_chars``: output cap per stream (default 8000).
    - ``max_transfer_bytes``: size cap for a single SFTP transfer
      (default 64 MiB).
    """

    def __init__(self) -> None:
        self._hosts: dict[str, dict[str, Any]] = {}
        self._known_hosts: str | None = None
        self._connect_timeout = _DEFAULT_CONNECT_TIMEOUT
        self._command_timeout = _DEFAULT_COMMAND_TIMEOUT
        self._max_output_chars = _DEFAULT_MAX_OUTPUT_CHARS
        self._max_transfer_bytes = _DEFAULT_MAX_TRANSFER_BYTES

        self._connections: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── Plugin contract ───────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "phoson-plugin-ssh"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Run commands and move files on remote hosts over SSH"

    def configure(self, config: dict[str, Any]) -> None:
        """Merge config; keys absent from ``config`` keep their current value.

        Raises:
            SshError: if ``hosts`` contains an unknown option key.
        """
        if "hosts" in config:
            raw_hosts = config["hosts"] or {}
            if not isinstance(raw_hosts, dict):
                raise SshError("'hosts' must be a mapping of alias -> options")
            parsed: dict[str, dict[str, Any]] = {}
            for alias, options in raw_hosts.items():
                if not isinstance(options, dict):
                    raise SshError(f"host {alias!r} must map to an options dict")
                unknown = set(options) - _ALLOWED_HOST_KEYS
                if unknown:
                    raise SshError(
                        f"host {alias!r} has unsupported option(s): "
                        f"{', '.join(sorted(unknown))}"
                    )
                parsed[str(alias)] = dict(options)
            self._hosts = parsed

        if "known_hosts" in config:
            value = config["known_hosts"]
            if value is not None and not str(value).strip():
                raise SshError("'known_hosts' must not be empty (fail closed)")
            self._known_hosts = str(value) if value is not None else None

        if "connect_timeout" in config:
            self._connect_timeout = max(1.0, float(config["connect_timeout"]))
        if "command_timeout" in config:
            self._command_timeout = max(1.0, float(config["command_timeout"]))
        if "max_output_chars" in config:
            self._max_output_chars = max(64, int(config["max_output_chars"]))
        if "max_transfer_bytes" in config:
            self._max_transfer_bytes = max(1, int(config["max_transfer_bytes"]))

    def initialize(self) -> None:
        """Validate that the transport is importable. Connection is lazy.

        Raises:
            SshError: when ``asyncssh`` is not installed, with an actionable
                install hint (mirrors the MCP plugin's missing-SDK behaviour).
        """
        if not SSH_AVAILABLE:
            raise SshError(
                "The SSH plugin requires the optional 'asyncssh' package. "
                "Install with: uv sync --extra ssh  (or: "
                "pip install 'phoson-engine-minimal[ssh]')."
            )
        logger.debug(
            "SSH plugin initialized: %d explicit host(s), known_hosts=%s",
            len(self._hosts),
            self._known_hosts or "~/.ssh/known_hosts",
        )

    async def aclose(self) -> None:
        """Close every pooled connection. Safe to call more than once."""
        connections = list(self._connections.values())
        self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("Failed to close an SSH connection", exc_info=True)
        for conn in connections:
            wait_closed = getattr(conn, "wait_closed", None)
            if wait_closed is None:
                continue
            try:
                await wait_closed()
            except Exception:  # noqa: BLE001 - the peer may already be gone
                logger.debug("Failed awaiting SSH close", exc_info=True)

    def cleanup(self) -> None:
        """Sync fallback: best-effort close when no loop can await us."""
        connections = list(self._connections.values())
        self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("Failed to close an SSH connection", exc_info=True)

    # ── Tools ─────────────────────────────────────────────────────────────

    def get_tools(self) -> list[AgentTool]:
        """Return the SSH tools with their permission risk hints attached."""

        @tool
        async def ssh_hosts() -> str:
            """List the configured SSH host aliases (local, no connection)."""
            return await self._ssh_hosts()

        @tool
        async def ssh_exec(
            host: str,
            command: str,
            cwd: str | None = None,
            timeout_seconds: int | None = None,
        ) -> dict[str, Any]:
            """Run a non-interactive command on a remote host.

            Returns stdout, stderr, the exit status and whether output was
            truncated. No PTY is allocated; password auth is not supported.
            """
            return await self._ssh_exec(host, command, cwd, timeout_seconds)

        @tool
        async def ssh_copy_local_to_remote(
            local_path: str,
            host: str,
            remote_path: str,
        ) -> dict[str, Any]:
            """Copy a local file to a remote host over SFTP."""
            return await self._ssh_transfer(host, local_path, remote_path, upload=True)

        @tool
        async def ssh_copy_remote_to_local(
            host: str,
            remote_path: str,
            local_path: str,
        ) -> dict[str, Any]:
            """Copy a remote file to the local machine over SFTP."""
            return await self._ssh_transfer(host, local_path, remote_path, upload=False)

        for mutating in (
            ssh_exec,
            ssh_copy_local_to_remote,
            ssh_copy_remote_to_local,
        ):
            mutating.metadata = dict(_MUTATING_HINT)
        ssh_hosts.metadata = dict(_READ_ONLY_HINT)

        return [ssh_hosts, ssh_exec, ssh_copy_local_to_remote, ssh_copy_remote_to_local]

    # ── Connection pool ───────────────────────────────────────────────────

    async def _connection(self, alias: str) -> Any:
        """Return a pooled connection to ``alias``, opening it on first use.

        A per-alias lock keeps concurrent tool calls from opening N sockets.
        """
        conn = self._connections.get(alias)
        if conn is not None:
            return conn
        lock = self._locks.get(alias)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[alias] = lock
        async with lock:
            existing = self._connections.get(alias)
            if existing is not None:
                return existing
            conn = await self._connect(alias)
            self._connections[alias] = conn
            return conn

    async def _connect(self, alias: str) -> Any:
        """Open one connection, enforcing strict host-key verification."""
        if not SSH_AVAILABLE or asyncssh is None:
            raise SshError("asyncssh is not installed")
        transport = asyncssh

        overrides = dict(self._hosts.get(alias, {}))
        options: dict[str, Any] = {"host": overrides.pop("host", alias)}
        options.update(overrides)
        # Strict verification is non-negotiable: never let a config entry
        # disable it, and fall back to the user's known_hosts.
        options["known_hosts"] = overrides.get("known_hosts") or self._known_hosts
        if not options["known_hosts"]:
            options["known_hosts"] = "~/.ssh/known_hosts"
        options.setdefault("connect_timeout", self._connect_timeout)
        options.setdefault("keepalive_interval", 30)
        # No plaintext passwords: reject one even if smuggled through config.
        options.pop("password", None)
        options["request_pty"] = False

        try:
            return await asyncio.wait_for(
                transport.connect(**options), timeout=self._connect_timeout
            )
        except TimeoutError as exc:
            raise SshError(
                f"Timed out connecting to host {alias!r} "
                f"after {self._connect_timeout:.0f}s"
            ) from exc

    async def _invalidate(self, alias: str) -> None:
        """Drop a dead pooled connection so the next call reconnects."""
        conn = self._connections.pop(alias, None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - already dead, best effort
            logger.debug("Failed to close a dead SSH connection", exc_info=True)

    # ── Tool implementations ──────────────────────────────────────────────

    async def _ssh_hosts(self) -> str:
        """Text listing of explicitly configured aliases (no connection)."""
        if not self._hosts:
            return (
                "No explicit SSH hosts configured. Aliases are resolved from "
                "~/.ssh/config and must be passed as the 'host' argument."
            )
        lines = []
        for alias in sorted(self._hosts):
            options = self._hosts[alias]
            target = options.get("host", alias)
            user = options.get("username")
            port = options.get("port")
            where = f"{user}@{target}" if user else str(target)
            if port:
                where += f":{port}"
            lines.append(f"- {alias} -> {where}")
        return "Configured SSH hosts:\n" + "\n".join(lines)

    async def _ssh_exec(
        self,
        host: str,
        command: str,
        cwd: str | None,
        timeout_seconds: int | None,
    ) -> dict[str, Any]:
        """Run ``command`` on ``host`` and return a structured result."""
        if not command.strip():
            return {"ok": False, "error": "command must not be empty"}

        effective_command = command
        if cwd:
            effective_command = f"cd {shlex.quote(cwd)} && {command}"

        timeout = float(timeout_seconds) if timeout_seconds else self._command_timeout
        timeout = max(1.0, timeout)

        try:
            conn = await self._connection(host)
            completed = await asyncio.wait_for(
                conn.run(effective_command, check=False, encoding="utf-8"),
                timeout=timeout,
            )
        except TimeoutError:
            return {
                "ok": False,
                "host": host,
                "error": f"Command timed out after {timeout:.0f}s",
            }
        except SshError as exc:
            return {"ok": False, "host": host, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface transport errors to the model
            await self._invalidate(host)
            return {
                "ok": False,
                "host": host,
                "error": f"{type(exc).__name__}: {exc}",
            }

        stdout, out_cut = _truncate(_decode(completed.stdout), self._max_output_chars)
        stderr, err_cut = _truncate(_decode(completed.stderr), self._max_output_chars)

        result: dict[str, Any] = {
            "ok": True,
            "host": host,
            "exit_status": completed.exit_status,
            "stdout": stdout,
            "stderr": stderr,
        }
        if out_cut or err_cut:
            result["truncated"] = True
            result["max_output_chars"] = self._max_output_chars
        return result

    async def _ssh_transfer(
        self,
        host: str,
        local_path: str,
        remote_path: str,
        *,
        upload: bool,
    ) -> dict[str, Any]:
        """Move one file over SFTP, either direction."""
        try:
            conn = await self._connection(host)
            async with conn.start_sftp_client() as sftp:
                if upload:
                    await asyncio.wait_for(
                        sftp.put(local_path, remote_path),
                        timeout=max(60.0, self._command_timeout * 4),
                    )
                else:
                    await asyncio.wait_for(
                        sftp.get(remote_path, local_path),
                        timeout=max(60.0, self._command_timeout * 4),
                    )
        except TimeoutError:
            return {"ok": False, "host": host, "error": "SFTP transfer timed out"}
        except SshError as exc:
            return {"ok": False, "host": host, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface transport errors to the model
            await self._invalidate(host)
            return {
                "ok": False,
                "host": host,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "ok": True,
            "host": host,
            "direction": "upload" if upload else "download",
            "local_path": local_path,
            "remote_path": remote_path,
        }


def create_plugin() -> SshPlugin:
    """Factory for the path-based loader.

    Style: ``path:./phoson_plugin_ssh/_plugin.py``.
    """
    return SshPlugin()


__all__ = [
    "SSH_AVAILABLE",
    "SshError",
    "SshPlugin",
    "create_plugin",
]
