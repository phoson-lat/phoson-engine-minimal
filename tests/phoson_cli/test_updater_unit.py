"""Unit tests for phoson_cli.updater (self-update logic)."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli import updater
from phoson_cli.updater import (
    InstallMode,
    manual_hint,
    upgrade_command,
    detect_install_mode,
    get_current_version,
    is_update_available,
    perform_self_update,
)

# ── Version comparison ────────────────────────────────────────────────────────


def test_is_update_available_comparisons() -> None:
    assert is_update_available("0.4.0", "0.5.0") is True
    assert is_update_available("0.5.0", "0.4.0") is False
    assert is_update_available("0.4.0", "0.4.0") is False
    # Numeric (not lexicographic) comparison.
    assert is_update_available("0.4.0", "0.10.0") is True
    # Pre-releases sort below the release.
    assert is_update_available("0.4.0rc1", "0.4.0") is True
    # "dev" (source checkout) always accepts an update.
    assert is_update_available("dev", "0.1.0") is True
    assert is_update_available("", "0.1.0") is True


def test_get_current_version_falls_back_to_dev(monkeypatch) -> None:
    from importlib.metadata import PackageNotFoundError

    def fake_version(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr("importlib.metadata.version", fake_version)
    assert get_current_version() == "dev"


def test_get_current_version_returns_installed_version() -> None:
    # In this test environment the package metadata is available.
    assert isinstance(get_current_version(), str)
    assert get_current_version() != ""


# ── Latest version from PyPI ──────────────────────────────────────────────────


def _fake_pypi_client(version: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"info": {"version": version}}
    response.raise_for_status.return_value = None

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_get_latest_version_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "phoson_cli.updater.httpx.AsyncClient",
        lambda **kw: _fake_pypi_client("9.9.9"),
    )
    assert await updater.get_latest_version() == "9.9.9"


@pytest.mark.asyncio
async def test_get_latest_version_network_failure() -> None:
    import httpx

    def failing_client(**kw):
        raise httpx.ConnectError("no network")

    with patch("phoson_cli.updater.httpx.AsyncClient", side_effect=failing_client):
        assert await updater.get_latest_version() is None


@pytest.mark.asyncio
async def test_get_latest_version_bad_payload() -> None:
    response = MagicMock()
    response.json.return_value = {"unexpected": True}
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("phoson_cli.updater.httpx.AsyncClient", return_value=client):
        assert await updater.get_latest_version() is None


# ── Install-mode detection ────────────────────────────────────────────────────


def test_detect_install_mode_uv_tool(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "prefix",
        "/home/u/.local/share/uv/tools/phoson-engine-minimal",
    )
    assert detect_install_mode() == InstallMode.UV_TOOL


def test_detect_install_mode_uvx(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "prefix",
        "/home/u/.cache/uv/archive-v0/abc123",
    )
    assert detect_install_mode() == InstallMode.UVX


def test_detect_install_mode_source(monkeypatch, tmp_path) -> None:
    checkout = tmp_path / "checkout"
    pkg = checkout / "my_pkg"
    pkg.mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    monkeypatch.setattr(sys, "prefix", "/opt/venv")
    monkeypatch.setattr(updater, "__file__", str(pkg / "mod.py"))
    assert detect_install_mode() == InstallMode.SOURCE


def test_detect_install_mode_pip_site_packages(monkeypatch) -> None:
    sp = Path("/usr/lib/python3.12/site-packages/phoson_cli")
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(updater, "__file__", str(sp / "updater.py"))
    assert detect_install_mode() == InstallMode.PIP


def test_detect_install_mode_unknown(monkeypatch, tmp_path) -> None:
    lonely = tmp_path / "nowhere" / "phoson_cli"
    lonely.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", "/opt/venv")
    monkeypatch.setattr(sys, "executable", "/opt/venv/bin/python")
    monkeypatch.setattr(updater, "__file__", str(lonely / "updater.py"))
    assert detect_install_mode() == InstallMode.UNKNOWN


# ── Upgrade command per mode ──────────────────────────────────────────────────


def test_upgrade_command_per_mode() -> None:
    assert upgrade_command(InstallMode.UV_TOOL) == [
        "uv",
        "tool",
        "upgrade",
        updater.PACKAGE,
    ]
    pip_cmd = upgrade_command(InstallMode.PIP)
    assert pip_cmd is not None
    assert "install" in pip_cmd and "-U" in pip_cmd
    assert pip_cmd[-1] == updater.PACKAGE
    assert upgrade_command(InstallMode.SOURCE) is None
    assert upgrade_command(InstallMode.UVX) is None
    assert upgrade_command(InstallMode.UNKNOWN) is None


def test_manual_hint_per_mode() -> None:
    assert "uv tool upgrade" in manual_hint(InstallMode.UV_TOOL)
    assert "pip install -U" in manual_hint(InstallMode.PIP)
    assert "git pull" in manual_hint(InstallMode.SOURCE)
    assert "uvx" in manual_hint(InstallMode.UVX)


# ── perform_self_update flows ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_perform_self_update_up_to_date(monkeypatch) -> None:
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.4.0")
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value="0.4.0"))
    run_upgrade = AsyncMock()
    monkeypatch.setattr(updater, "run_upgrade_command", run_upgrade)

    summary = await perform_self_update(assume_yes=True)

    assert "You're up to date (0.4.0)" in summary
    run_upgrade.assert_not_awaited()


@pytest.mark.asyncio
async def test_perform_self_update_offline(monkeypatch) -> None:
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.4.0")
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value=None))
    monkeypatch.setattr(updater, "detect_install_mode", lambda: InstallMode.UV_TOOL)

    summary = await perform_self_update(assume_yes=True)

    assert "Could not check PyPI" in summary
    assert "uv tool upgrade" in summary


@pytest.mark.asyncio
async def test_perform_self_update_source_mode_skips_upgrade(monkeypatch) -> None:
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.3.0")
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value="0.4.0"))
    monkeypatch.setattr(updater, "detect_install_mode", lambda: InstallMode.SOURCE)
    run_upgrade = AsyncMock()
    monkeypatch.setattr(updater, "run_upgrade_command", run_upgrade)

    summary = await perform_self_update(assume_yes=True)

    assert "running from source" in summary
    assert "git pull" in summary
    run_upgrade.assert_not_awaited()


@pytest.mark.asyncio
async def test_perform_self_update_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.3.0")
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value="0.4.0"))
    monkeypatch.setattr(updater, "detect_install_mode", lambda: InstallMode.UV_TOOL)
    monkeypatch.setattr(updater, "_update_confirm", AsyncMock(return_value=False))
    run_upgrade = AsyncMock()
    monkeypatch.setattr(updater, "run_upgrade_command", run_upgrade)

    summary = await perform_self_update(assume_yes=False)

    assert "Update cancelled." in summary
    run_upgrade.assert_not_awaited()


@pytest.mark.asyncio
async def test_perform_self_update_success(monkeypatch) -> None:
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.3.0")
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value="0.4.0"))
    monkeypatch.setattr(updater, "detect_install_mode", lambda: InstallMode.UV_TOOL)
    monkeypatch.setattr(updater, "_update_confirm", AsyncMock(return_value=True))
    monkeypatch.setattr(
        updater, "run_upgrade_command", AsyncMock(return_value=(0, "ok"))
    )

    summary = await perform_self_update(assume_yes=False)

    assert "Running: uv tool upgrade" in summary
    assert "✅ Updated to 0.4.0" in summary
    assert "restart the CLI" in summary


@pytest.mark.asyncio
async def test_perform_self_update_failure_shows_manual_hint(monkeypatch) -> None:
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.3.0")
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value="0.4.0"))
    monkeypatch.setattr(updater, "detect_install_mode", lambda: InstallMode.UV_TOOL)
    monkeypatch.setattr(updater, "_update_confirm", AsyncMock(return_value=True))
    monkeypatch.setattr(
        updater,
        "run_upgrade_command",
        AsyncMock(return_value=(1, "boom: uv not found")),
    )

    summary = await perform_self_update(assume_yes=False)

    assert "Update failed (exit 1)" in summary
    assert "boom: uv not found" in summary
    assert "Try manually: uv tool upgrade" in summary


# ── Command + flag wiring ─────────────────────────────────────────────────────


def test_update_command_registered() -> None:
    from phoson_cli.commands import COMMANDS

    assert "/update" in COMMANDS
    assert "/upgrade" in COMMANDS


def test_self_update_flag_uses_shared_flow(monkeypatch, capsys) -> None:
    import phoson_cli.__main__ as main_module

    calls = []

    async def fake_update(assume_yes: bool = False) -> str:
        calls.append(assume_yes)
        return "You're up to date (0.4.0)."

    monkeypatch.setattr(main_module, "perform_self_update", fake_update)
    main_module.self_update()

    assert calls == [False]  # the flag asks for confirmation
    assert "You're up to date (0.4.0)." in capsys.readouterr().out


def test_self_update_flag_exits_nonzero_on_failure(monkeypatch, capsys) -> None:
    import pytest as _pytest

    import phoson_cli.__main__ as main_module

    async def fake_update(assume_yes: bool = False) -> str:
        return "Update failed (exit 1): boom"

    monkeypatch.setattr(main_module, "perform_self_update", fake_update)
    with _pytest.raises(SystemExit) as exc_info:
        main_module.self_update()
    assert exc_info.value.code == 1
    assert "Update failed" in capsys.readouterr().out
