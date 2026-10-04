"""Regression tests for :mod:`phoson_cli.updater`.

These lock in behaviour that is easy to break while refactoring the
self-update flow: PEP 440 ordering, malformed PyPI payloads, subprocess
cancellation/reaping, bounded output, fresh-interpreter verification,
result severity/exit codes, hint wording and literal Rich rendering.

Nothing here touches the network or performs a real update: PyPI and the
upgrade command are mocked, and the only real subprocesses are harmless
``python -c "time.sleep(...)"`` children that we kill ourselves.
"""

import io
import os
import sys
import json
import time
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli import updater
from phoson_cli.updater import (
    InstallMode,
    UpdateResult,
    manual_hint,
    upgrade_command,
    startup_check_due,
    get_latest_version,
    is_update_available,
    perform_self_update,
    print_update_result,
    run_upgrade_command,
    get_installed_version,
    check_for_startup_update,
)

POSIX = os.name == "posix"


# ── PEP 440 ordering ──────────────────────────────────────────────────────────


def test_pep440_epoch_beats_high_release() -> None:
    # An epoch dominates the release segment.
    assert is_update_available("2.0.0", "1!0.1.0") is True
    assert is_update_available("1!0.1.0", "2.0.0") is False


def test_pep440_post_and_pre_releases() -> None:
    assert is_update_available("1.0.0", "1.0.0.post1") is True
    assert is_update_available("1.0.0.post1", "1.0.0") is False
    assert is_update_available("1.0.0rc1", "1.0.0") is True
    assert is_update_available("1.0.0", "1.0.0rc1") is False
    assert is_update_available("1.0.0.dev1", "1.0.0") is True


def test_pep440_local_version_is_newer() -> None:
    assert is_update_available("1.0.0", "1.0.0+local") is True
    assert is_update_available("1.0.0+local", "1.0.0") is False


def test_pep440_equality_and_unparseable_fallback() -> None:
    assert is_update_available("1.0.0", "1.0.0") is False
    # Unparseable latest: conservative "suggest an update" unless identical.
    assert is_update_available("1.0.0", "not-a-version") is True
    assert is_update_available("1.0.0", "1.0.0") is False


# ── Malformed PyPI payloads ───────────────────────────────────────────────────


def _client_with(response: MagicMock) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _response(payload: object, *, json_error: Exception | None = None) -> MagicMock:
    response = MagicMock()
    if json_error is not None:
        response.json.side_effect = json_error
    else:
        response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@pytest.mark.asyncio
async def test_get_latest_version_missing_info_key() -> None:
    with patch(
        "phoson_cli.updater.httpx.AsyncClient",
        return_value=_client_with(_response({"unexpected": True})),
    ):
        assert await get_latest_version() is None


@pytest.mark.asyncio
async def test_get_latest_version_non_string_version() -> None:
    with patch(
        "phoson_cli.updater.httpx.AsyncClient",
        return_value=_client_with(_response({"info": {"version": 123}})),
    ):
        assert await get_latest_version() is None


@pytest.mark.asyncio
async def test_get_latest_version_invalid_version_string() -> None:
    with patch(
        "phoson_cli.updater.httpx.AsyncClient",
        return_value=_client_with(_response({"info": {"version": "not-a-version"}})),
    ):
        assert await get_latest_version() is None


@pytest.mark.asyncio
async def test_get_latest_version_json_decode_error() -> None:
    with patch(
        "phoson_cli.updater.httpx.AsyncClient",
        return_value=_client_with(_response(None, json_error=ValueError("bad json"))),
    ):
        assert await get_latest_version() is None


@pytest.mark.asyncio
async def test_get_latest_version_http_status_error() -> None:
    import httpx

    response = _response({"info": {"version": "1.0.0"}})
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )
    with patch(
        "phoson_cli.updater.httpx.AsyncClient",
        return_value=_client_with(response),
    ):
        assert await get_latest_version() is None


# ── Subprocess: spawn error, bounded output ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_upgrade_command_spawn_error_returns_127() -> None:
    code, output = await run_upgrade_command(
        ["/nonexistent/definitely-not-a-real-binary-xyz"]
    )
    assert code == 127
    assert "Could not start" in output
    assert "/nonexistent/definitely-not-a-real-binary-xyz" in output


@pytest.mark.asyncio
async def test_run_upgrade_command_output_is_bounded_to_tail() -> None:
    code, output = await run_upgrade_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('A'*10000 + 'B'*10000)"]
    )
    assert code == 0
    assert len(output) <= 2000
    # Only the 8 KiB tail is retained, so the returned slice is all "B".
    assert output == "B" * 2000


@pytest.mark.asyncio
async def test_run_upgrade_command_empty_output() -> None:
    code, output = await run_upgrade_command([sys.executable, "-c", "pass"])
    assert code == 0
    assert output == ""


# ── Cancellation: kill + reap real sleeping children ─────────────────────────


async def _wait_for_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"timed out waiting for {path}")


def _capture_proc(monkeypatch) -> dict:
    """Wrap create_subprocess_exec so the test can inspect the child."""
    captured: dict = {}
    real = asyncio.create_subprocess_exec

    async def wrapper(*args, **kwargs):
        proc = await real(*args, **kwargs)
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", wrapper)
    return captured


@pytest.mark.asyncio
async def test_cancellation_kills_and_reaps_direct_child(monkeypatch) -> None:
    captured = _capture_proc(monkeypatch)
    task = asyncio.create_task(
        run_upgrade_command([sys.executable, "-c", "import time; time.sleep(60)"])
    )
    # Give the child a moment to start, then cancel the awaiting task.
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    proc = captured["proc"]
    assert proc.returncode is not None  # reaped, no live child left behind
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


@pytest.mark.skipif(not POSIX, reason="POSIX process-group cleanup only")
@pytest.mark.asyncio
async def test_cancellation_kills_process_group_grandchild(
    monkeypatch, tmp_path: Path
) -> None:
    if not Path("/proc").exists():
        pytest.skip("requires /proc to observe the grandchild")

    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        "open(sys.argv[1],'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    captured = _capture_proc(monkeypatch)
    task = asyncio.create_task(
        run_upgrade_command([sys.executable, "-c", code, str(pidfile)])
    )
    await _wait_for_file(pidfile)
    grandchild = int(pidfile.read_text(encoding="utf-8").strip())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured["proc"].returncode is not None

    # The grandchild shares the child's process group and must be gone too.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{grandchild}/stat", encoding="utf-8") as stream:
                state = stream.read().split(") ", 1)[1].split()[0]
        except FileNotFoundError:
            state = None
        if state in (None, "Z", "X"):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"grandchild {grandchild} survived process-group kill")


@pytest.mark.asyncio
async def test_run_upgrade_command_timeout_kills_child(monkeypatch) -> None:
    captured = _capture_proc(monkeypatch)
    code, output = await run_upgrade_command(
        [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.3
    )
    assert code == 124
    assert "timed out" in output
    proc = captured["proc"]
    assert proc.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


# ── Fresh-interpreter verification ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_installed_version_uses_fresh_isolated_interpreter(
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    async def fake_run(command, timeout=updater.UPGRADE_TIMEOUT):
        calls.append(command)
        return 0, "9.9.9"

    monkeypatch.setattr(updater, "run_upgrade_command", fake_run)
    assert await get_installed_version() == "9.9.9"
    assert len(calls) == 1
    command = calls[0]
    assert command[0] == sys.executable
    assert "-I" in command  # isolated: ignores the running CLI's caches
    assert "-c" in command
    code = command[command.index("-c") + 1]
    assert "importlib.metadata" in code
    assert repr(updater.PACKAGE) in code


@pytest.mark.asyncio
async def test_get_installed_version_rejects_bad_output(monkeypatch) -> None:
    monkeypatch.setattr(
        updater, "run_upgrade_command", AsyncMock(return_value=(0, "not-a-version"))
    )
    assert await get_installed_version() is None


@pytest.mark.asyncio
async def test_get_installed_version_none_on_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(
        updater, "run_upgrade_command", AsyncMock(return_value=(1, "boom"))
    )
    assert await get_installed_version() is None


# ── perform_self_update severity / exit codes ────────────────────────────────


def _setup_update(monkeypatch, *, current, latest, mode, run, installed=None):
    monkeypatch.setattr(updater, "get_current_version", lambda: current)
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value=latest))
    monkeypatch.setattr(updater, "detect_install_mode", lambda: mode)
    monkeypatch.setattr(updater, "_update_confirm", AsyncMock(return_value=True))
    monkeypatch.setattr(updater, "run_upgrade_command", AsyncMock(return_value=run))
    monkeypatch.setattr(
        updater, "get_installed_version", AsyncMock(return_value=installed)
    )


@pytest.mark.asyncio
async def test_perform_self_update_success_severity(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.3.0",
        latest="0.4.0",
        mode=InstallMode.UV_TOOL,
        run=(0, "ok"),
        installed="0.4.0",
    )
    result = await perform_self_update(assume_yes=True)
    assert isinstance(result, UpdateResult)
    assert result.exit_code == 0
    assert result.level == "info"
    assert "Updated to v0.4.0" in result


@pytest.mark.asyncio
async def test_perform_self_update_noop_severity(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.3.0",
        latest="0.4.0",
        mode=InstallMode.UV_TOOL,
        run=(0, "ok"),
        installed="0.3.0",  # installer ran but nothing changed
    )
    result = await perform_self_update(assume_yes=True)
    assert result.exit_code == 0
    assert result.level == "warn"
    assert "No version change (v0.3.0)" in result
    assert "v0.4.0 is available on PyPI" in result


@pytest.mark.asyncio
async def test_perform_self_update_partial_severity(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.3.0",
        latest="0.5.0",
        mode=InstallMode.UV_TOOL,
        run=(0, "ok"),
        installed="0.4.0",  # moved forward, but not all the way
    )
    result = await perform_self_update(assume_yes=True)
    assert result.exit_code == 0
    assert result.level == "warn"
    assert "Updated to v0.4.0" in result
    assert "PyPI has v0.5.0" in result


@pytest.mark.asyncio
async def test_perform_self_update_unverified_severity(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.3.0",
        latest="0.4.0",
        mode=InstallMode.UV_TOOL,
        run=(0, "ok"),
        installed=None,  # verification failed
    )
    result = await perform_self_update(assume_yes=True)
    assert result.exit_code == 1
    assert result.level == "warn"
    assert "could not be verified" in result


@pytest.mark.asyncio
async def test_perform_self_update_failure_severity(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.3.0",
        latest="0.4.0",
        mode=InstallMode.UV_TOOL,
        run=(1, "boom"),
        installed=None,
    )
    result = await perform_self_update(assume_yes=True)
    assert result.exit_code == 1
    assert result.level == "error"
    assert "Update failed (exit 1)" in result


@pytest.mark.asyncio
async def test_perform_self_update_offline_severity(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.3.0",
        latest=None,
        mode=InstallMode.UV_TOOL,
        run=(0, "ok"),
        installed=None,
    )
    result = await perform_self_update(assume_yes=True)
    assert result.exit_code == 1
    assert result.level == "warn"
    assert "Could not check PyPI" in result


@pytest.mark.asyncio
async def test_perform_self_update_up_to_date_severity(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.4.0",
        latest="0.4.0",
        mode=InstallMode.UV_TOOL,
        run=(0, "ok"),
        installed="0.4.0",
    )
    result = await perform_self_update(assume_yes=True)
    assert result.exit_code == 0
    assert result.level == "info"
    assert "up to date" in result


@pytest.mark.asyncio
async def test_perform_self_update_cancelled_severity(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.3.0",
        latest="0.4.0",
        mode=InstallMode.UV_TOOL,
        run=(0, "ok"),
        installed="0.4.0",
    )
    monkeypatch.setattr(updater, "_update_confirm", AsyncMock(return_value=False))
    result = await perform_self_update(assume_yes=False)
    assert result.exit_code == 0
    assert result.level == "info"
    assert result == "Update cancelled."


# ── uvx hint ─────────────────────────────────────────────────────────────────


def test_uvx_hint_is_correct() -> None:
    assert manual_hint(InstallMode.UVX) == (
        f"uvx --from {updater.PACKAGE}@latest phoson-cli"
    )
    assert upgrade_command(InstallMode.UVX) is None


@pytest.mark.asyncio
async def test_perform_self_update_uvx_explains_cached_versions(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.3.0",
        latest="0.4.0",
        mode=InstallMode.UVX,
        run=(0, "ok"),
        installed=None,
    )
    result = await perform_self_update(assume_yes=True)
    assert result.exit_code == 0
    assert result.level == "warn"
    assert "uvx reuses cached versions" in result
    assert f"uvx --from {updater.PACKAGE}@latest phoson-cli" in result


# ── Cached hint clears when current >= latest ────────────────────────────────


def _write_cache(
    path: Path, *, checked_at: float, ok: bool, latest: str | None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"checked_at": checked_at, "ok": ok, "latest_version": latest}),
        encoding="utf-8",
    )


def test_cached_hint_clears_when_current_equals_latest(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "last_update_check"
    now = 1000.0
    _write_cache(cache, checked_at=now, ok=True, latest="0.4.0")
    assert startup_check_due(cache, now=now) is False
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.4.0")
    import asyncio as _asyncio

    assert _asyncio.run(check_for_startup_update(path=cache, now=now)) is None


def test_cached_hint_clears_when_current_newer(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "last_update_check"
    now = 1000.0
    _write_cache(cache, checked_at=now, ok=True, latest="0.4.0")
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.5.0")
    import asyncio as _asyncio

    assert _asyncio.run(check_for_startup_update(path=cache, now=now)) is None


def test_cached_hint_returned_when_update_available(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "last_update_check"
    now = 1000.0
    _write_cache(cache, checked_at=now, ok=True, latest="0.4.0")
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.3.0")
    import asyncio as _asyncio

    assert _asyncio.run(check_for_startup_update(path=cache, now=now)) == "0.4.0"


def test_cached_hint_ignores_invalid_cached_version(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "last_update_check"
    now = 1000.0
    _write_cache(cache, checked_at=now, ok=True, latest="not-a-version")
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.3.0")
    import asyncio as _asyncio

    assert _asyncio.run(check_for_startup_update(path=cache, now=now)) is None


# ── Rich output: literal markup ──────────────────────────────────────────────


def test_print_update_result_does_not_interpret_markup() -> None:
    from rich.console import Console

    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, width=200)
    print_update_result(console, UpdateResult("[bold red]boom[/bold red]", "error"))
    rendered = stream.getvalue()
    # Rich must render the brackets literally, not swallow them as markup.
    assert "[bold red]boom[/bold red]" in rendered
    assert "boom" in rendered


def test_print_update_result_handles_plain_string() -> None:
    from rich.console import Console

    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, width=200)
    print_update_result(console, "plain summary")
    assert "plain summary" in stream.getvalue()


@pytest.mark.asyncio
async def test_update_command_routes_progress_and_result(monkeypatch) -> None:
    from phoson_cli.commands import Command, CommandHandler

    host = MagicMock()
    host.confirm = AsyncMock(return_value=True)
    handler = object.__new__(CommandHandler)
    handler.host = host

    async def update(**kwargs):
        assert kwargs["confirm"] is host.confirm
        assert kwargs["assume_yes"] is False
        kwargs["notify"]("info", "Installing update")
        return UpdateResult("Update failed.", "error", 1)

    monkeypatch.setattr("phoson_cli.commands.perform_self_update", update)
    assert await handler._cmd_update(Command("/update", "")) is True
    host.print_info.assert_called_once_with("Installing update")
    host.print_error.assert_called_once_with("Update failed.")
    host.print_warn.assert_not_called()


def test_flag_uses_structured_exit_not_message(monkeypatch, capsys) -> None:
    from phoson_cli import __main__ as main

    async def update(**kwargs):
        kwargs["notify"]("info", "Checking for updates")
        return UpdateResult("Could not verify installation.", "warn", 1)

    monkeypatch.setattr(main, "perform_self_update", update)
    with pytest.raises(SystemExit) as exc:
        main.self_update()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert output.count("Checking for updates") == 1
    assert "Could not verify installation" in output


@pytest.mark.asyncio
async def test_update_cancel_reports_repair_hint(monkeypatch) -> None:
    _setup_update(
        monkeypatch,
        current="0.3.0",
        latest="0.4.0",
        mode=InstallMode.UV_TOOL,
        run=(0, "ok"),
        installed="0.4.0",
    )
    monkeypatch.setattr(
        updater, "run_upgrade_command", AsyncMock(side_effect=asyncio.CancelledError)
    )
    notify = MagicMock()
    with pytest.raises(asyncio.CancelledError):
        await perform_self_update(assume_yes=True, notify=notify)
    level, message = notify.call_args.args
    assert level == "warn"
    assert "Installation may be incomplete" in message
    assert "uv tool upgrade" in message
