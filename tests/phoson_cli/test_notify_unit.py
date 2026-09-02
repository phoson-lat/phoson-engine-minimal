"""Tests for #167: terminal notification on run completion."""

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.notify import (
    NOTIFY_MODES,
    DEFAULT_TITLE,
    emit,
    is_valid_mode,
    build_sequence,
    notify_run_done,
)

# ── build_sequence (pure) ────────────────────────────────────────────────────


def test_bell_is_bel() -> None:
    assert build_sequence("bell") == "\x07"


def test_off_is_empty() -> None:
    assert build_sequence("off") == ""


def test_desktop_has_osc9_and_osc777_and_bel() -> None:
    seq = build_sequence("desktop", title="Phoson finished")
    # OSC 9 (ST-terminated), OSC 777 (ST-terminated), then a BEL fallback.
    assert "\x1b]9;Phoson finished\x1b\\" in seq
    assert "\x1b]777;notify;Phoson finished;Phoson finished\x1b\\" in seq
    assert seq.endswith("\x07")


def test_desktop_default_title() -> None:
    assert DEFAULT_TITLE == "Phoson"
    assert build_sequence("desktop") == build_sequence("desktop", title=DEFAULT_TITLE)


# ── is_valid_mode ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", NOTIFY_MODES)
def test_valid_modes(mode: str) -> None:
    assert is_valid_mode(mode)


def test_case_insensitive_and_stripped() -> None:
    assert is_valid_mode("  BELL ")


@pytest.mark.parametrize("bad", ["", "beep", "BELL2", "be ll", 42, None])
def test_invalid_modes(bad: object) -> None:
    assert not is_valid_mode(bad)


# ── emit (TTY-gated) ─────────────────────────────────────────────────────────


class _TTYFile:
    def __init__(self, tty: bool = True) -> None:
        self.data = ""
        self.flushed = 0
        self._tty = tty

    def write(self, text: str) -> None:
        self.data += text

    def flush(self) -> None:
        self.flushed += 1

    def isatty(self) -> bool:
        return self._tty


def test_emit_bell_to_tty() -> None:
    f = _TTYFile(tty=True)
    assert emit("bell", file=f) is True
    assert f.data == "\x07"
    assert f.flushed == 1


def test_emit_off_writes_nothing() -> None:
    f = _TTYFile(tty=True)
    assert emit("off", file=f) is False
    assert f.data == ""


def test_emit_skips_non_tty() -> None:
    """Piped/redirected output must not be polluted with control sequences."""
    f = _TTYFile(tty=False)
    assert emit("bell", file=f) is False
    assert f.data == ""


def test_emit_invalid_mode_no_op() -> None:
    f = _TTYFile(tty=True)
    assert emit("beep", file=f) is False
    assert f.data == ""


def test_emit_explicit_interactive_overrides_isatty() -> None:
    f = _TTYFile(tty=False)
    # interactive=True forces the write even when isatty() is False.
    assert emit("bell", file=f, interactive=True) is True
    assert f.data == "\x07"
    f2 = _TTYFile(tty=True)
    # interactive=False suppresses even a real TTY (e.g. one-shot piped use).
    assert emit("bell", file=f2, interactive=False) is False
    assert f2.data == ""


# ── notify_run_done (status-gated) ───────────────────────────────────────────


def test_notify_only_on_done() -> None:
    f = _TTYFile(tty=True)
    assert notify_run_done("bell", "done", file=f) is True
    assert f.data == "\x07"


def test_notify_silent_on_error_and_cancel() -> None:
    f = _TTYFile(tty=True)
    assert notify_run_done("bell", "error", file=f) is False
    assert notify_run_done("bell", "cancelled", file=f) is False
    assert f.data == ""


def test_notify_off_mode_silent() -> None:
    f = _TTYFile(tty=True)
    assert notify_run_done("off", "done", file=f) is False
    assert f.data == ""


# ── config wiring ────────────────────────────────────────────────────────────


def test_config_defaults_to_off() -> None:
    # #167: off by default — a bell on every (frequent) coding turn would be
    # intrusive, so the user opts in via /notify or config/env.
    assert PhosonConfig().notify_on_completion == "off"


def test_config_env_override(monkeypatch) -> None:
    import phoson_cli.config as cfgmod

    monkeypatch.setenv("PHOSON_NOTIFY_ON_COMPLETION", "desktop")
    loaded = cfgmod.load_config()
    assert loaded.notify_on_completion == "desktop"


def test_config_invalid_falls_back_to_off(monkeypatch) -> None:
    import warnings

    import phoson_cli.config as cfgmod

    monkeypatch.setenv("PHOSON_NOTIFY_ON_COMPLETION", "beep")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        loaded = cfgmod.load_config()
    assert loaded.notify_on_completion == "off"


# ── one-shot / run_turn integration (controller) ─────────────────────────────


def test_run_turn_done_emits_notification(tmp_path, monkeypatch) -> None:
    """A successful run_turn (both interactive front ends) notifies; an error
    run does not. The write is captured via a TTY fake on sys.stdout."""
    from phoson_agent.models import (
        AgentDoneEvent,
        AgentRunResult,
        AgentErrorEvent,
        AgentStartEvent,
    )

    captured: list[str] = []

    class _TTYOut:
        def write(self, t: str) -> None:
            captured.append(t)

        def flush(self) -> None:
            pass

        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("phoson_cli.notify.sys.stdout", _TTYOut())

    from phoson_cli.config import PhosonConfig
    from phoson_cli.controller import SessionController

    class _Sink:
        def on_user_message(self, *a):
            pass

        def on_event(self, e):
            pass

        def notify(self, *a):
            pass

        def set_session(self, *a):
            pass

        def flush_line(self):
            pass

        def capture_partial_reasoning(self):
            pass

        def take_reasoning(self) -> str:
            return ""

        def on_subagent_progress(self, *a):
            pass

    config = PhosonConfig(
        provider="ollama",
        model="test-model",
        sessions_dir=tmp_path,
        notify_on_completion="bell",
    )

    from unittest.mock import MagicMock, patch

    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        ctrl = SessionController(config, _Sink())

    # Drive a synthetic stream: start + done → run_turn returns "done".
    done = AgentDoneEvent(
        result=AgentRunResult(
            final_content="hi",
            history=[],
            input_messages=[],
            steps=[],
        )
    )
    events = [AgentStartEvent(model="m", message_count=0, max_iterations=1), done]

    async def fake_stream(path, config):
        for e in events:
            yield e

    ctrl.engine.stream = fake_stream  # type: ignore[method-assign]

    import asyncio

    outcome = asyncio.run(ctrl.run_turn("hello"))
    assert outcome.status == "done"
    assert "\x07" in "".join(captured), "done run must emit a bell"

    # Now an error run: must NOT emit.
    captured.clear()
    err = AgentErrorEvent(message="boom", code="unknown")
    events2 = [AgentStartEvent(model="m", message_count=0, max_iterations=1), err]

    async def fake_stream_err(path, config):
        for e in events2:
            yield e

    ctrl.engine.stream = fake_stream_err  # type: ignore[method-assign]
    outcome2 = asyncio.run(ctrl.run_turn("hello again"))
    assert outcome2.status == "error"
    assert "".join(captured) == "", "error run must stay silent"


# ── /notify command ──────────────────────────────────────────────────────────


class _FakeHost:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_warn(self, message: str) -> None:  # pragma: no cover
        pass

    def print_error(self, message: str) -> None:
        self.errors.append(message)


def _notify_repl(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(config=PhosonConfig(sessions_dir=tmp_path))


@pytest.mark.asyncio
async def test_notify_command_sets_and_saves(tmp_path, monkeypatch) -> None:

    from phoson_cli.commands import Command, CommandHandler

    repl = _notify_repl(tmp_path)
    host = _FakeHost()
    handler = CommandHandler(repl, host=host)

    saved: dict[str, object] = {}
    monkeypatch.setattr(
        "phoson_cli.commands.save_config",
        lambda cfg, only_fields=None: saved.update(only_fields=only_fields),
    )

    await handler.handle(Command(name="/notify", args="desktop"))
    assert repl.config.notify_on_completion == "desktop"
    assert saved.get("only_fields") == {"notify_on_completion"}
    assert any("desktop" in i for i in host.infos)


@pytest.mark.asyncio
async def test_notify_command_no_arg_reports_current(tmp_path) -> None:
    from phoson_cli.commands import Command, CommandHandler

    repl = _notify_repl(tmp_path)
    host = _FakeHost()
    handler = CommandHandler(repl, host=host)
    repl.config.notify_on_completion = "bell"

    await handler.handle(Command(name="/notify", args=""))
    assert any("bell" in i for i in host.infos)


@pytest.mark.asyncio
async def test_notify_command_rejects_unknown(tmp_path) -> None:
    from phoson_cli.commands import Command, CommandHandler

    repl = _notify_repl(tmp_path)
    host = _FakeHost()
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/notify", args="beep"))
    assert host.errors, "unknown mode must print an error"
    assert repl.config.notify_on_completion == "off"  # unchanged (default)
