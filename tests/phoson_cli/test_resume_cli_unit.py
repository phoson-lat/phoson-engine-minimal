"""Tests for ``--session``/``--resume`` and the exit resume hint."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from phoson_cli.__main__ import _resume_session, _print_resume_hint


def _meta(
    session_id: str, title: str | None = None, cwd: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(id=session_id, title=title, cwd=cwd)


def _repl(
    *,
    session_id: str = "",
    started: bool = False,
    title: str | None = None,
    metas: list | None = None,
    all_metas: list | None = None,
    load_ok: bool = True,
):
    tree = SimpleNamespace(session_id=session_id, title=title)
    controller = SimpleNamespace(session_started=started)

    async def list_meta(cwd: str | None = None) -> list:
        # cwd-scoped listing vs. every session (``list_meta()``).
        return (metas or []) if cwd is not None else (all_metas or metas or [])

    storage = SimpleNamespace(list_meta=list_meta)
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
    assert await _resume_session(repl, "abcdef") is None
    repl.load_session.assert_awaited_once_with("abcdef1234567890")


@pytest.mark.asyncio
async def test_resume_session_not_found_reports() -> None:
    repl = _repl(metas=[_meta("abcdef1234567890")])
    assert "no session matching" in await _resume_session(repl, "zzz")
    repl.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_session_in_another_directory_says_where() -> None:
    repl = _repl(metas=[], all_metas=[_meta("ac6c0ab0ffff", cwd="/home/me/proj")])
    error = await _resume_session(repl, "ac6c0ab0")
    assert "belongs to /home/me/proj" in error
    assert "cd /home/me/proj && phoson-cli --session ac6c0ab0" in error
    repl.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_session_ambiguous_lists_candidates() -> None:
    repl = _repl(metas=[_meta("abcd0000"), _meta("abcd1111", "other")])
    error = await _resume_session(repl, "abcd")
    assert "2 sessions match" in error
    assert "abcd0000" in error
    repl.load_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_session_load_failure_reports() -> None:
    repl = _repl(metas=[_meta("abcdef1234567890")], load_ok=False)
    assert "could not load session" in await _resume_session(repl, "abcdef")


# ── front-end runners: resume then run, one event loop ───────────────────────


@pytest.mark.asyncio
async def test_run_fullscreen_resumes_then_runs() -> None:
    from phoson_cli.__main__ import _run_fullscreen

    repl = _repl(metas=[_meta("abcdef1234567890")])
    app = SimpleNamespace(session=repl, run_async=AsyncMock())

    assert await _run_fullscreen(app, "abcdef") is None
    repl.load_session.assert_awaited_once_with("abcdef1234567890")
    app.run_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_fullscreen_aborts_when_session_missing() -> None:
    from phoson_cli.__main__ import _run_fullscreen

    repl = _repl(metas=[])
    repl.shutdown = AsyncMock()
    app = SimpleNamespace(session=repl, run_async=AsyncMock())

    assert "no session matching" in await _run_fullscreen(app, "nope")
    repl.shutdown.assert_awaited_once()
    app.run_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_classic_resumes_then_runs() -> None:
    from phoson_cli.__main__ import _run_classic

    repl = _repl(metas=[_meta("abcdef1234567890")])
    repl.run = AsyncMock()

    assert await _run_classic(repl, "abcdef") is None
    repl.load_session.assert_awaited_once()
    repl.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_classic_aborts_when_session_missing() -> None:
    from phoson_cli.__main__ import _run_classic

    repl = _repl(metas=[])
    repl.run = AsyncMock()
    repl.shutdown = AsyncMock()

    assert "no session matching" in await _run_classic(repl, "nope")
    repl.shutdown.assert_awaited_once()
    repl.run.assert_not_awaited()
