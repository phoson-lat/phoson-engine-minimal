"""Unit tests for the startup update check (IMPROVEMENTS.md E5).

Covers the new UI-independent check layer in ``phoson_cli.updater``
(cache path, cadence gating, atomic persistence, hint text, and the full
``check_for_startup_update`` flow) and its wiring into both front ends
(classic REPL prompt fragment + shutdown cancellation, full-screen TUI
header + ``run_async`` startup task).
"""

import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli import updater
from phoson_cli.updater import (
    LAST_UPDATE_CHECK,
    STARTUP_CHECK_TIMEOUT,
    UPDATE_CHECK_INTERVAL,
    update_hint,
    startup_check_due,
    check_for_startup_update,
)

T_NOW = 1_000_000.0  # fixed "now" for cadence tests


def _write_cache(
    path: Path, checked_at: float, latest: str | None, ok: bool = True
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"checked_at": checked_at, "ok": ok, "latest_version": latest}),
        encoding="utf-8",
    )


# ── Cache path ───────────────────────────────────────────────────────────────


def test_update_check_path_defaults_to_phoson_dir() -> None:
    from phoson_cli.updater import _update_check_path

    path = _update_check_path()
    assert path.name == LAST_UPDATE_CHECK
    assert path.parent.name == ".phoson"
    assert "~" not in str(path)


def test_update_check_path_honors_phoson_home(monkeypatch) -> None:
    from phoson_cli.updater import _update_check_path

    monkeypatch.setenv("PHOSON_HOME", "/tmp/custom-phoson")
    assert _update_check_path() == Path("/tmp/custom-phoson") / LAST_UPDATE_CHECK


# ── Cadence gating (startup_check_due) ───────────────────────────────────────


def test_due_when_cache_missing(tmp_path) -> None:
    assert startup_check_due(tmp_path / "missing") is True


def test_due_when_cache_corrupt(tmp_path) -> None:
    path = tmp_path / LAST_UPDATE_CHECK
    path.write_text("not json", encoding="utf-8")
    assert startup_check_due(path) is True


def test_due_when_cache_wrong_shape(tmp_path) -> None:
    path = tmp_path / LAST_UPDATE_CHECK
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert startup_check_due(path) is True


def test_due_when_cache_lacks_checked_at(tmp_path) -> None:
    path = tmp_path / LAST_UPDATE_CHECK
    path.write_text('{"latest_version": null}', encoding="utf-8")
    assert startup_check_due(path) is True


def test_due_when_stale(tmp_path) -> None:
    path = tmp_path / LAST_UPDATE_CHECK
    _write_cache(path, T_NOW, "9.9.9")  # recent *successful* check
    # Exactly at the interval boundary: due.
    assert startup_check_due(path, now=T_NOW + UPDATE_CHECK_INTERVAL) is True
    assert startup_check_due(path, now=T_NOW + UPDATE_CHECK_INTERVAL + 1) is True
    # One second before the interval elapses: not due.
    assert startup_check_due(path, now=T_NOW + UPDATE_CHECK_INTERVAL - 1) is False


def test_due_when_last_attempt_failed(tmp_path) -> None:
    # A recent *failed* check (ok=false) must be retried on the next
    # start — the interval is reset by failures, so offline users are
    # retried per start instead of waiting 24h.
    path = tmp_path / LAST_UPDATE_CHECK
    _write_cache(path, T_NOW - 10, None, ok=False)
    assert startup_check_due(path, now=T_NOW) is True


def test_not_due_after_success_with_update_available(tmp_path) -> None:
    path = tmp_path / LAST_UPDATE_CHECK
    _write_cache(path, T_NOW - 10, "9.9.9", ok=True)
    assert startup_check_due(path, now=T_NOW) is False


def test_not_due_after_success_without_update(tmp_path) -> None:
    # Regression: a successful "no update available" check is NOT a
    # failure — it must sleep for the full interval, not re-hit PyPI on
    # every start (the old null-check made it indistinguishable from a
    # failure).
    path = tmp_path / LAST_UPDATE_CHECK
    _write_cache(path, T_NOW - 10, None, ok=True)
    assert startup_check_due(path, now=T_NOW) is False


def test_interval_is_24_hours() -> None:
    assert UPDATE_CHECK_INTERVAL == 86_400.0


# ── Hint text ────────────────────────────────────────────────────────────────


def test_update_hint_text() -> None:
    assert update_hint("0.8.1") == "⬆ v0.8.1 available — /update"


# ── check_for_startup_update flow ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_writes_cache_and_returns_newer(tmp_path, monkeypatch) -> None:
    path = tmp_path / LAST_UPDATE_CHECK
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value="9.9.9"))
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.12.4")
    with patch("phoson_cli.updater.time.time", return_value=T_NOW):
        result = await check_for_startup_update(path)

    assert result == "9.9.9"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["checked_at"] == T_NOW
    assert payload["ok"] is True
    assert payload["latest_version"] == "9.9.9"


@pytest.mark.asyncio
async def test_check_no_newer_version_returns_none_and_nulls_cache(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / LAST_UPDATE_CHECK
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value="0.12.4"))
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.12.4")

    result = await check_for_startup_update(path)

    assert result is None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ok"] is True  # PyPI answered → full 24 h sleep
    assert payload["latest_version"] is None  # but no hint to show


@pytest.mark.asyncio
async def test_check_dev_install_treats_any_release_as_newer(
    tmp_path, monkeypatch
) -> None:
    # Source checkouts report "dev" — is_update_available("dev", x) is
    # always True, so dev users still get the (accurate) hint.
    path = tmp_path / LAST_UPDATE_CHECK
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value="0.1.0"))
    monkeypatch.setattr(updater, "get_current_version", lambda: "dev")

    assert await check_for_startup_update(path) == "0.1.0"


@pytest.mark.asyncio
async def test_check_offline_returns_none_and_retries_next_start(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / LAST_UPDATE_CHECK
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value=None))
    with patch("phoson_cli.updater.time.time", return_value=T_NOW):
        assert await check_for_startup_update(path) is None

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["checked_at"] == T_NOW
    assert payload["ok"] is False
    assert payload["latest_version"] is None
    # The failed attempt is retried immediately (cadence reset by failure).
    assert startup_check_due(path, now=T_NOW + 1) is True


@pytest.mark.asyncio
async def test_check_skips_network_when_not_due(tmp_path, monkeypatch) -> None:
    path = tmp_path / LAST_UPDATE_CHECK
    _write_cache(path, T_NOW - 10, "9.9.9")  # recent successful check
    latest = AsyncMock(return_value="9.9.9")
    monkeypatch.setattr(updater, "get_latest_version", latest)

    result = await check_for_startup_update(path, now=T_NOW)

    assert result is None
    latest.assert_not_awaited()
    # Cache untouched.
    assert json.loads(path.read_text(encoding="utf-8"))["checked_at"] == T_NOW - 10


@pytest.mark.asyncio
async def test_check_uses_startup_timeout() -> None:
    """The startup check passes its own timeout to the PyPI fetch."""
    seen: dict[str, float] = {}

    class FakeClient:
        def __init__(self, timeout: float = 0.0) -> None:
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            response = MagicMock()
            response.json.return_value = {"info": {"version": "0.12.4"}}
            response.raise_for_status.return_value = None
            return response

    with (
        patch(
            "phoson_cli.updater.httpx.AsyncClient",
            lambda **kw: FakeClient(**kw),
        ),
        patch.object(updater, "get_current_version", lambda: "0.12.4"),
        patch.object(updater, "_write_update_check_cache", lambda p, d: None),
    ):
        await check_for_startup_update(Path("/tmp/e5-check-tmp"))

    assert seen["timeout"] == STARTUP_CHECK_TIMEOUT


@pytest.mark.asyncio
async def test_check_write_failure_is_swallowed(monkeypatch, tmp_path) -> None:
    """A read-only HOME must not crash the startup check (best-effort)."""
    path = tmp_path / LAST_UPDATE_CHECK

    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    # The real writer must swallow OSError (tmp file / rename failure).
    monkeypatch.setattr(updater.os, "replace", boom)
    updater._write_update_check_cache(path, {"x": 1})  # must not raise
    assert not path.exists()

    # And the whole check flow must still complete (returning the hint)
    # when persistence is impossible.
    monkeypatch.setattr(updater, "get_latest_version", AsyncMock(return_value="9.9.9"))
    monkeypatch.setattr(updater, "get_current_version", lambda: "0.1.0")
    assert await check_for_startup_update(tmp_path / "last") == "9.9.9"


# ── Classic REPL wiring ──────────────────────────────────────────────────────


def _make_repl(tmp_path):
    from phoson_cli.repl import PhosonRepl
    from phoson_cli.config import PhosonConfig

    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        repl = PhosonRepl(PhosonConfig(provider="ollama", sessions_dir=tmp_path))
    return repl


def test_repl_prompt_fragment_without_hint(tmp_path) -> None:
    repl = _make_repl(tmp_path)
    assert repl.update_hint is None
    text = "".join(t for _s, t in repl._prompt_fragments())
    assert "/update" not in text
    assert "⬆" not in text


def test_repl_prompt_fragment_with_hint(tmp_path) -> None:
    repl = _make_repl(tmp_path)
    repl.update_hint = "⬆ v0.8.1 available — /update"
    fragments = repl._prompt_fragments()
    text = "".join(t for _s, t in fragments)
    assert "⬆ v0.8.1 available — /update" in text
    # The hint is its own fragment (styled separately), after the arrow.
    styles = [s for s, _t in fragments]
    assert "class:prompt.update" in styles
    assert styles.index("class:prompt.update") > styles.index("class:prompt.arrow")


@pytest.mark.asyncio
async def test_repl_start_update_check_sets_hint(tmp_path, monkeypatch) -> None:
    repl = _make_repl(tmp_path)
    monkeypatch.setattr(
        "phoson_cli.updater.check_for_startup_update",
        AsyncMock(return_value="0.8.1"),
    )
    assert repl._update_check_task is None

    repl.start_update_check()
    assert repl._update_check_task is not None
    await repl._update_check_task

    assert repl.update_hint == "⬆ v0.8.1 available — /update"


@pytest.mark.asyncio
async def test_repl_start_update_check_failure_keeps_hint_none(
    tmp_path, monkeypatch
) -> None:
    async def boom() -> str | None:
        raise RuntimeError("network down")

    repl = _make_repl(tmp_path)
    monkeypatch.setattr(
        "phoson_cli.updater.check_for_startup_update", AsyncMock(side_effect=boom)
    )

    repl.start_update_check()
    await repl._update_check_task

    assert repl.update_hint is None  # never crashes, just no hint


@pytest.mark.asyncio
async def test_repl_start_update_check_on_settle_callback(
    tmp_path, monkeypatch
) -> None:
    repl = _make_repl(tmp_path)
    monkeypatch.setattr(
        "phoson_cli.updater.check_for_startup_update",
        AsyncMock(return_value=None),
    )
    settled: list[bool] = []
    repl.start_update_check(on_settle=lambda: settled.append(True))
    await repl._update_check_task
    assert settled == [True]


@pytest.mark.asyncio
async def test_repl_shutdown_cancels_inflight_check(tmp_path, monkeypatch) -> None:
    started = asyncio.Event()

    async def slow_check() -> str | None:
        started.set()
        await asyncio.sleep(30)
        return None

    repl = _make_repl(tmp_path)
    monkeypatch.setattr("phoson_cli.updater.check_for_startup_update", slow_check)

    repl.start_update_check()
    await asyncio.wait_for(started.wait(), timeout=2)
    assert not repl._update_check_task.done()

    await asyncio.wait_for(repl.shutdown(), timeout=2)
    assert repl._update_check_task.cancelled()


def test_repl_prompt_style_includes_update_class() -> None:
    from phoson_cli.theme import DARK, build_prompt_style

    styles = build_prompt_style(DARK)
    assert "prompt.update" in styles
    # Dim like the other prompt metadata (not bold, not accent).
    assert styles["prompt.update"] == styles["prompt.tokens"]


# ── Full-screen TUI wiring ───────────────────────────────────────────────────


def _make_app(tmp_path):
    from phoson_cli.config import PhosonConfig
    from phoson_cli.fullscreen.app import PhosonApp

    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        app = PhosonApp(
            PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                history_file=tmp_path / "history.txt",
            )
        )
    return app


def test_tui_header_without_hint(tmp_path) -> None:
    app = _make_app(tmp_path)
    html = str(app._get_header_text())
    assert "/update" not in html
    assert "⬆" not in html


def test_tui_header_with_hint(tmp_path) -> None:
    app = _make_app(tmp_path)
    app.repl.update_hint = "⬆ v0.8.1 available — /update"
    html = str(app._get_header_text())
    assert "⬆ v0.8.1 available — /update" in html
    # Rendered dim (header_dim), not bold accent.
    assert '<style class="header_dim"> | ⬆ v0.8.1 available — /update</style>' in html


@pytest.mark.asyncio
async def test_tui_run_async_starts_update_check(tmp_path, monkeypatch) -> None:
    app = _make_app(tmp_path)
    check = AsyncMock(return_value="0.8.1")
    monkeypatch.setattr("phoson_cli.updater.check_for_startup_update", check)

    async def fake_run_async():
        # Yield enough for the background check task to settle before the
        # real run_async's finally-block would cancel it on exit.
        for _ in range(5):
            await asyncio.sleep(0)

    monkeypatch.setattr(app.app, "run_async", fake_run_async)

    await app.run_async()

    check.assert_awaited_once()
    assert app.repl.update_hint == "⬆ v0.8.1 available — /update"
    # The hint is visible in the header once the check settled.
    assert "⬆ v0.8.1 available — /update" in str(app._get_header_text())


@pytest.mark.asyncio
async def test_tui_run_async_check_offline_no_hint(tmp_path, monkeypatch) -> None:
    app = _make_app(tmp_path)
    monkeypatch.setattr(
        "phoson_cli.updater.check_for_startup_update",
        AsyncMock(return_value=None),
    )

    async def fake_run_async():
        for _ in range(5):
            await asyncio.sleep(0)

    monkeypatch.setattr(app.app, "run_async", fake_run_async)

    await app.run_async()

    assert app.repl.update_hint is None
    assert "/update" not in str(app._get_header_text())
