"""Tests for the --textual/--classic UI mode flags (Textual migration, phase 0)."""

import sys

import pytest

from phoson_cli.config import PhosonConfig


def _main_module():
    import phoson_cli.__main__ as main_module

    return main_module


def _classic_env(monkeypatch, tmp_path, argv, repl_ran: dict):
    """Wire main() to a recording FakeRepl with a minimal config file."""
    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[defaults]\n", encoding="utf-8")

    class FakeRepl:
        def __init__(self, config):
            self.config = config

        async def run(self):
            repl_ran["ran"] = True

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(_main_module(), "load_config", lambda: PhosonConfig())
    monkeypatch.setattr(_main_module(), "build_chat", lambda config: None)
    monkeypatch.setattr(_main_module(), "PhosonRepl", FakeRepl)


def test_resolve_ui_mode_default_is_classic() -> None:
    main_module = _main_module()
    assert main_module._resolve_ui_mode([]) == ("classic", [])
    assert main_module._resolve_ui_mode(["--classic"]) == ("classic", [])
    mode, rest = main_module._resolve_ui_mode(["--textual", "-p", "task"])
    assert mode == "textual"
    assert rest == ["-p", "task"]


def test_textual_without_dependency_exits_with_friendly_error(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(_main_module(), "_textual_available", lambda: False)
    repl_ran: dict = {}
    _classic_env(monkeypatch, tmp_path, ["phoson-cli", "--textual"], repl_ran)

    with pytest.raises(SystemExit) as exc_info:
        _main_module().main()

    assert exc_info.value.code == 1
    assert repl_ran.get("ran") is not True
    out = capsys.readouterr().out
    assert "optional 'tui' extra" in out
    assert "uv sync --extra tui" in out


def test_textual_with_dependency_launches_tui(monkeypatch, tmp_path) -> None:
    """Phase 3: with the tui extra, --textual hands over to the TUI.

    ``_start_textual_ui`` is stubbed (launching the real app would block);
    the contract under test is that main() starts it with the config and
    never runs the classic REPL.
    """
    monkeypatch.setattr(_main_module(), "_textual_available", lambda: True)
    repl_ran: dict = {}
    started: dict = {}
    _classic_env(monkeypatch, tmp_path, ["phoson-cli", "--textual"], repl_ran)

    def _fake_start(config):
        started["config"] = config
        return True

    monkeypatch.setattr(_main_module(), "_start_textual_ui", _fake_start)
    _main_module().main()

    assert "config" in started  # TUI took over with the loaded config
    assert repl_ran.get("ran") is not True  # classic REPL never started


def test_classic_flag_runs_classic_without_textual(monkeypatch, tmp_path, capsys):
    repl_ran: dict = {}
    _classic_env(monkeypatch, tmp_path, ["phoson-cli", "--classic"], repl_ran)

    def _boom():
        raise AssertionError("--classic must never attempt the Textual path")

    monkeypatch.setattr(_main_module(), "_start_textual_ui", _boom)
    _main_module().main()

    assert repl_ran.get("ran") is True
    assert "under construction" not in capsys.readouterr().out


def test_default_flagless_run_is_classic(monkeypatch, tmp_path):
    repl_ran: dict = {}
    _classic_env(monkeypatch, tmp_path, ["phoson-cli"], repl_ran)

    def _boom():
        raise AssertionError("no flag must never attempt the Textual path")

    monkeypatch.setattr(_main_module(), "_start_textual_ui", _boom)
    _main_module().main()

    assert repl_ran.get("ran") is True


def test_oneshot_task_ignores_ui_flags(monkeypatch, tmp_path):
    """`--textual "task"` must run one-shot with the task, not the flag."""
    main_module = _main_module()

    home = tmp_path / "home"
    config_dir = home / ".phoson"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[defaults]\n", encoding="utf-8")

    captured: dict = {}

    async def fake_oneshot(config, task):
        captured["task"] = task
        return 0

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "argv", ["phoson-cli", "--textual", "do the thing"])
    monkeypatch.setattr(main_module, "load_config", lambda: PhosonConfig())
    monkeypatch.setattr(main_module, "build_chat", lambda config: None)
    monkeypatch.setattr(main_module, "_run_oneshot", fake_oneshot)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 0
    assert captured["task"] == "do the thing"
