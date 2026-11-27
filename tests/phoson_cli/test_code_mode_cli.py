"""Code mode in the CLI: config, engine wiring, permission card, rendering."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import PhosonConfig, PhosonConfigError, load_config
from phoson_cli.runtime import build_engine_runtime
from phoson_cli.formatting import tool_icon, tool_verb, tool_detail
from phoson_cli.session_utils import build_offload, build_summarizer
from phoson_cli.fullscreen.floats import CARD_MAX_LINES, bash_card_rows
from phoson_cli.permissions_store import build_permission_middleware


class _FakeChat:
    async def aclose(self) -> None:
        pass


def _runtime(config: PhosonConfig):
    return build_engine_runtime(
        config,
        chat=_FakeChat(),  # type: ignore[arg-type]
        plugins=[],
        offload=build_offload(config),
        summarizer=build_summarizer(config),
        permission=build_permission_middleware(on_ask=None),
        main_model="main",
        subagent_model="sub",
    )


# ── config ───────────────────────────────────────────────────────────────


def test_code_mode_is_off_by_default(tmp_path) -> None:
    config = PhosonConfig(sessions_dir=tmp_path)
    assert config.code_mode is False
    names = {t.name for t in _runtime(config).engine.tools}
    assert "run_code" not in names


def test_env_enables_code_mode(monkeypatch) -> None:
    monkeypatch.setenv("PHOSON_CODE_MODE", "1")
    monkeypatch.setenv("PHOSON_CODE_MODE_SANDBOX", "OFF")
    monkeypatch.setenv("PHOSON_CODE_MODE_TIMEOUT", "30")
    config = load_config()
    assert config.code_mode is True
    assert config.code_mode_sandbox == "off"
    assert config.code_mode_timeout == 30.0


def test_invalid_sandbox_mode_is_a_config_error(monkeypatch) -> None:
    monkeypatch.setenv("PHOSON_CODE_MODE_SANDBOX", "sometimes")
    with pytest.raises(PhosonConfigError, match="code_mode_sandbox"):
        load_config()


def test_runtime_registers_run_code_with_config(tmp_path) -> None:
    config = PhosonConfig(
        sessions_dir=tmp_path,
        code_mode=True,
        code_mode_sandbox="off",
        code_mode_timeout=45.0,
    )
    engine = _runtime(config).engine
    run_code = engine._tools_by_name["run_code"]
    assert "45s by default" in run_code.description
    # The built-in tools are listed as script functions.
    assert "tools.read_file(" in run_code.description
    # Sub-agents do not get it: available_tools is the built-in set only.
    assert "run_code" not in engine.context.extra["available_tools"]


def test_system_prompt_guidance_mentions_run_code() -> None:
    from phoson_cli.session_utils import _tool_usage_block

    assert "run_code" in _tool_usage_block({"run_code", "read_file"})
    assert "run_code" not in _tool_usage_block({"read_file"})


# ── permission card ──────────────────────────────────────────────────────


def test_bash_card_unchanged_without_title() -> None:
    text = "".join(t for _s, t in bash_card_rows("ls -la"))
    assert "Run bash command?" in text and "$ ls -la" in text


def test_script_card_shows_every_line_without_shell_prompt() -> None:
    code = "a = 1\nprint(a)"
    text = "".join(t for _s, t in bash_card_rows(code, title="Run script?"))
    assert "Run script?" in text
    assert "  a = 1\n" in text and "  print(a)\n" in text
    assert "$" not in text


def test_script_card_elides_long_scripts() -> None:
    code = "\n".join(f"x{i} = {i}" for i in range(CARD_MAX_LINES + 5))
    text = "".join(t for _s, t in bash_card_rows(code, title="Run script?"))
    assert "… 5 more line(s)" in text


@pytest.mark.asyncio
async def test_controller_shows_the_whole_script(tmp_path) -> None:
    from phoson_cli.controller import SessionController

    with patch("phoson_cli.controller.build_chat"):
        controller = SessionController(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path), MagicMock()
        )
    card = AsyncMock(return_value=True)
    controller.confirmation = MagicMock()
    controller.confirmation.confirm_bash_command = card
    code = "for p in tools.glob(pattern='*.py'):\n    print(p)\n" + "# pad\n" * 40
    try:
        assert await controller._ask_permission("run_code", {"code": code}) is True
    finally:
        await controller.shutdown()
    args, kwargs = card.await_args
    assert args[0] == code  # not the 120-char generic summary
    assert kwargs["title"] == "Run Python script (run_code)?"
    assert kwargs["on_always"] == controller.remember_run_code_allowed


@pytest.mark.asyncio
async def test_always_persists_an_explicit_run_code_allow(
    tmp_path, monkeypatch
) -> None:
    from phoson_cli import permissions_store
    from phoson_cli.controller import SessionController

    monkeypatch.setattr(
        permissions_store, "DEFAULT_PERMISSIONS_FILE", tmp_path / "permissions.json"
    )
    with patch("phoson_cli.controller.build_chat"):
        controller = SessionController(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path), MagicMock()
        )
    try:
        await controller.remember_run_code_allowed("print(1)")
    finally:
        await controller.shutdown()
    policy = permissions_store.load_policy(tmp_path / "permissions.json")
    assert policy.levels["run_code"] == "allow"
    assert policy.evaluate("run_code", None, {"code": "x"})[0] == "allow"


# ── rendering ────────────────────────────────────────────────────────────


def test_run_code_card_detail_is_first_code_line() -> None:
    args = {"code": "\n\n# count TODOs\nprint(1)\n"}
    assert tool_detail("run_code", args) == "# count TODOs"
    assert tool_verb("run_code") == "running code"
    assert tool_icon("run_code") == "λ"
