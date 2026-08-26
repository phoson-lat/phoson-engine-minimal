"""Unit tests for CLI argument parsing and front-end selection.

Covers the pure ``parse_args`` function (IMPROVEMENTS.md D5) and the
``main()`` behaviors that depend on it: ``--version``, the ``--classic``
front-end selection with ``TERM=dumb`` auto-detection (IMPROVEMENTS.md
D2), and the one-off overrides (``--model``/``--provider``/``--theme``/
``--max-turns``).
"""

import sys

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.__main__ import (
    parse_args,
    _apply_overrides,
    _should_use_classic,
)

# ── parse_args: individual flags ─────────────────────────────────────────────


def test_parse_args_version() -> None:
    options = parse_args(["--version"])
    assert options.version is True
    assert options.task is None


def test_parse_args_model_provider_theme() -> None:
    options = parse_args(
        ["--model", "openai/gpt-4o", "--provider", "openai", "--theme", "light"]
    )
    assert options.model == "openai/gpt-4o"
    assert options.provider == "openai"
    assert options.theme == "light"


def test_parse_args_max_turns() -> None:
    options = parse_args(["--max-turns", "12"])
    assert options.max_turns == 12


def test_parse_args_classic_and_alias() -> None:
    assert parse_args(["--classic"]).classic is True
    assert parse_args(["--no-fullscreen"]).classic is True
    assert parse_args([]).classic is False


def test_parse_args_print_flag() -> None:
    assert parse_args(["-p", "task"]).print_mode is True
    assert parse_args(["--print", "task"]).print_mode is True


def test_parse_args_positional_task() -> None:
    options = parse_args(["fix", "the", "failing", "tests"])
    assert options.task == "fix the failing tests"


def test_parse_args_combined_flags_and_task() -> None:
    options = parse_args(
        ["--classic", "--model", "x/y", "-p", "summarize", "this", "repo"]
    )
    assert options.classic is True
    assert options.model == "x/y"
    assert options.print_mode is True
    assert options.task == "summarize this repo"


def test_parse_args_no_task_when_no_args(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert parse_args([]).task is None


# ── parse_args: errors ───────────────────────────────────────────────────────


def test_parse_args_unknown_option_exits_2() -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--bogus"])
    assert exc_info.value.code == 2


def test_parse_args_missing_value_exits_2() -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--model"])
    assert exc_info.value.code == 2


def test_parse_args_bad_max_turns_exits_2() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--max-turns", "abc"])
    with pytest.raises(SystemExit):
        parse_args(["--max-turns", "0"])


def test_parse_args_help_exits_0(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--version" in out
    assert "--classic" in out


# Piped-stdin one-shot parsing is covered in test_oneshot_unit.py.


# ── Overrides (D5) ───────────────────────────────────────────────────────────


def test_apply_overrides_only_sets_given_flags() -> None:
    config = PhosonConfig()
    _apply_overrides(
        config,
        parse_args(["--model", "a/b", "--provider", "anthropic", "--max-turns", "7"]),
    )
    assert config.model == "a/b"
    assert config.provider == "anthropic"
    assert config.max_iterations == 7
    # Untouched flags keep the config value.
    assert config.theme == "dark"


def test_apply_overrides_noop_when_no_flags() -> None:
    config = PhosonConfig()
    _apply_overrides(config, parse_args([]))
    assert config.model == PhosonConfig().model
    assert config.provider == PhosonConfig().provider
    assert config.max_iterations == PhosonConfig().max_iterations


# ── Classic front-end selection (D2) ─────────────────────────────────────────


def test_should_use_classic_explicit_flag_wins(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    assert _should_use_classic(parse_args(["--classic"])) is True


def test_should_use_classic_auto_detects_dumb_term(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "dumb")
    assert _should_use_classic(parse_args([])) is True
    monkeypatch.delenv("TERM")
    assert _should_use_classic(parse_args([])) is True


def test_should_use_classic_not_auto_on_real_terminal(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert _should_use_classic(parse_args([])) is False


def test_should_use_classic_not_auto_without_tty(monkeypatch) -> None:
    """Piped stdin is one-shot mode, never classic — even with TERM=dumb."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setenv("TERM", "dumb")
    assert _should_use_classic(parse_args([])) is False


# ── main() integration ───────────────────────────────────────────────────────


def test_main_version_prints_and_returns(monkeypatch, capsys) -> None:
    import phoson_cli.__main__ as main_module

    monkeypatch.setattr(sys, "argv", ["phoson-cli", "--version"])
    monkeypatch.setattr(main_module, "get_current_version", lambda: "9.9.9")
    main_module.main()
    assert capsys.readouterr().out.strip() == "phoson-cli 9.9.9"


def test_main_classic_flag_launches_repl_not_app(monkeypatch, tmp_path) -> None:
    import phoson_cli.__main__ as main_module

    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[defaults]\n", encoding="utf-8")

    repl_ran = False
    app_ran = False

    class FakeRepl:
        def __init__(self, config):
            self.config = config

        async def run(self):
            nonlocal repl_ran
            repl_ran = True

    class FakeApp:
        def __init__(self, config):
            self.config = config

        async def run_async(self):
            nonlocal app_ran
            app_ran = True

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys, "argv", ["phoson-cli", "--classic"])
    monkeypatch.setattr(main_module, "load_config", lambda: PhosonConfig())
    monkeypatch.setattr(main_module, "build_chat", lambda config: None)
    monkeypatch.setattr(main_module, "PhosonRepl", FakeRepl)
    monkeypatch.setattr(main_module, "PhosonApp", FakeApp)

    main_module.main()

    assert repl_ran
    assert not app_ran


def test_main_dumb_term_auto_selects_classic_with_notice(
    monkeypatch, tmp_path, capsys
) -> None:
    """TERM=dumb on a real TTY: the classic REPL is selected and the user
    is told why (D2 degraded mode)."""
    import phoson_cli.__main__ as main_module

    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[defaults]\n", encoding="utf-8")

    repl_ran = False

    class FakeRepl:
        def __init__(self, config):
            self.config = config

        async def run(self):
            nonlocal repl_ran
            repl_ran = True

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", ["phoson-cli"])
    monkeypatch.setattr(main_module, "load_config", lambda: PhosonConfig())
    monkeypatch.setattr(main_module, "build_chat", lambda config: None)
    monkeypatch.setattr(main_module, "PhosonRepl", FakeRepl)

    main_module.main()

    assert repl_ran
    assert "classic REPL" in capsys.readouterr().err


def test_main_overrides_reach_the_app_config(monkeypatch, tmp_path) -> None:
    """--model/--provider/--theme/--max-turns override the loaded config
    for this run only (D5)."""
    import phoson_cli.__main__ as main_module

    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[defaults]\n", encoding="utf-8")

    seen_configs: list[PhosonConfig] = []

    class FakeApp:
        def __init__(self, config):
            seen_configs.append(config)

        async def run_async(self):
            pass

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "phoson-cli",
            "--model",
            "openai/gpt-4o",
            "--provider",
            "openai",
            "--theme",
            "ansi",
            "--max-turns",
            "9",
        ],
    )
    monkeypatch.setattr(main_module, "load_config", lambda: PhosonConfig())
    monkeypatch.setattr(main_module, "build_chat", lambda config: None)
    monkeypatch.setattr(main_module, "PhosonApp", FakeApp)

    main_module.main()

    assert len(seen_configs) == 1
    config = seen_configs[0]
    assert config.model == "openai/gpt-4o"
    assert config.provider == "openai"
    assert config.theme == "ansi"
    assert config.max_iterations == 9


def test_main_overrides_survive_setup_reload(monkeypatch, tmp_path) -> None:
    """After the setup wizard reloads the config, CLI overrides still win."""
    import phoson_cli.__main__ as main_module

    home = tmp_path / "home"  # deliberately does NOT create ~/.phoson

    seen_configs: list[PhosonConfig] = []

    class FakeApp:
        def __init__(self, config):
            seen_configs.append(config)

        async def run_async(self):
            pass

    async def fake_setup(config):
        return config

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys, "argv", ["phoson-cli", "--model", "x/y"])
    monkeypatch.setattr(
        main_module, "load_config", lambda: PhosonConfig(provider="openrouter")
    )
    monkeypatch.setattr(main_module, "build_chat", lambda config: None)
    monkeypatch.setattr(main_module, "run_install_wizard", fake_setup)
    monkeypatch.setattr(main_module, "PhosonApp", FakeApp)

    main_module.main()

    assert seen_configs[0].model == "x/y"
