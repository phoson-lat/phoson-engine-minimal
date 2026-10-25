"""The full-screen welcome screen (``_views.render_welcome``)."""

import datetime
from types import SimpleNamespace

from rich.console import Console

from phoson_cli.theme import DARK
from phoson_cli._views import render_welcome


def _text(renderable) -> str:
    console = Console(width=100, highlight=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_welcome_lists_recent_sessions_and_tips() -> None:
    now = datetime.datetime(2026, 9, 7, 14, 0)
    sessions = [
        SimpleNamespace(
            id="ac6c0ab0ffff",
            title="Revisión de specs",
            updated_at=datetime.datetime(2026, 9, 7, 9, 30),
        ),
        SimpleNamespace(id="f3f5b030aaaa", title=None, updated_at=None),
    ]
    out = _text(
        render_welcome(
            version="0.56.0",
            cwd="~/proj",
            model="m (p)",
            sessions=sessions,
            theme=DARK,
            now=now,
        )
    )
    assert "phoson-cli  v0.56.0  ·  terminal agent" in out
    assert "~/proj  ·  m (p)" in out
    assert "ac6c0ab0  Revisión de specs  ·  09:30" in out
    assert "f3f5b030  untitled" in out
    assert "/resume <id>" in out
    assert "esc esc" in out


def test_welcome_without_sessions_skips_the_recent_block() -> None:
    out = _text(
        render_welcome(version="1", cwd="~", model="m", sessions=[], theme=DARK)
    )
    assert "Recent here" not in out
    assert "shift+tab" in out
