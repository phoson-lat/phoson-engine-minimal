"""Unit tests for the one-shot (non-interactive) mode."""

import sys
import types
from unittest.mock import patch

import pytest

import phoson_cli.__main__ as main_module
from phoson_cli.config import PhosonConfig
from phoson_cli.__main__ import parse_args, _run_oneshot


class _FakeStdin:
    def __init__(self, text: str = "", tty: bool = False) -> None:
        self._text = text
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def read(self, size: int = -1) -> str:
        return self._text


# ── one-shot task parsing (now part of parse_args) ───────────────────────────


def test_parse_oneshot_positional(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True))
    assert parse_args(["fix", "the tests"]).task == "fix the tests"


def test_parse_oneshot_print_flag(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True))
    assert parse_args(["-p", "a task"]).task == "a task"
    assert parse_args(["--print", "a task"]).task == "a task"


def test_parse_oneshot_piped_stdin(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(text="piped task"))
    assert parse_args([]).task == "piped task"


def test_parse_oneshot_piped_empty_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(text="   "))
    assert parse_args([]).task is None


def test_parse_oneshot_print_flag_with_piped_stdin(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(text="via pipe"))
    assert parse_args(["-p"]).task == "via pipe"


def test_parse_oneshot_print_flag_empty_stdin_exits(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(text=""))
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["-p"])
    assert exc_info.value.code == 1


def test_parse_oneshot_print_flag_interactive_tty_exits(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True))
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["-p"])
    assert exc_info.value.code == 1


def test_parse_oneshot_interactive_default(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True))
    assert parse_args([]).task is None


def test_parse_oneshot_stdin_read_error_is_not_fatal(monkeypatch) -> None:
    """Closed/captured stdin (OSError on read) degrades to interactive mode."""

    class _RaisingStdin:
        def isatty(self) -> bool:
            return False

        def read(self, size: int = -1) -> str:
            raise OSError("no stdin here")

    monkeypatch.setattr(sys, "stdin", _RaisingStdin())
    assert parse_args([]).task is None


# ── _run_oneshot ──────────────────────────────────────────────────────────────


class _FakeChat:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class _FakeEngine:
    def __init__(self, *, fail: bool = False, **_kwargs) -> None:
        self.context = types.SimpleNamespace(extra={})
        self.tools = []
        self._loaded_plugins = []
        self.fail = fail

    async def run(self, messages, config):  # noqa: ANN001
        if self.fail:
            raise RuntimeError("agent exploded")
        return types.SimpleNamespace(final_content="ONE-SHOT RESULT")


@pytest.mark.asyncio
async def test_run_oneshot_success_prints_result(capsys, tmp_path) -> None:
    chat = _FakeChat()
    with (
        patch("phoson_cli.__main__.build_chat", return_value=chat),
        patch("phoson_agent.AgentEngine", _FakeEngine),
    ):
        rc = await _run_oneshot(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path), "do it"
        )

    assert rc == 0
    assert "ONE-SHOT RESULT" in capsys.readouterr().out
    assert chat.closed == 1


@pytest.mark.asyncio
async def test_run_oneshot_error_returns_1(capsys, tmp_path) -> None:
    chat = _FakeChat()
    with (
        patch("phoson_cli.__main__.build_chat", return_value=chat),
        patch("phoson_agent.AgentEngine", lambda **kw: _FakeEngine(fail=True)),
    ):
        rc = await _run_oneshot(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path), "do it"
        )

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error: agent exploded" in err
    assert chat.closed == 1


@pytest.mark.asyncio
async def test_run_oneshot_passes_configured_community_plugins(tmp_path) -> None:
    captured: dict = {}

    class _CapturingEngine(_FakeEngine):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            captured.update(kwargs)

    with (
        patch("phoson_cli.__main__.build_chat", return_value=_FakeChat()),
        patch("phoson_cli.repl.build_plugin_specs", return_value=["entrypoint:demo"]),
        patch("phoson_agent.AgentEngine", _CapturingEngine),
    ):
        rc = await _run_oneshot(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path), "do it"
        )

    assert rc == 0
    assert captured["plugins"] == ["entrypoint:demo"]


async def test_run_oneshot_injects_subagent_context(tmp_path) -> None:
    instance: dict = {}

    class _CapturingEngine(_FakeEngine):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            instance["engine"] = self

    with (
        patch("phoson_cli.__main__.build_chat", return_value=_FakeChat()),
        patch("phoson_agent.AgentEngine", _CapturingEngine),
    ):
        rc = await _run_oneshot(
            PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                subagent_max_parallel=7,
                subagent_timeout_seconds=120.0,
            ),
            "do it",
        )

    assert rc == 0
    extra = instance["engine"].context.extra
    assert extra["subagent_max_parallel"] == 7
    assert extra["subagent_timeout_seconds"] == 120.0
    assert "default_model" in extra and "chat" in extra


# ── main() wiring ─────────────────────────────────────────────────────────────


def test_main_oneshot_exits_with_agent_code(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["phoson-cli", "a task"])
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda: PhosonConfig(provider="ollama", sessions_dir=tmp_path),
    )
    runs: list[str] = []

    async def fake_oneshot(config, task):
        runs.append(task)
        return 0

    monkeypatch.setattr(main_module, "_run_oneshot", fake_oneshot)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 0
    assert runs == ["a task"]


def test_main_oneshot_missing_credential_exits_1(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / ".phoson").mkdir()
    (tmp_path / ".phoson" / "config.toml").write_text(
        '[defaults]\nprovider = "openrouter"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["phoson-cli", "-p", "a task"])
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda: PhosonConfig(provider="openrouter", sessions_dir=tmp_path / "s"),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err
    assert "phoson-cli --setup" in err
