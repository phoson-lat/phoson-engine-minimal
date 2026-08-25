"""Tests for the CLI permission store and /permissions (A1 phase 1)."""

import json
from pathlib import Path

import pytest

from phoson_agent.permissions import LEVEL_ASK, LEVEL_DENY, PermissionPolicy
from phoson_cli.permissions_store import (
    set_level,
    add_pattern,
    load_policy,
    save_policy,
    remove_pattern,
)


@pytest.fixture
def policy_file(tmp_path, monkeypatch) -> Path:
    """Point the durable store at a temp file for the whole test."""
    from phoson_cli import permissions_store

    target = tmp_path / "permissions.json"
    monkeypatch.setattr(permissions_store, "DEFAULT_PERMISSIONS_FILE", target)
    return target


def test_load_policy_returns_empty_when_file_missing(policy_file) -> None:
    policy = load_policy()
    assert policy.levels == {}
    assert policy.allow_patterns == {}


def test_save_then_load_round_trip(policy_file) -> None:
    policy = PermissionPolicy(
        levels={"bash": LEVEL_ASK, "web_search": LEVEL_DENY},
        allow_patterns={"bash": ["git status", "pytest*"]},
    )
    save_policy(policy, policy_file)

    raw = json.loads(policy_file.read_text(encoding="utf-8"))
    assert raw["levels"] == {"bash": "ask", "web_search": "deny"}

    loaded = load_policy(policy_file)
    assert loaded.levels == policy.levels
    assert loaded.allow_patterns == policy.allow_patterns


def test_saved_file_has_restricted_permissions(policy_file) -> None:
    save_policy(PermissionPolicy(), policy_file)
    assert (policy_file.stat().st_mode & 0o777) == 0o600


def test_load_policy_ignores_malformed_file(policy_file) -> None:
    policy_file.write_text("{not json", encoding="utf-8")
    assert load_policy(policy_file).levels == {}


def test_load_policy_drops_invalid_levels(policy_file) -> None:
    policy_file.write_text(
        json.dumps({"levels": {"bash": "sometimes", "web_search": "deny"}}),
        encoding="utf-8",
    )
    assert load_policy(policy_file).levels == {"web_search": "deny"}


def test_set_level_validates_and_normalizes_allow(policy_file) -> None:
    policy = PermissionPolicy(levels={"bash": LEVEL_ASK})
    assert set_level(policy, "bash", "nope") is False
    assert set_level(policy, "bash", "deny") is True
    assert policy.levels["bash"] == LEVEL_DENY

    # allow is the default: the entry is dropped to keep the file minimal.
    assert set_level(policy, "bash", "allow") is True
    assert "bash" not in policy.levels


def test_add_and_remove_pattern(policy_file) -> None:
    policy = PermissionPolicy()
    add_pattern(policy, "bash", "git status")
    add_pattern(policy, "bash", "git status")  # deduplicated
    assert policy.allow_patterns == {"bash": ["git status"]}
    assert remove_pattern(policy, "bash", "git status") is True
    assert remove_pattern(policy, "bash", "git status") is False
    assert "bash" not in policy.allow_patterns


# ── Controller wiring ─────────────────────────────────────────────────────────


def _controller_with(tmp_path):
    """Build a SessionController against a mock chat (no network)."""
    from unittest.mock import MagicMock, patch

    from phoson_cli.config import PhosonConfig
    from phoson_cli.controller import SessionController

    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(provider="ollama", sessions_dir=tmp_path)
        return SessionController(config, sink=MagicMock())


def test_controller_installs_permission_middleware(tmp_path, policy_file) -> None:
    controller = _controller_with(tmp_path)

    names = [type(m).__name__ for m in controller.engine.middlewares]
    assert "PermissionMiddleware" in names
    assert controller.engine.middlewares[1] is controller.permission_middleware


def test_denied_tool_never_reaches_the_handler(tmp_path, policy_file) -> None:
    """End-to-end through the engine: deny bash, run a turn with a fake LLM."""
    import asyncio
    from unittest.mock import MagicMock, patch

    from phoson_cli.config import PhosonConfig
    from phoson_llm.schemas import (
        TokenUsage,
        UsageEvent,
        LLMDoneEvent,
        ToolCallEvent,
    )
    from phoson_cli.controller import SessionController

    policy = load_policy()
    policy.levels["bash"] = LEVEL_DENY
    save_policy(policy, policy_file)

    with patch("phoson_cli.controller.build_chat") as mock_build:
        chat = MagicMock()

        async def fake_stream(history, config, tools=None):
            yield ToolCallEvent(
                index=0,
                tool_call_id="t1",
                tool_name="bash",
                args={"command": "echo hi"},
            )
            yield UsageEvent(usage=TokenUsage(input=1, output=1))
            yield LLMDoneEvent(content="", has_tool_calls=True)

        chat.stream = fake_stream
        mock_build.return_value = chat

        controller = SessionController(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path),
            sink=MagicMock(),
        )
        outcome = asyncio.run(controller.run_turn("run echo hi"))

    assert outcome.status == "error"  # max_iterations without final answer
    # The tool result recorded the actionable refusal.
    partial = controller.engine.get_partial_history()
    tool_results = [
        b.result
        for m in partial
        if isinstance(m.content, list)
        for b in m.content
        if getattr(b, "result", None)
    ]
    assert any("Blocked" in r and "/permissions" in r for r in tool_results)


# ── /permissions command ──────────────────────────────────────────────────────


class _FakeHost:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    async def confirm(self, prompt: str) -> bool:
        return True

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_warn(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def print_help(self, entries) -> None:
        pass


class _DummyRepl:
    def __init__(self) -> None:
        from types import SimpleNamespace

        self.tree = SimpleNamespace(session_id="current")
        self.storage = None
        self.theme = None


def _handler(policy_file):
    from phoson_cli.commands import CommandHandler

    return CommandHandler(_DummyRepl(), _FakeHost())


def _host_of(handler):
    return handler.host


async def test_permissions_command_lists_and_sets(policy_file) -> None:
    from phoson_cli.commands import Command

    handler = _handler(policy_file)
    host = _host_of(handler)

    # Empty state → usage hint.
    await handler.handle(Command(name="/permissions", args=""))
    assert any("No permission rules" in m for m in host.infos)

    # Set a level → persisted.
    await handler.handle(Command(name="/permissions", args="bash ask"))
    assert load_policy().levels == {"bash": LEVEL_ASK}
    assert any("bash → ask · saved" in m for m in host.infos)

    # Listing shows the configured tool.
    await handler.handle(Command(name="/permissions", args=""))
    assert any("bash: ask" in m for m in host.infos)


async def test_permissions_command_rejects_bad_input(policy_file) -> None:
    from phoson_cli.commands import Command

    handler = _handler(policy_file)
    host = _host_of(handler)

    await handler.handle(Command(name="/permissions", args="bash sometimes"))
    assert host.errors and "Usage:" in host.errors[0]

    await handler.handle(Command(name="/permissions", args="onlytool"))
    assert host.errors and "Usage:" in host.errors[-1]
