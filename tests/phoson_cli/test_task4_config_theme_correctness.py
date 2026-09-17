from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prompt_toolkit.formatted_text import to_plain_text

from phoson_agent import (
    ProgressBlock,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentToolDoneEvent,
    AgentReasoningEvent,
    AgentToolStartEvent,
)
from phoson_cli.theme import DARK, LIGHT, NO_COLOR, load_theme, default_theme_registry
from phoson_cli.config import (
    _SECRET_ENV_BY_KEY,
    PhosonConfig,
    PhosonConfigError,
    load_config,
    save_config,
    enabled_providers_from_config,
)
from phoson_llm.schemas import Message
from phoson_cli.__main__ import CliOptions, parse_args, _apply_overrides
from phoson_cli.plugin_ui import SinkPluginUiService
from phoson_cli.terminal_theme import parse_osc11_response, query_terminal_bg_light
from phoson_cli.fullscreen.sink import FullScreenSink


def test_secret_persistence_map_covers_every_supported_secret() -> None:
    assert _SECRET_ENV_BY_KEY == {
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "github_token": "GITHUB_TOKEN",
        "nvidia_api_key": "NVIDIA_API_KEY",
        "xai_api_key": "XAI_API_KEY",
        "groq_api_key": "GROQ_API_KEY",
        "deepseek_api_key": "DEEPSEEK_API_KEY",
        "together_api_key": "TOGETHER_API_KEY",
        "perplexity_api_key": "PERPLEXITY_API_KEY",
        "azure_openai_api_key": "AZURE_OPENAI_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
        "mistral_api_key": "MISTRAL_API_KEY",
        "fireworks_api_key": "FIREWORKS_API_KEY",
        "cohere_api_key": "COHERE_API_KEY",
        "omniroute_api_key": "OMNIROUTE_API_KEY",
        "vllm_api_key": "VLLM_API_KEY",
    }


def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for env_var in _SECRET_ENV_BY_KEY.values():
        monkeypatch.delenv(env_var, raising=False)
    return home


@pytest.mark.parametrize(("field", "env_var"), _SECRET_ENV_BY_KEY.items())
def test_full_save_never_persists_an_env_only_secret(
    field: str,
    env_var: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv(env_var, "environment-only-secret")

    config = load_config()
    path = save_config(config)

    assert getattr(config, field) == "environment-only-secret"
    assert field not in path.read_text(encoding="utf-8")
    assert "environment-only-secret" not in path.read_text(encoding="utf-8")
    assert path.parent == home / ".phoson"


@pytest.mark.parametrize(("field", "env_var"), _SECRET_ENV_BY_KEY.items())
def test_explicit_file_secret_remains_persistable(
    field: str,
    env_var: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    path.write_text(
        f'[defaults]\nprovider = "ollama"\n{field} = "file-secret"\n',
        encoding="utf-8",
    )

    config = load_config()
    save_config(config)

    assert getattr(config, field) == "file-secret"
    assert f'{field} = "file-secret"' in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("field", "env_var"), _SECRET_ENV_BY_KEY.items())
def test_env_override_does_not_replace_an_explicit_file_secret(
    field: str,
    env_var: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    path.write_text(
        f'[defaults]\nprovider = "ollama"\n{field} = "file-secret"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(env_var, "environment-secret")

    config = load_config()
    save_config(config)

    assert getattr(config, field) == "environment-secret"
    content = path.read_text(encoding="utf-8")
    assert f'{field} = "file-secret"' in content
    assert "environment-secret" not in content


@pytest.mark.parametrize(("field", "env_var"), _SECRET_ENV_BY_KEY.items())
def test_explicit_secret_can_be_saved_while_its_env_var_is_present(
    field: str,
    env_var: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv(env_var, "environment-secret")
    config = PhosonConfig(provider="ollama")
    setattr(config, field, "explicit-user-secret")

    save_config(config, explicit_secret_fields={field})
    monkeypatch.delenv(env_var)

    assert getattr(load_config(), field) == "explicit-user-secret"


@pytest.mark.asyncio
async def test_setup_explicit_secret_survives_env_removal_on_fresh_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from phoson_cli.installer import SetupWizard

    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    wizard = SetupWizard(load_config())
    wizard.enabled_providers = ["openai"]
    wizard.session.prompt_async = AsyncMock(return_value="explicit-user-secret")
    updated = await wizard._configure_providers(wizard.config)
    updated.enabled_providers = ["openai"]
    updated.provider = "openai"
    updated.mark_provider_explicit()

    save_config(updated, explicit_secret_fields=wizard.explicit_secret_fields)
    monkeypatch.delenv("OPENAI_API_KEY")

    reloaded = load_config()
    assert reloaded.openai_api_key == "explicit-user-secret"
    assert enabled_providers_from_config(reloaded) == ["openai"]


@pytest.mark.parametrize(
    "raw",
    ["{", "[]", '"high"', '{"planning": 4}', '{"planning": "turbo"}'],
)
def test_invalid_reasoning_effort_profile_env_is_a_config_error(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("PHOSON_REASONING_EFFORT_PROFILE", raw)

    with pytest.raises(PhosonConfigError, match="PHOSON_REASONING_EFFORT_PROFILE"):
        load_config()


def test_sessions_directory_failure_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("PHOSON_SESSIONS_DIR", str(blocker / "sessions"))

    with pytest.raises(PhosonConfigError, match="sessions directory"):
        load_config()


def test_config_file_read_failure_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    path.write_text("[defaults]\n", encoding="utf-8")
    original_open = Path.open

    def fail_config_open(self: Path, *args, **kwargs):
        if self == path:
            raise OSError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_config_open)
    with pytest.raises(PhosonConfigError, match="configuration file"):
        load_config()


def test_config_directory_write_failure_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home_blocker = tmp_path / "home-is-a-file"
    home_blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home_blocker))

    with pytest.raises(PhosonConfigError, match="configuration directory"):
        save_config(PhosonConfig(provider="ollama"))


def test_save_existing_config_read_failure_is_not_suppressed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    path.write_text('[defaults]\nprovider = "ollama"\n', encoding="utf-8")
    original_open = Path.open

    def fail_read(self: Path, *args, **kwargs):
        if self == path:
            raise OSError("read denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_read)
    with pytest.raises(PhosonConfigError, match="read configuration file"):
        save_config(PhosonConfig(provider="ollama"))


def test_save_atomic_write_failure_keeps_existing_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import phoson_cli.config as config_module

    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    original = '[defaults]\nprovider = "ollama"\n'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        config_module.os,
        "fdopen",
        MagicMock(side_effect=OSError("write denied")),
    )

    with pytest.raises(PhosonConfigError, match="write configuration file"):
        save_config(PhosonConfig(provider="openai"))

    assert path.read_text(encoding="utf-8") == original


def test_save_atomic_replace_failure_keeps_existing_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import phoson_cli.config as config_module

    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    original = '[defaults]\nprovider = "ollama"\n'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        config_module.os,
        "replace",
        MagicMock(side_effect=OSError("replace denied")),
    )

    with pytest.raises(PhosonConfigError, match="replace configuration file"):
        save_config(PhosonConfig(provider="openai"))

    assert path.read_text(encoding="utf-8") == original


def test_save_mode_failure_is_not_suppressed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import phoson_cli.config as config_module

    _isolated_home(monkeypatch, tmp_path)
    real_chmod = config_module.os.chmod

    def fail_owner_only(path, mode):
        if mode == 0o600:
            raise OSError("chmod denied")
        return real_chmod(path, mode)

    monkeypatch.setattr(config_module.os, "chmod", fail_owner_only)
    with pytest.raises(PhosonConfigError, match="permissions"):
        save_config(PhosonConfig(provider="ollama"))


def test_cli_prints_config_errors_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import phoson_cli.__main__ as main_module

    monkeypatch.setattr(main_module.sys, "argv", ["phoson-cli", "task"])
    monkeypatch.setattr(
        main_module,
        "load_config",
        MagicMock(side_effect=PhosonConfigError("bad config")),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "Error: bad config"
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("env_var", "value", "field"),
    [
        ("PHOSON_MAX_ITERATIONS", "0", "max_iterations"),
        ("PHOSON_SUBAGENT_MAX_PARALLEL", "-1", "subagent_max_parallel"),
        ("PHOSON_SUBAGENT_TIMEOUT", "0", "subagent_timeout_seconds"),
        ("PHOSON_SSH_COMMAND_TIMEOUT", "-0.5", "ssh_command_timeout"),
        (
            "PHOSON_PERMISSION_CLASSIFIER_TIMEOUT",
            "0",
            "permission_classifier_timeout_s",
        ),
        ("PHOSON_COMPACT_THRESHOLD", "0", "compact_threshold"),
        ("PHOSON_COMPACT_THRESHOLD", "1.1", "compact_threshold"),
        ("PHOSON_COMPACT_MIN_KEEP", "0", "compact_min_keep_messages"),
    ],
)
def test_invalid_runtime_ranges_from_env_are_rejected(
    env_var: str,
    value: str,
    field: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv(env_var, value)

    with pytest.raises(PhosonConfigError, match=field):
        load_config()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_iterations", "0"),
        ("subagent_max_parallel", "-2"),
        ("subagent_timeout_seconds", "0.0"),
        ("compact_threshold", "1.5"),
        ("compact_min_keep_messages", "0"),
    ],
)
def test_invalid_runtime_ranges_from_toml_are_rejected(
    key: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    (home / ".phoson" / "config.toml").write_text(
        f"[defaults]\n{key} = {value}\n", encoding="utf-8"
    )

    with pytest.raises(PhosonConfigError, match=key):
        load_config()


def test_zero_run_budget_remains_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("PHOSON_RUN_BUDGET_SECONDS", "0")
    assert load_config().run_budget_seconds == 0


@pytest.mark.parametrize("value", ["true", '"not-a-number"'])
def test_toml_float_values_reject_bool_and_string(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    (home / ".phoson" / "config.toml").write_text(
        f"[defaults]\nsubagent_timeout_seconds = {value}\n", encoding="utf-8"
    )

    with pytest.raises(PhosonConfigError, match="subagent_timeout_seconds.*number"):
        load_config()


@pytest.mark.parametrize("value", [True, "4.5", None, object()])
def test_direct_float_config_invalid_types_raise_config_error(value: object) -> None:
    from phoson_cli.config import validate_config

    with pytest.raises(PhosonConfigError, match="subagent_timeout_seconds.*number"):
        validate_config(PhosonConfig(subagent_timeout_seconds=value))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_setup_retries_non_positive_iterations_and_restyles_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from phoson_cli.installer import SetupWizard

    wizard = SetupWizard(PhosonConfig(provider="ollama", sessions_dir=tmp_path))
    monkeypatch.setattr(
        wizard,
        "_prompt_text",
        AsyncMock(side_effect=[str(tmp_path / "sessions"), "0", "7"]),
    )
    monkeypatch.setattr(wizard, "_confirm", AsyncMock(return_value=False))
    monkeypatch.setattr(wizard, "_pick_theme", AsyncMock(return_value="light"))

    result = await wizard._configure_runtime(wizard.config)

    assert result.max_iterations == 7
    assert wizard.theme is LIGHT
    assert any("#6f2dbd" in style for _, style in wizard.session.style.style_rules)


def test_parse_args_defers_theme_registry_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phoson_cli.theme as theme_module

    monkeypatch.setattr(
        theme_module,
        "default_theme_registry",
        MagicMock(side_effect=AssertionError("theme registry loaded while parsing")),
    )

    assert parse_args(["--theme", "Plugin-Neon"]).theme == "plugin-neon"


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_cli_theme_rejects_empty_or_whitespace(value: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--theme", value])

    assert exc_info.value.code == 2


def test_cli_theme_beats_phoson_theme_but_not_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOSON_THEME", "light")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    config = PhosonConfig(theme="system")
    _apply_overrides(config, CliOptions(theme="ansi"))

    assert load_theme(config.theme, cli_value=config.cli_theme).name == "ansi"

    monkeypatch.setenv("NO_COLOR", "1")
    assert load_theme(config.theme, cli_value=config.cli_theme) is NO_COLOR


@pytest.mark.parametrize("value", ["1", " ", "\t"])
def test_any_nonempty_no_color_value_wins(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_COLOR", value)
    monkeypatch.setenv("PHOSON_THEME", "light")
    monkeypatch.delenv("CLICOLOR", raising=False)
    assert load_theme(config_value="light", cli_value="ansi") is NO_COLOR


def test_empty_no_color_value_is_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setenv("PHOSON_THEME", "light")
    monkeypatch.delenv("CLICOLOR", raising=False)
    assert load_theme(config_value="dark") is LIGHT


@pytest.mark.asyncio
@pytest.mark.parametrize("no_color", ["1", " "])
async def test_direct_theme_command_refuses_color_when_no_color_is_set(
    no_color: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from phoson_cli.commands import Command, CommandHandler

    monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setattr("phoson_cli.commands.save_config", MagicMock())
    renderer = MagicMock()
    repl = MagicMock()
    repl.theme = NO_COLOR
    repl.config = PhosonConfig(provider="ollama", theme="no-color")
    repl.renderer = renderer
    repl.theme_registry = default_theme_registry()
    repl.picker_capable = True
    host = MagicMock()

    assert await CommandHandler(repl, host=host).handle(
        Command(name="/theme", args="light")
    )
    host.apply_theme.assert_not_called()
    host.print_error.assert_called_once()
    assert "NO_COLOR" in host.print_error.call_args.args[0]


@pytest.mark.parametrize(
    ("filename", "content", "warning"),
    [
        ("builtin.json", '{"name": "dark", "base": "light"}', "built-in"),
        ("rich.json", '{"name": "bad-rich", "accent": "not a style !"}', "style"),
        ("pt.json", '{"name": "bad-pt", "pt_accent": "not-a-color"}', "style"),
        (
            "syntax.json",
            '{"name": "bad-syntax", "code_theme": "not-a-code-theme"}',
            "style",
        ),
        ("type.json", '{"name": "bad-type", "accent": 7}', "must be a string"),
    ],
)
def test_invalid_json_themes_warn_and_are_not_registered(
    filename: str,
    content: str,
    warning: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import phoson_cli.theme as theme_module

    (tmp_path / filename).write_text(content, encoding="utf-8")
    monkeypatch.setattr(theme_module, "JSON_THEMES_DIR", tmp_path)

    with pytest.warns(UserWarning, match=warning):
        registry = default_theme_registry()

    assert "bad-rich" not in registry.valid_names()
    assert "bad-pt" not in registry.valid_names()
    assert "bad-syntax" not in registry.valid_names()
    assert "bad-type" not in registry.valid_names()
    assert registry.get("dark") is DARK


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"\x1b]11;rgb:ffff/ffff/ffff\x07", True),
        (b"\x1b]11;rgb:0000/0000/0000\x07", False),
        (b"\x1b]11;rgb:f/f/f\x07", True),
        (b"\x1b]11;rgb:8000/8000/8000\x1b\\", True),
        (b"\x1b]11;rgb:7fff/7fff/7fff\x07", False),
        (b"\x1b]11;rgb:ffff/00/nope\x07", None),
    ],
)
def test_standard_osc11_rgb_components_are_scaled(
    raw: bytes, expected: bool | None
) -> None:
    assert parse_osc11_response(raw) is expected


def _build_app(tmp_path: Path):
    from phoson_cli.fullscreen.app import PhosonApp

    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        return PhosonApp(
            PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                history_file=tmp_path / "history.txt",
            )
        )


def test_fullscreen_header_escapes_every_dynamic_html_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _build_app(tmp_path)
    dangerous = "A&B <tag> \"quoted\" 'single'"
    app.repl.current_model = dangerous
    app.repl.config.provider = dangerous
    app.repl.update_hint = dangerous
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: Path("/tmp/A&B <tag>")))
    monkeypatch.setattr(app.repl._controller, "monitor_status", lambda: dangerous)
    monkeypatch.setattr(app.sink, "status_text", lambda: dangerous)
    app._header_cache_key = None

    plain = to_plain_text(app._get_header_text())

    assert dangerous in plain
    assert "A&B <tag>" in plain


def test_runtime_theme_change_updates_plugin_ui_and_new_plugin_blocks(
    tmp_path: Path,
) -> None:
    app = _build_app(tmp_path)
    ui = app.repl._controller.plugin_ui
    assert ui.theme is DARK or ui.theme.name == "system"

    app.apply_theme(LIGHT)
    ui.publish(ProgressBlock("new", "new palette", 1, 1))

    assert ui.theme is LIGHT
    assert app.sink.theme is LIGHT
    assert getattr(app.sink.blocks[-1], "style", None) == LIGHT.muted


def test_sink_theme_switch_has_explicit_append_only_transcript_behavior() -> None:
    sink = FullScreenSink(lambda: None, DARK)
    ui = SinkPluginUiService(sink, DARK)
    ui.publish(ProgressBlock("old", "old palette", 0, 1))
    old = sink.blocks[0]

    sink.set_theme(LIGHT)
    ui.set_theme(LIGHT)
    ui.publish(ProgressBlock("new", "new palette", 1, 1))

    assert sink.blocks[0] is old
    assert getattr(old, "style", None) == DARK.muted
    assert getattr(sink.blocks[1], "style", None) == LIGHT.muted


def test_tool_details_rebuild_uses_the_tool_cards_originating_theme() -> None:
    import phoson_cli.fullscreen.sink as sink_module

    sink = FullScreenSink(lambda: None, DARK)
    sink.on_user_message("hello", Message(role="user", content="hello"))
    user_block = sink.blocks[-1]
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=2))
    sink.on_event(AgentTokenEvent(content="before tool"))
    sink.on_event(
        AgentToolStartEvent(
            tool_name="bash",
            args={"command": "true"},
            tool_call_id="call-1",
        )
    )
    assistant_block = sink.blocks[-2]
    sink.on_event(
        AgentToolDoneEvent(
            tool_name="bash",
            result="ok",
            tool_call_id="call-1",
        )
    )
    sink.set_theme(LIGHT)

    with patch.object(
        sink_module,
        "render_tool_done_line",
        wraps=sink_module.render_tool_done_line,
    ) as render:
        sink.set_tool_details()

    assert render.call_args.args[1] is DARK
    assert sink.blocks[0] is user_block
    assert assistant_block in sink.blocks


def test_reasoning_expansion_uses_the_reasonings_originating_theme() -> None:
    import phoson_cli.fullscreen.sink as sink_module

    sink = FullScreenSink(lambda: None, DARK)
    sink.on_user_message("hello", Message(role="user", content="hello"))
    user_block = sink.blocks[-1]
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=2))
    sink.on_event(AgentTokenEvent(content="answer"))
    sink.on_event(AgentReasoningEvent(content="private thought"))
    sink.on_event(AgentErrorEvent(message="stop", code=None))
    assistant_block = sink.blocks[1]
    sink.set_theme(LIGHT)

    with patch.object(
        sink_module,
        "render_reasoning_expanded",
        wraps=sink_module.render_reasoning_expanded,
    ) as render:
        assert sink.expand_reasoning("node-1", "private thought")

    assert render.call_args.args[1] is DARK
    assert sink.blocks[0] is user_block
    assert sink.blocks[1] is assistant_block


def test_enabled_providers_are_loaded_as_persisted_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    path.write_text(
        "[defaults]\n"
        'provider = "openai"\n'
        'enabled_providers = "openai"\n'
        'openai_api_key = "keep-openai"\n'
        'anthropic_api_key = "keep-anthropic"\n',
        encoding="utf-8",
    )

    config = load_config()

    assert config.anthropic_api_key == "keep-anthropic"
    assert enabled_providers_from_config(config) == ["openai"]
    save_config(config)
    assert 'enabled_providers = "openai"' in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_setup_deselection_persists_without_deleting_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from phoson_cli.installer import SetupWizard

    home = _isolated_home(monkeypatch, tmp_path)
    config = PhosonConfig(
        provider="openai",
        openai_api_key="keep-openai",
        anthropic_api_key="keep-anthropic",
        enabled_providers=["openai", "anthropic"],
        sessions_dir=tmp_path / "sessions",
    )
    save_config(config)
    wizard = SetupWizard(load_config())
    monkeypatch.setattr(wizard, "_prompt_text", AsyncMock(side_effect=["3", ""]))
    monkeypatch.setattr(
        wizard, "_configure_providers", AsyncMock(side_effect=lambda c: c)
    )
    monkeypatch.setattr(
        wizard, "_configure_defaults", AsyncMock(side_effect=lambda c: c)
    )
    monkeypatch.setattr(
        wizard, "_configure_runtime", AsyncMock(side_effect=lambda c: c)
    )
    monkeypatch.setattr(wizard, "_print_banner", MagicMock())
    monkeypatch.setattr(wizard, "_print_intro", MagicMock())
    monkeypatch.setattr(wizard, "_print_summary", MagicMock())
    monkeypatch.setattr(wizard, "_confirm", AsyncMock(return_value=True))

    await wizard.run()
    reloaded = load_config()

    assert reloaded.anthropic_api_key == "keep-anthropic"
    assert enabled_providers_from_config(reloaded) == ["openai"]
    assert 'enabled_providers = "openai"' in (
        home / ".phoson" / "config.toml"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(("field", "env_var"), _SECRET_ENV_BY_KEY.items())
@pytest.mark.parametrize("env_after_load", [None, "changed-environment-secret"])
def test_loaded_env_secret_provenance_survives_environment_changes(
    field: str,
    env_var: str,
    env_after_load: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv(env_var, "loaded-environment-secret")
    config = load_config()
    if env_after_load is None:
        monkeypatch.delenv(env_var)
    else:
        monkeypatch.setenv(env_var, env_after_load)

    path = save_config(config)

    assert config.secret_sources[field] == "env"
    assert field not in path.read_text(encoding="utf-8")
    assert path.parent == home / ".phoson"


@pytest.mark.parametrize(("field", "env_var"), _SECRET_ENV_BY_KEY.items())
@pytest.mark.parametrize("env_after_load", [None, "changed-environment-secret"])
def test_env_override_never_replaces_file_secret_after_environment_changes(
    field: str,
    env_var: str,
    env_after_load: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    path.write_text(
        f'[defaults]\nprovider = "ollama"\n{field} = "file-secret"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(env_var, "loaded-environment-secret")
    config = load_config()
    if env_after_load is None:
        monkeypatch.delenv(env_var)
    else:
        monkeypatch.setenv(env_var, env_after_load)

    save_config(config)

    assert config.secret_sources[field] == "env"
    content = path.read_text(encoding="utf-8")
    assert f'{field} = "file-secret"' in content
    assert "loaded-environment-secret" not in content


def test_loaded_secret_provenance_is_immutable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    config = load_config()

    with pytest.raises(TypeError):
        config.secret_sources["openai_api_key"] = "file"  # type: ignore[index]


def test_secret_provenance_records_file_and_default_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    (home / ".phoson" / "config.toml").write_text(
        '[defaults]\nprovider = "ollama"\nopenai_api_key = "file-secret"\n',
        encoding="utf-8",
    )

    config = load_config()

    assert config.secret_sources["openai_api_key"] == "file"
    assert config.secret_sources["anthropic_api_key"] == "default"


def test_save_does_not_run_a_failing_chmod_after_atomic_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import phoson_cli.config as config_module

    home = _isolated_home(monkeypatch, tmp_path)
    config_path = home / ".phoson" / "config.toml"
    real_chmod = config_module.os.chmod

    def reject_post_commit(path, mode):
        if Path(path) == config_path:
            raise AssertionError("config chmod occurred after atomic replacement")
        return real_chmod(path, mode)

    monkeypatch.setattr(config_module.os, "chmod", reject_post_commit)

    assert save_config(PhosonConfig(provider="ollama")) == config_path
    assert config_path.exists()


def test_toml_string_encoding_round_trips_newlines_and_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    value = 'line 1\nline 2\t\b\f\r\x00\x1f\x7f"\\end'

    path = save_config(PhosonConfig(provider="ollama", model=value))

    assert load_config().model == value
    encoded = path.read_text(encoding="utf-8")
    assert "line 1\\nline 2\\t\\b\\f\\r\\u0000\\u001F\\u007F" in encoded
    assert path.parent == home / ".phoson"


def test_generated_toml_semantic_validation_preserves_original_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import phoson_cli.config as config_module

    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    original = '[defaults]\nprovider = "ollama"\n'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(config_module.tomllib, "loads", lambda value: {"defaults": []})

    with pytest.raises(PhosonConfigError, match="generated configuration"):
        save_config(PhosonConfig(provider="openai"))

    assert path.read_text(encoding="utf-8") == original


def test_generated_toml_encoding_failure_preserves_original_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    original = '[defaults]\nprovider = "ollama"\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(PhosonConfigError, match="generated configuration"):
        save_config(PhosonConfig(provider="ollama", model="bad-\ud800-value"))

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("value", ["true", "1.0", "1.5"])
def test_integer_toml_values_reject_bool_and_float(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    (home / ".phoson" / "config.toml").write_text(
        f"[defaults]\nmax_iterations = {value}\n", encoding="utf-8"
    )

    with pytest.raises(PhosonConfigError, match="max_iterations.*integer"):
        load_config()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"name": 7}', "name must be a string"),
        ('{"name": "typed", "base": []}', "base must be a string"),
    ],
)
def test_json_theme_name_and_base_require_strings(
    content: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import phoson_cli.theme as theme_module

    (tmp_path / "typed.json").write_text(content, encoding="utf-8")
    monkeypatch.setattr(theme_module, "JSON_THEMES_DIR", tmp_path)

    with pytest.warns(UserWarning, match=message):
        assert "typed" not in default_theme_registry().valid_names()


@pytest.mark.parametrize(
    "raw",
    [
        b"\x1b]11;256;0;0\x07",
        b"\x1b]11;-1;0;0\x07",
        b"\x1b]11;1;;2;3\x07",
        b"\x1b]11;1,2,\x07",
    ],
)
def test_osc11_decimal_components_are_strict(raw: bytes) -> None:
    assert parse_osc11_response(raw) is None


def test_osc11_probe_accumulates_echo_and_fragmented_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("phoson_cli.terminal_theme.os.isatty", lambda fd: fd == 7)
    chunks = [
        b"\x1b]11;?\x07",
        b"\x1b]11;rgb:ffff/ffff/ffff\x1b",
        b"\\",
        b"unread",
    ]

    result = query_terminal_bg_light(
        tty_fd=7,
        write=lambda data: None,
        read=lambda: chunks.pop(0) if chunks else b"",
    )

    assert result is True
    assert chunks == [b"unread"]


def test_osc11_probe_accumulates_split_color_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("phoson_cli.terminal_theme.os.isatty", lambda fd: fd == 7)
    chunks = [b"\x1b]11;rgb:00", b"00/0000/0000", b"\x07"]
    assert (
        query_terminal_bg_light(
            tty_fd=7,
            write=lambda data: None,
            read=lambda: chunks.pop(0) if chunks else b"",
        )
        is False
    )
    assert chunks == []


@pytest.mark.parametrize(
    ("enabled", "message"),
    [("", "must contain at least one"), ("openai,unknown", "unknown provider")],
)
def test_invalid_explicit_enabled_providers_are_rejected(
    enabled: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    (home / ".phoson" / "config.toml").write_text(
        f'[defaults]\nprovider = "openai"\nenabled_providers = "{enabled}"\n',
        encoding="utf-8",
    )
    with pytest.raises(PhosonConfigError, match=message):
        load_config()


def test_enabled_provider_aliases_are_canonicalized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    (home / ".phoson" / "config.toml").write_text(
        '[defaults]\nprovider = "google"\nenabled_providers = "google,grok,aws"\n',
        encoding="utf-8",
    )

    config = load_config()

    assert config.provider == "google"
    assert enabled_providers_from_config(config) == ["gemini", "xai", "bedrock"]


def test_persisted_active_provider_must_be_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    (home / ".phoson" / "config.toml").write_text(
        '[defaults]\nprovider = "anthropic"\nenabled_providers = "openai"\n',
        encoding="utf-8",
    )
    with pytest.raises(PhosonConfigError, match="active provider.*not enabled"):
        load_config()


def test_transient_provider_env_override_need_not_be_persistently_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    path.write_text(
        '[defaults]\nprovider = "openai"\nenabled_providers = "openai"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PHOSON_PROVIDER", "anthropic")

    config = load_config()
    save_config(config)

    assert config.provider == "anthropic"
    assert enabled_providers_from_config(config) == ["openai"]
    assert 'provider = "openai"' in path.read_text(encoding="utf-8")


def test_fresh_transient_provider_env_save_writes_coherent_default_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tomllib

    home = _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("PHOSON_PROVIDER", "anthropic")

    config = load_config()
    path = save_config(config)

    assert config.provider == "anthropic"
    with path.open("rb") as stream:
        defaults = tomllib.load(stream)["defaults"]
    assert defaults["provider"] == "openrouter"
    assert defaults["enabled_providers"].split(",") == ["openrouter"]
    assert path.parent == home / ".phoson"


@pytest.mark.parametrize("existing", [False, True])
def test_cli_provider_override_never_persists_an_incoherent_pair(
    existing: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tomllib

    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    if existing:
        path.write_text(
            '[defaults]\nprovider = "openai"\nenabled_providers = "openai"\n',
            encoding="utf-8",
        )
    config = load_config()
    persisted_provider = "openai" if existing else "openrouter"
    _apply_overrides(config, CliOptions(provider="anthropic"))

    save_config(config)

    assert config.provider == "anthropic"
    with path.open("rb") as stream:
        defaults = tomllib.load(stream)["defaults"]
    assert defaults["provider"] == persisted_provider
    assert persisted_provider in defaults["enabled_providers"].split(",")
    assert "anthropic" not in defaults["enabled_providers"].split(",")


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_setup_provider_selection_under_env_is_persisted_explicitly(
    existing: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from phoson_cli.installer import SetupWizard

    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    if existing:
        path.write_text(
            "[defaults]\n"
            'provider = "openai"\n'
            'enabled_providers = "openai,anthropic"\n'
            'openai_api_key = "openai-file-key"\n'
            'anthropic_api_key = "anthropic-file-key"\n',
            encoding="utf-8",
        )
    monkeypatch.setenv("PHOSON_PROVIDER", "anthropic")
    config = load_config()
    wizard = SetupWizard(config)
    wizard.enabled_providers = ["openai", "anthropic"]
    monkeypatch.setattr(
        "phoson_cli.installer.list_available_models", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        wizard,
        "_choose_default_provider",
        AsyncMock(return_value="openai"),
    )
    monkeypatch.setattr(
        wizard,
        "_prompt_text",
        AsyncMock(side_effect=["chosen-model", "chosen-subagent"]),
    )

    updated = await wizard._configure_defaults(config)
    updated.enabled_providers = list(wizard.enabled_providers)
    save_config(updated)

    assert updated._provider_source == "explicit"
    monkeypatch.delenv("PHOSON_PROVIDER")
    reloaded = load_config()
    assert reloaded.provider == "openai"
    assert enabled_providers_from_config(reloaded) == ["openai", "anthropic"]


@pytest.mark.parametrize("max_iterations", [0, 1.0])
def test_runtime_validation_prevents_replacing_existing_config(
    max_iterations: int | float,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    path = home / ".phoson" / "config.toml"
    original = '[defaults]\nprovider = "ollama"\n'
    path.write_text(original, encoding="utf-8")
    config = PhosonConfig(
        provider="ollama",
        max_iterations=max_iterations,  # type: ignore[arg-type]
    )

    with pytest.raises(PhosonConfigError, match="max_iterations"):
        save_config(config)

    assert path.read_text(encoding="utf-8") == original
    assert not path.with_name("config.toml.bak").exists()


@pytest.mark.parametrize("failure", ["provider", "encoding"])
def test_invalid_fresh_save_creates_no_config_artifact(
    failure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "absent-home"
    monkeypatch.setenv("HOME", str(home))
    if failure == "provider":
        config = PhosonConfig(provider="openai", enabled_providers=["anthropic"])
    else:
        config = PhosonConfig(provider="ollama", model="bad-\ud800-value")

    with pytest.raises(PhosonConfigError):
        save_config(config)

    assert not (home / ".phoson").exists()
