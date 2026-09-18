"""Tests for ``--session``/``--resume`` and the exit resume hint."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from phoson_cli.__main__ import _resume_session, _print_resume_hint


def _meta(session_id: str, title: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=session_id, title=title)


def _repl(
    *,
    session_id: str = "",
    started: bool = False,
    title: str | None = None,
    metas: list | None = None,
    load_ok: bool = True,
):
    tree = SimpleNamespace(session_id=session_id, title=title)
    controller = SimpleNamespace(session_started=started)
    storage = SimpleNamespace(list_meta=AsyncMock(return_value=metas or []))
    load_session = AsyncMock(return_value=load_ok)
    return SimpleNamespace(
        tree=tree,
        _controller=controller,
        storage=storage,
        load_session=load_session,
    )


# ── _print_resume_hint ───────────────────────────────────────────────────────


def test_resume_hint_prints_when_session_started(capsys) -> None:
    repl = _repl(session_id="abcdef1234567890", started=True)
    _print_resume_hint(repl)
    out = capsys.readouterr().out
    assert "phoson-cli --session abcdef12" in out


def test_resume_hint_silent_before_first_message(capsys) -> None:
    """A fresh session was never created — nothing to resume."""
    repl = _repl(session_id="abcdef1234567890", started=False)
    _print_resume_hint(repl)
    assert capsys.readouterr().out == ""


def test_resume_hint_silent_without_session_id(capsys) -> None:
    repl = _repl(session_id="", started=True)
    _print_resume_hint(repl)
    assert capsys.readouterr().out == ""


# ── _resume_session ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_session_prefix_match_loads() -> None:
    repl = _repl(metas=[_meta("abcdef1234567890", "A title")])
    assert await _resume_session(repl, "abcdef") is True
    repl.load_session.assert_awaited_once_with("abcdef1234567890")


@pytest.mark.asyncio
async def test_resume_session_not_found_reports(capsys) -> None:
    repl = _repl(metas=[_meta("abcdef1234567890")])
    assert await _resume_session(repl, "zzz") is False
    assert "no session matching" in capsys.readouterr().err
    repl.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_session_ambiguous_lists_candidates(capsys) -> None:
    repl = _repl(metas=[_meta("abcd0000"), _meta("abcd1111", "other")])
    assert await _resume_session(repl, "abcd") is False
    err = capsys.readouterr().err
    assert "2 sessions match" in err
    assert "abcd0000" in err
    repl.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_session_load_failure_reports(capsys) -> None:
    repl = _repl(metas=[_meta("abcdef1234567890")], load_ok=False)
    assert await _resume_session(repl, "abcdef") is False
    assert "could not load session" in capsys.readouterr().err


# ── front-end runners: resume then run, one event loop ───────────────────────


@pytest.mark.asyncio
async def test_run_fullscreen_resumes_then_runs() -> None:
    from phoson_cli.__main__ import _run_fullscreen

    repl = _repl(metas=[_meta("abcdef1234567890")])
    app = SimpleNamespace(repl=repl, run_async=AsyncMock())

    assert await _run_fullscreen(app, "abcdef") is True
    repl.load_session.assert_awaited_once_with("abcdef1234567890")
    app.run_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_fullscreen_aborts_when_session_missing() -> None:
    from phoson_cli.__main__ import _run_fullscreen

    repl = _repl(metas=[])
    repl.shutdown = AsyncMock()
    app = SimpleNamespace(repl=repl, run_async=AsyncMock())

    assert await _run_fullscreen(app, "nope") is False
    repl.shutdown.assert_awaited_once()
    app.run_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_classic_resumes_then_runs() -> None:
    from phoson_cli.__main__ import _run_classic

    repl = _repl(metas=[_meta("abcdef1234567890")])
    repl.run = AsyncMock()

    assert await _run_classic(repl, "abcdef") is True
    repl.load_session.assert_awaited_once()
    repl.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_classic_aborts_when_session_missing() -> None:
    from phoson_cli.__main__ import _run_classic

    repl = _repl(metas=[])
    repl.run = AsyncMock()
    repl.shutdown = AsyncMock()

    assert await _run_classic(repl, "nope") is False
    repl.shutdown.assert_awaited_once()
    repl.run.assert_not_awaited()
