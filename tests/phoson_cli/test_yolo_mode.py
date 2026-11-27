"""YOLO mode: the permission gate never asks; deny rules still block."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import PhosonConfig, load_config, save_config
from phoson_cli.runtime import effective_safe_mode
from phoson_llm.schemas import ToolCallEvent
from phoson_agent.permissions import (
    SOURCE_YOLO,
    PermissionPolicy,
    ToolBlockedError,
    PermissionMiddleware,
)


def _call(tool: str, **args) -> ToolCallEvent:
    return ToolCallEvent(index=0, tool_call_id="c1", tool_name=tool, args=args)


# ── the gate ─────────────────────────────────────────────────────────────


class TestGate:
    async def test_ask_runs_without_prompt_or_classifier(self):
        on_ask = AsyncMock(return_value=False)
        classifier = AsyncMock()
        decisions = []
        gate = PermissionMiddleware(
            PermissionPolicy(levels={"bash": "ask"}),
            on_ask=on_ask,
            classifier=classifier,
            on_decision=decisions.append,
            yolo=True,
        )
        call = _call("bash", command="rm -rf build")
        assert await gate.on_before_tool(call) is call
        on_ask.assert_not_called()
        classifier.assert_not_called()
        assert decisions[-1].source == SOURCE_YOLO
        assert decisions[-1].allowed is True

    async def test_fails_open_without_a_confirmation_service(self):
        # One-shot has no on_ask: normally fail closed, YOLO runs it.
        gate = PermissionMiddleware(PermissionPolicy(levels={"bash": "ask"}), yolo=True)
        call = _call("bash", command="make")
        assert await gate.on_before_tool(call) is call

    async def test_protective_intents_no_longer_ask(self):
        # Writes outside the workspace ask by default even in auto mode.
        gate = PermissionMiddleware(PermissionPolicy(levels={"*": "allow"}), yolo=True)
        call = _call("write_file", path="/etc/hosts-copy", content="x")
        assert await gate.on_before_tool(call) is call

    async def test_deny_still_blocks(self):
        gate = PermissionMiddleware(
            PermissionPolicy(levels={"web_search": "deny"}), yolo=True
        )
        with pytest.raises(ToolBlockedError, match="denied by permissions policy"):
            await gate.on_before_tool(_call("web_search", query="x"))

    async def test_toggle_is_live(self):
        on_ask = AsyncMock(return_value=False)
        gate = PermissionMiddleware(
            PermissionPolicy(levels={"bash": "ask"}), on_ask=on_ask
        )
        with pytest.raises(ToolBlockedError):
            await gate.on_before_tool(_call("bash", command="ls"))
        gate.yolo = True
        await gate.on_before_tool(_call("bash", command="ls"))
        assert on_ask.await_count == 1


# ── config / flag ────────────────────────────────────────────────────────


def test_off_by_default() -> None:
    assert PhosonConfig().yolo is False


def test_env_enables_it(monkeypatch) -> None:
    monkeypatch.setenv("PHOSON_YOLO", "1")
    assert load_config().yolo is True


def test_config_file_enables_it(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    config_dir = Path("~/.phoson").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text("[defaults]\nyolo = true\n")
    assert load_config().yolo is True


def test_flag_sets_it_for_the_run() -> None:
    from phoson_cli.__main__ import parse_args, _apply_overrides

    options = parse_args(["--yolo", "-p", "do it"])
    assert options.yolo is True
    config = PhosonConfig()
    _apply_overrides(config, options)
    assert config.yolo is True


def test_a_live_toggle_is_never_saved(tmp_path) -> None:
    from pathlib import Path

    config = PhosonConfig(sessions_dir=tmp_path, yolo=True)
    path = save_config(config)
    assert "yolo" not in Path(path).read_text()


def test_yolo_turns_safe_mode_off() -> None:
    assert effective_safe_mode(PhosonConfig(safe_mode=True)) is True
    assert effective_safe_mode(PhosonConfig(safe_mode=True, yolo=True)) is False
    assert effective_safe_mode(PhosonConfig(safe_mode=False, yolo=True)) is False


# ── controller ───────────────────────────────────────────────────────────


def _controller(tmp_path, **config):
    from phoson_cli.controller import SessionController

    with patch("phoson_cli.controller.build_chat"):
        return SessionController(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path, **config),
            MagicMock(),
        )


async def test_controller_starts_in_yolo_from_config(tmp_path) -> None:
    controller = _controller(tmp_path, yolo=True, safe_mode=True)
    try:
        assert controller.yolo is True
        assert controller.permission_middleware.yolo is True
        assert controller.engine.context.extra["safe_mode"] is False
    finally:
        await controller.shutdown()


async def test_set_yolo_updates_gate_and_safe_mode(tmp_path) -> None:
    controller = _controller(tmp_path, safe_mode=True)
    try:
        assert controller.engine.context.extra["safe_mode"] is True
        controller.set_yolo(True)
        assert controller.permission_middleware.yolo is True
        assert controller.engine.context.extra["safe_mode"] is False
        controller.set_yolo(False)
        assert controller.permission_middleware.yolo is False
        assert controller.engine.context.extra["safe_mode"] is True
    finally:
        await controller.shutdown()


# ── /yolo and /permissions ───────────────────────────────────────────────


class _Host:
    def __init__(self) -> None:
        self.info: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.info.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Session:
    def __init__(self) -> None:
        self.yolo = False

    def set_yolo(self, enabled: bool) -> None:
        self.yolo = enabled


def _handler(session):
    from phoson_cli.commands import CommandHandler

    handler = CommandHandler.__new__(CommandHandler)
    handler.session = session
    handler.host = _Host()
    return handler


async def test_yolo_command_toggles_and_reports() -> None:
    from phoson_cli.commands import Command

    session = _Session()
    handler = _handler(session)
    await handler._cmd_yolo(Command(name="/yolo", args=""))
    assert "YOLO mode: off" in handler.host.info[-1]
    await handler._cmd_yolo(Command(name="/yolo", args="on"))
    assert session.yolo is True
    assert "deny rules still block" in handler.host.info[-1]
    await handler._cmd_yolo(Command(name="/yolo", args="off"))
    assert session.yolo is False
    await handler._cmd_yolo(Command(name="/yolo", args="maybe"))
    assert handler.host.errors


async def test_permissions_listing_mentions_yolo(tmp_path, monkeypatch) -> None:
    from phoson_cli import permissions_store
    from phoson_cli.commands import Command

    monkeypatch.setattr(
        permissions_store, "DEFAULT_PERMISSIONS_FILE", tmp_path / "permissions.json"
    )
    session = SimpleNamespace(yolo=True)
    handler = _handler(session)
    await handler._cmd_permissions(Command(name="/permissions", args=""))
    assert any("YOLO mode is on" in line for line in handler.host.info)


def test_yolo_is_registered_and_run_safe() -> None:
    from phoson_cli.command_catalog import COMMAND_SPECS, RUN_SAFE_COMMANDS

    assert any("/yolo" in spec.names for spec in COMMAND_SPECS)
    assert "/yolo" in RUN_SAFE_COMMANDS
