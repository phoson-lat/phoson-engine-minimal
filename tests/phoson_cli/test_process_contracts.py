"""Process-level contracts for non-interactive and degraded terminal modes."""

import io
import sys
import json
import asyncio
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

import phoson_cli.__main__ as main_module
import phoson_cli.warnings_hook as warnings_hook
from phoson_agent import (
    TodoItem,
    NoticeBlock,
    KeyValueBlock,
    ProgressBlock,
    TodoListBlock,
)
from phoson_cli.theme import DARK
from phoson_cli.trace import TraceWriter
from phoson_cli.config import PhosonConfig
from phoson_cli.models import load_models_file
from phoson_agent.models import AgentErrorEvent, AgentToolStartEvent
from phoson_cli.commands import Command, CommandHandler
from phoson_cli.renderer import Renderer, WaitingSpinner
from phoson_cli.plugin_ui import NonInteractivePluginUiService


class _Stdin:
    def __init__(self, text: str = "", *, tty: bool = False) -> None:
        self.text = text
        self.tty = tty
        self.reads = 0

    def isatty(self) -> bool:
        return self.tty

    def read(self, size: int = -1) -> str:
        self.reads += 1
        return self.text


class _UnreadableStdin:
    def isatty(self) -> bool:
        return False

    def read(self, size: int = -1) -> str:
        raise AssertionError("stdin must not be read")


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


@pytest.mark.parametrize(
    "argv",
    [
        ["--version"],
        ["bg", "list"],
        ["--setup"],
        ["--self-update"],
        ["--uninstall"],
        ["plugin", "list"],
    ],
)
def test_immediate_actions_do_not_read_piped_stdin(monkeypatch, argv) -> None:
    stdin = _Stdin("unrelated piped input")
    monkeypatch.setattr(sys, "stdin", stdin)

    options = main_module.parse_args(argv)

    assert stdin.reads == 0
    assert options.task is None


def test_empty_non_tty_stdin_fails_without_constructing_frontend(
    monkeypatch, capsys
) -> None:
    stdin = _Stdin("")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "argv", ["phoson-cli"])
    monkeypatch.setattr(
        main_module,
        "PhosonApp",
        lambda config: pytest.fail("fullscreen frontend constructed"),
    )
    monkeypatch.setattr(
        main_module,
        "PhosonRepl",
        lambda config: pytest.fail("classic frontend constructed"),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    assert "stdin was empty" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--setup", "--self-update", "--uninstall"])
def test_interactive_immediate_actions_reject_non_tty_before_dispatch(
    monkeypatch, capsys, flag
) -> None:
    monkeypatch.setattr(sys, "stdin", _UnreadableStdin())
    monkeypatch.setattr(sys, "argv", ["phoson-cli", flag])
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda: pytest.fail("configuration loaded before TTY validation"),
    )
    monkeypatch.setattr(
        main_module,
        "self_update",
        lambda: pytest.fail("self update dispatched without a TTY"),
    )
    monkeypatch.setattr(
        main_module,
        "uninstall",
        lambda: pytest.fail("uninstall dispatched without a TTY"),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    captured = capsys.readouterr()
    assert exc_info.value.code != 0
    assert captured.out == ""
    assert flag in captured.err
    assert "interactive terminal" in captured.err


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "term", "classic"),
    [
        (True, True, "xterm-256color", False),
        (True, False, "xterm-256color", True),
        (False, True, "xterm-256color", True),
        (True, True, "dumb", True),
        (True, True, "", True),
    ],
)
def test_fullscreen_requires_capable_input_and_output(
    monkeypatch, stdin_tty, stdout_tty, term, classic
) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=stdin_tty))
    monkeypatch.setattr(sys, "stdout", _Stream(tty=stdout_tty))
    if term:
        monkeypatch.setenv("TERM", term)
    else:
        monkeypatch.delenv("TERM", raising=False)

    assert main_module._should_use_classic(main_module.CliOptions()) is classic


def test_noninteractive_plugin_blocks_are_deterministic_diagnostics_only() -> None:
    diagnostics = io.StringIO()
    ui = NonInteractivePluginUiService(DARK, stream=diagnostics)
    blocks = [
        NoticeBlock("notice", "warn", "Careful"),
        KeyValueBlock("kv", "Build", (("sha", "abc123"),)),
        TodoListBlock("todos", "Tasks", (TodoItem("one", "Compile", True),)),
        ProgressBlock("progress", "Upload", 2, 3, "steady"),
    ]

    for block in blocks:
        ui.publish(block)

    output = diagnostics.getvalue()
    assert "Careful" in output
    assert "Build" in output and "abc123" in output
    assert "Compile" in output
    assert "Upload 2/3" in output
    assert "<rich." not in output


def test_plugin_blocks_are_structured_records_in_trace_mode() -> None:
    stream = io.StringIO()
    writer = TraceWriter(stream)
    ui = NonInteractivePluginUiService(DARK, trace_writer=writer)

    ui.publish(ProgressBlock("progress", "Upload", 2, 3))

    record = json.loads(stream.getvalue())
    assert record["phoson_trace"] == "diagnostic"
    assert record["source"] == "plugin_ui"
    assert record["block_type"] == "ProgressBlock"
    assert "Upload 2/3" in record["message"]


def test_malformed_models_warning_uses_fullscreen_notice_once(tmp_path) -> None:
    path = tmp_path / "models.json"
    path.write_text("{broken", encoding="utf-8")
    notices: list[str] = []
    restore = warnings_hook.install()
    warnings_hook.set_fullscreen_active(True, notices.append)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            assert load_models_file(path) == {}
            assert load_models_file(path) == {}
    finally:
        warnings_hook.set_fullscreen_active(False)
        restore()

    assert len(notices) == 1
    assert "not valid JSON" in notices[0]


def test_waiting_spinner_emits_nothing_to_redirected_output(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = _Stream(tty=False)
    spinner = WaitingSpinner(Console(file=stream, force_terminal=True))

    spinner.start("thinking")
    spinner.update("working")
    spinner.stop()

    assert stream.getvalue() == ""


def test_waiting_spinner_emits_nothing_for_dumb_or_no_color(monkeypatch) -> None:
    for env in ({"TERM": "dumb"}, {"TERM": "xterm", "NO_COLOR": "1"}):
        monkeypatch.delenv("NO_COLOR", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        stream = _Stream(tty=True)
        spinner = WaitingSpinner(Console(file=stream, force_terminal=True))
        spinner.start("thinking")
        spinner.stop()
        assert stream.getvalue() == ""


@pytest.mark.parametrize(
    ("term", "tty", "no_color"),
    [
        ("xterm-256color", False, False),
        ("dumb", True, False),
        (None, True, False),
        ("xterm-256color", True, True),
    ],
)
def test_subagent_live_is_disabled_but_static_event_remains(
    monkeypatch, term, tty, no_color
) -> None:
    if term is None:
        monkeypatch.delenv("TERM", raising=False)
    else:
        monkeypatch.setenv("TERM", term)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _Stream(tty=tty)
    renderer = Renderer(console=Console(file=stream, color_system=None), theme=DARK)

    renderer._on_tool_start(
        AgentToolStartEvent(tool_name="agents", args={"tasks": ["Inspect"]})
    )
    assert renderer._subagent_spinner._live is None
    assert renderer._subagent_spinner._thread is None
    renderer.stop_subagent_waiting()

    assert "spawning subagents" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()
    assert "\r" not in stream.getvalue()


@pytest.mark.asyncio
async def test_trace_error_stderr_is_jsonl_with_terminal_error(
    tmp_path, capsys
) -> None:
    class FailingEngine:
        def __init__(self, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = []

        async def run(self, messages, config):
            warnings.warn("degraded model metadata")
            raise RuntimeError("agent exploded")

    restore = warnings_hook.install()
    try:
        with (
            patch("phoson_cli.__main__.build_chat", return_value=SimpleNamespace()),
            patch("phoson_agent.AgentEngine", FailingEngine),
        ):
            rc = await main_module._run_oneshot(
                PhosonConfig(provider="ollama", sessions_dir=tmp_path),
                "do it",
                trace=True,
            )
    finally:
        restore()

    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines()]
    assert rc == 1
    assert captured.out == ""
    assert any(record["phoson_trace"] == "diagnostic" for record in records)
    assert records[-1]["phoson_trace"] == "error"
    assert records[-1]["message"] == "agent exploded"


@pytest.mark.asyncio
async def test_agent_error_event_produces_one_terminal_error_after_cleanup(
    tmp_path, capsys
) -> None:
    class WarningPlugin:
        async def aclose(self) -> None:
            warnings.warn("cleanup diagnostic")

    class ErrorEventEngine:
        def __init__(self, *, middlewares, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = [WarningPlugin()]
            self.middlewares = middlewares

        async def run(self, messages, config):
            event = AgentErrorEvent(
                message="agent event failed", code="agent_failure", retryable=True
            )
            for middleware in self.middlewares:
                await middleware.on_agent_event(event)
            raise RuntimeError("agent event failed")

    restore = warnings_hook.install()
    try:
        with (
            patch("phoson_cli.__main__.build_chat", return_value=SimpleNamespace()),
            patch("phoson_agent.AgentEngine", ErrorEventEngine),
        ):
            rc = await main_module._run_oneshot(
                PhosonConfig(provider="ollama", sessions_dir=tmp_path),
                "task",
                trace=True,
            )
    finally:
        restore()

    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines()]
    terminal = [r for r in records if r["phoson_trace"] in {"done", "error"}]
    assert rc == 1
    assert captured.out == ""
    assert len(terminal) == 1
    assert records[-1] == terminal[0]
    assert terminal[0]["code"] == "agent_failure"
    assert terminal[0]["retryable"] is True
    assert any("cleanup diagnostic" in r.get("message", "") for r in records[:-1])


@pytest.mark.asyncio
async def test_oneshot_stdout_is_only_answer_with_plugin_and_warning_diagnostics(
    tmp_path, capsys
) -> None:
    class PublishingEngine:
        def __init__(self, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = []

        async def run(self, messages, config):
            self.context.extra["plugin_ui"].publish(
                KeyValueBlock("build", "Build", (("sha", "abc123"),))
            )
            warnings.warn("using fallback metadata")
            return SimpleNamespace(final_content="FINAL ANSWER\n")

    restore = warnings_hook.install()
    try:
        with (
            patch("phoson_cli.__main__.build_chat", return_value=SimpleNamespace()),
            patch("phoson_agent.AgentEngine", PublishingEngine),
        ):
            rc = await main_module._run_oneshot(
                PhosonConfig(provider="ollama", sessions_dir=tmp_path), "do it"
            )
    finally:
        restore()

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "FINAL ANSWER\n"
    assert "Build" in captured.err and "abc123" in captured.err
    assert "using fallback metadata" in captured.err
    assert "<rich." not in captured.err


def test_oneshot_preconfig_and_preflight_warnings_never_reach_stdout(
    monkeypatch, capsys, tmp_path
) -> None:
    def warning_config() -> PhosonConfig:
        warnings.warn("configuration fallback")
        return PhosonConfig(provider="ollama", sessions_dir=tmp_path)

    def warning_preflight(config) -> object:
        warnings.warn("chat preflight fallback")
        return object()

    async def fake_oneshot(config, task, **kwargs) -> int:
        print("FINAL")
        return 0

    monkeypatch.setattr(sys, "argv", ["phoson-cli", "task"])
    monkeypatch.setattr(main_module, "load_config", warning_config)
    monkeypatch.setattr(main_module, "build_chat", warning_preflight)
    monkeypatch.setattr(main_module, "_run_oneshot", fake_oneshot)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert captured.out == "FINAL\n"
    assert "configuration fallback" in captured.err
    assert "chat preflight fallback" in captured.err


@pytest.mark.asyncio
async def test_oneshot_cleanup_warnings_never_reach_stdout(tmp_path, capsys) -> None:
    class WarningPlugin:
        async def aclose(self) -> None:
            warnings.warn("plugin cleanup fallback")

    class WarningChat:
        async def aclose(self) -> None:
            warnings.warn("chat cleanup fallback")

    class CleanupEngine:
        def __init__(self, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = [WarningPlugin()]

        async def run(self, messages, config):
            return SimpleNamespace(final_content="FINAL")

    restore = warnings_hook.install()
    try:
        with (
            patch("phoson_cli.__main__.build_chat", return_value=WarningChat()),
            patch("phoson_agent.AgentEngine", CleanupEngine),
        ):
            rc = await main_module._run_oneshot(
                PhosonConfig(provider="ollama", sessions_dir=tmp_path), "task"
            )
    finally:
        restore()

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "FINAL\n"
    assert "plugin cleanup fallback" in captured.err
    assert "chat cleanup fallback" in captured.err


@pytest.mark.asyncio
async def test_trace_done_is_last_after_cleanup_diagnostics(tmp_path, capsys) -> None:
    class WarningPlugin:
        async def aclose(self) -> None:
            warnings.warn("trace plugin cleanup")

    class WarningChat:
        async def aclose(self) -> None:
            warnings.warn("trace chat cleanup")

    class SuccessEngine:
        def __init__(self, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = [WarningPlugin()]

        async def run(self, messages, config):
            return SimpleNamespace(final_content="FINAL")

    restore = warnings_hook.install()
    try:
        with (
            patch("phoson_cli.__main__.build_chat", return_value=WarningChat()),
            patch("phoson_agent.AgentEngine", SuccessEngine),
        ):
            rc = await main_module._run_oneshot(
                PhosonConfig(provider="ollama", sessions_dir=tmp_path),
                "task",
                trace=True,
            )
    finally:
        restore()

    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines()]
    assert rc == 0
    assert captured.out == "FINAL\n"
    assert [r["phoson_trace"] for r in records].count("done") == 1
    assert not any(r["phoson_trace"] == "error" for r in records)
    assert records[-1]["phoson_trace"] == "done"
    assert "trace chat cleanup" in records[-2]["message"]


@pytest.mark.asyncio
async def test_trace_timeout_has_json_terminal_error(tmp_path, capsys) -> None:
    class SlowEngine:
        def __init__(self, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = []

        async def run(self, messages, config):
            await asyncio.sleep(1)

    restore = warnings_hook.install()
    try:
        with (
            patch("phoson_cli.__main__.build_chat", return_value=SimpleNamespace()),
            patch("phoson_agent.AgentEngine", SlowEngine),
        ):
            rc = await main_module._run_oneshot(
                PhosonConfig(
                    provider="ollama",
                    sessions_dir=tmp_path,
                    run_budget_seconds=0.001,
                ),
                "do it",
                trace=True,
            )
    finally:
        restore()

    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines()]
    assert rc == 124
    assert captured.out == ""
    assert records[-1]["phoson_trace"] == "error"
    assert records[-1]["code"] == "timeout"


@pytest.mark.asyncio
async def test_trace_direct_cancellation_cleans_up_and_emits_one_terminal_last(
    tmp_path, capsys
) -> None:
    run_started = asyncio.Event()
    cleaned: list[str] = []

    class Plugin:
        async def aclose(self) -> None:
            cleaned.append("plugin")

    class Chat:
        async def aclose(self) -> None:
            cleaned.append("chat")

    class SlowEngine:
        def __init__(self, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = [Plugin()]

        async def run(self, messages, config):
            run_started.set()
            await asyncio.sleep(30)

    restore = warnings_hook.install()
    try:
        with (
            patch("phoson_cli.__main__.build_chat", return_value=Chat()),
            patch("phoson_agent.AgentEngine", SlowEngine),
        ):
            task = asyncio.create_task(
                main_module._run_oneshot(
                    PhosonConfig(provider="ollama", sessions_dir=tmp_path),
                    "do it",
                    trace=True,
                )
            )
            await run_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        restore()

    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    terminal = [r for r in records if r["phoson_trace"] in {"done", "error"}]
    assert cleaned == ["plugin", "chat"]
    assert len(terminal) == 1
    assert records[-1] == terminal[0]
    assert terminal[0]["code"] == "cancelled"


@pytest.mark.asyncio
async def test_oneshot_malformed_json_theme_keeps_stdout_exact(
    tmp_path, capsys, monkeypatch
) -> None:
    import phoson_cli.theme as theme_module

    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(theme_module, "JSON_THEMES_DIR", themes)

    class Engine:
        def __init__(self, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = []

        async def run(self, messages, config):
            return SimpleNamespace(final_content="EXACT ANSWER")

    restore = warnings_hook.install()
    try:
        with (
            patch("phoson_cli.__main__.build_chat", return_value=SimpleNamespace()),
            patch("phoson_agent.AgentEngine", Engine),
        ):
            rc = await main_module._run_oneshot(
                PhosonConfig(provider="ollama", sessions_dir=tmp_path), "task"
            )
    finally:
        restore()

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "EXACT ANSWER\n"
    assert "Skipping invalid JSON theme" in captured.err


@pytest.mark.asyncio
async def test_oneshot_cli_theme_accepts_plugin_theme(tmp_path, capsys) -> None:
    from phoson_agent import Plugin, ThemeExtension, load_plugin, register_loader

    calls = {"factory": 0, "configure": 0, "initialize": 0}

    class ThemePlugin(Plugin):
        accent = ""

        @property
        def name(self) -> str:
            return "theme-plugin"

        def configure(self, config) -> None:
            calls["configure"] += 1
            self.accent = config["accent"]

        def initialize(self) -> None:
            calls["initialize"] += 1

        def get_theme_extension(self) -> ThemeExtension:
            return ThemeExtension(
                name="plugin-neon",
                description="plugin theme",
                tokens={"accent": self.accent},
            )

    def factory(name: str) -> Plugin:
        calls["factory"] += 1
        return ThemePlugin()

    register_loader("review-theme", factory)

    class Engine:
        def __init__(self, *, plugins, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = [load_plugin(spec) for spec in plugins]

        async def run(self, messages, config):
            assert self.context.extra["plugin_ui"].theme.name == "plugin-neon"
            assert self.context.extra["plugin_ui"].theme.accent == "cyan"
            return SimpleNamespace(final_content="plugin theme works")

    config = PhosonConfig(
        provider="ollama",
        theme="plugin-neon",
        cli_theme="plugin-neon",
        plugins=[{"name": "review-theme:plugin", "config": {"accent": "cyan"}}],
        sessions_dir=tmp_path,
    )
    with (
        patch("phoson_cli.__main__.build_chat", return_value=SimpleNamespace()),
        patch("phoson_agent.AgentEngine", Engine),
    ):
        rc = await main_module._run_oneshot(config, "task")

    captured = capsys.readouterr()
    assert rc == 0
    assert calls == {"factory": 1, "configure": 1, "initialize": 1}
    assert captured.out == "plugin theme works\n"
    assert "Unknown theme" not in captured.err


@pytest.mark.asyncio
async def test_oneshot_unknown_cli_theme_is_usage_error(tmp_path, capsys) -> None:
    constructed = 0

    class Engine:
        def __init__(self, **kwargs) -> None:
            nonlocal constructed
            constructed += 1
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = []

        async def run(self, messages, config):
            pytest.fail("deferred invalid theme reached the agent run")

    config = PhosonConfig(
        provider="ollama",
        theme="missing-theme",
        cli_theme="missing-theme",
        sessions_dir=tmp_path,
    )
    with (
        patch("phoson_cli.__main__.build_chat", return_value=SimpleNamespace()),
        patch("phoson_agent.AgentEngine", Engine),
    ):
        rc = await main_module._run_oneshot(config, "task")

    assert rc == 2
    assert constructed == 1
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_oneshot_without_theme_loads_plugin_factory_once(tmp_path) -> None:
    from phoson_agent import Plugin, load_plugin, register_loader

    factory_calls = 0

    class PlainPlugin(Plugin):
        @property
        def name(self) -> str:
            return "plain-plugin"

    def factory(name: str) -> Plugin:
        nonlocal factory_calls
        factory_calls += 1
        return PlainPlugin()

    register_loader("review-plain", factory)

    class Engine:
        def __init__(self, *, plugins, **kwargs) -> None:
            self.context = SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = [load_plugin(spec) for spec in plugins]

        async def run(self, messages, config):
            return SimpleNamespace(final_content="ok")

    config = PhosonConfig(
        provider="ollama",
        plugins=["review-plain:plugin"],
        sessions_dir=tmp_path,
    )
    with (
        patch("phoson_cli.__main__.build_chat", return_value=SimpleNamespace()),
        patch("phoson_agent.AgentEngine", Engine),
    ):
        assert await main_module._run_oneshot(config, "task") == 0

    assert factory_calls == 1


def test_interactive_deferred_invalid_theme_uses_built_runtime_registry(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(
        sys,
        "argv",
        ["phoson-cli", "--classic", "--theme", "plugin-missing"],
    )
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda: PhosonConfig(provider="ollama", sessions_dir=tmp_path),
    )
    monkeypatch.setattr(main_module, "build_chat", lambda config: object())

    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "option --theme expects one of" in captured.err
    assert "Unknown theme" not in captured.err


@pytest.mark.asyncio
async def test_incapable_classic_command_picker_fallbacks_are_actionable(
    monkeypatch,
) -> None:
    from phoson_agent.sessions.models import SessionMeta

    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    session = SessionMeta(
        id="saved-session",
        created_at=now,
        updated_at=now,
        message_count=1,
        total_cost=0.0,
        total_tokens=0,
        step_count=1,
        last_model="model",
    )

    class RendererCapture:
        def __init__(self) -> None:
            self.infos: list[str] = []
            self.warnings: list[str] = []
            self.errors: list[str] = []

        def print_info(self, message: str) -> None:
            self.infos.append(message)

        def print_warn(self, message: str) -> None:
            self.warnings.append(message)

        def print_error(self, message: str) -> None:
            self.errors.append(message)

    class Storage:
        async def list_meta(self, cwd=None):
            return [session]

    renderer = RendererCapture()
    repl = SimpleNamespace(
        picker_capable=False,
        renderer=renderer,
        config=PhosonConfig(provider="ollama"),
        current_model="model",
        subagent_model="submodel",
        theme=DARK,
        storage=Storage(),
        tree=SimpleNamespace(session_id="current-session"),
    )
    handler = CommandHandler(repl)

    async def listings(config, providers):
        pytest.fail("model listing performed before picker capability check")

    monkeypatch.setattr("phoson_cli.commands.list_models_for_providers", listings)
    monkeypatch.setattr(
        "phoson_cli.commands.pick_model",
        lambda *args, **kwargs: pytest.fail("model picker constructed"),
    )
    monkeypatch.setattr(
        "phoson_cli.commands.pick_provider",
        lambda *args, **kwargs: pytest.fail("provider picker constructed"),
    )
    monkeypatch.setattr(
        "phoson_cli.commands.pick_theme",
        lambda *args, **kwargs: pytest.fail("theme picker constructed"),
    )
    monkeypatch.setattr(
        "phoson_cli.session_picker.pick_session",
        lambda *args, **kwargs: pytest.fail("session picker constructed"),
    )

    for name, args in (
        ("/model", ""),
        ("/subagent-model", ""),
        ("/theme", ""),
        ("/provider", ""),
        ("/sessions", "pick"),
    ):
        assert await handler.handle(Command(name=name, args=args)) is True

    messages = "\n".join(renderer.warnings)
    assert len(renderer.warnings) == 5
    assert not any(message == "Cancelled." for message in renderer.infos)
    assert "Use /model <id> or /model list" in messages
    assert "Use /subagent-model <id> or /subagent-model list" in messages
    assert "Use /theme <system|dark|light|ansi|no-color>" in messages
    assert "Use /provider <id>" in messages
    assert "Use /sessions list or /sessions load <#>" in messages


@pytest.mark.asyncio
async def test_old_custom_command_host_pick_model_signature_is_supported(
    monkeypatch,
) -> None:
    class OldHost:
        def __init__(self) -> None:
            self.infos: list[str] = []

        def picker_unavailable(self, usage: str) -> bool:
            return False

        def print_info(self, message: str) -> None:
            self.infos.append(message)

        def print_warn(self, message: str) -> None:
            pass

        def print_error(self, message: str) -> None:
            pass

        async def pick_model(self, models, current_model, *, unavailable=None):
            return SimpleNamespace(
                model_id="next-model",
                provider="ollama",
                cancelled=False,
                unavailable=False,
            )

    repl = SimpleNamespace(
        config=PhosonConfig(provider="ollama", enabled_providers=["ollama"]),
        current_model="old-model",
        subagent_model="old-model",
        engine=SimpleNamespace(context=SimpleNamespace(extra={})),
        set_model=AsyncMock(),
    )
    monkeypatch.setattr(
        "phoson_cli.commands.list_models_for_providers",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    provider="ollama",
                    options=[SimpleNamespace(id="next-model", provider="ollama")],
                    available=True,
                    error="",
                )
            ]
        ),
    )
    monkeypatch.setattr("phoson_cli.commands.save_config", MagicMock())

    assert await CommandHandler(repl, host=OldHost()).handle(
        Command(name="/model", args="")
    )
    repl.set_model.assert_awaited_once_with("next-model", reuse_engine=True)


def test_legacy_host_without_capability_satisfies_command_host_protocol() -> None:
    from phoson_cli.command_host import CommandHost

    class LegacyHost:
        def print_info(self, message):
            pass

        def print_warn(self, message):
            pass

        def print_error(self, message):
            pass

        def print_help(self, entries):
            pass

        def print_renderable(self, renderable):
            pass

        async def pick_model(self, models, current_model, *, unavailable=None):
            pass

        async def pick_provider(self, providers, current_provider):
            pass

        async def pick_theme(self, current_theme, *, detected_theme=None):
            pass

        def apply_theme(self, theme):
            pass

        async def pick_session(self, sessions, current_id):
            pass

        async def confirm(self, prompt):
            pass

        async def run_setup(self):
            pass

    host: CommandHost = LegacyHost()
    assert isinstance(host, CommandHost)


def test_plugin_operational_failure_returns_nonzero(capsys) -> None:
    from phoson_cli.plugin_manager import PluginManagerError

    with patch(
        "phoson_cli.plugin_manager.enable_plugin",
        side_effect=PluginManagerError("missing plugin"),
    ):
        status = main_module._run_plugin_command(["enable", "missing"], PhosonConfig())

    assert status == 1
    assert "missing plugin" in capsys.readouterr().err


def test_plugin_install_does_not_consume_non_tty_confirmation(monkeypatch) -> None:
    stdin = _Stdin("yes\n")
    monkeypatch.setattr(sys, "stdin", stdin)

    status = main_module._run_plugin_command(
        ["install", "demo-package"], PhosonConfig()
    )

    assert status == 0
    assert stdin.reads == 0


def test_trace_empty_stdin_error_is_jsonl(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(""))
    options = main_module.CliOptions(trace=True)

    with pytest.raises(SystemExit) as exc_info:
        main_module._resolve_task(options, TraceWriter())

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == ""
    record = json.loads(captured.err)
    assert record["phoson_trace"] == "error"
    assert record["code"] == "input_error"


@pytest.mark.parametrize("source", ["flag", "environment"])
def test_trace_without_task_on_interactive_tty_fails_before_frontend(
    monkeypatch, capsys, source
) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(sys, "stdout", _Stream(tty=True))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(
        main_module,
        "PhosonApp",
        lambda config: pytest.fail("fullscreen frontend constructed"),
    )
    monkeypatch.setattr(
        main_module,
        "PhosonRepl",
        lambda config: pytest.fail("classic frontend constructed"),
    )
    if source == "flag":
        monkeypatch.setattr(sys, "argv", ["phoson-cli", "--trace"])
        monkeypatch.delenv("PHOSON_TRACE", raising=False)
    else:
        monkeypatch.setattr(sys, "argv", ["phoson-cli"])
        monkeypatch.setenv("PHOSON_TRACE", "1")

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    record = json.loads(capsys.readouterr().err)
    assert exc_info.value.code != 0
    assert record["phoson_trace"] == "error"
    assert "one-shot" in record["message"]


@pytest.mark.parametrize(
    "stream",
    [
        None,
        object(),
        SimpleNamespace(isatty=None),
        SimpleNamespace(isatty=lambda: (_ for _ in ()).throw(OSError("closed"))),
        SimpleNamespace(isatty=lambda: (_ for _ in ()).throw(ValueError("closed"))),
        SimpleNamespace(isatty=lambda: (_ for _ in ()).throw(AttributeError("closed"))),
    ],
)
def test_tty_probe_fails_closed_for_hostile_streams(stream) -> None:
    from phoson_cli.terminal import stream_is_tty

    assert stream_is_tty(stream) is False


def test_missing_stdin_fails_cleanly_and_frontend_probe_fails_closed(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(isatty=lambda: True))
    options = main_module.CliOptions()

    with pytest.raises(SystemExit) as exc_info:
        main_module._resolve_task(options)

    assert exc_info.value.code == 1
    assert "could not read stdin" in capsys.readouterr().err
    assert main_module._should_use_classic(options) is True
