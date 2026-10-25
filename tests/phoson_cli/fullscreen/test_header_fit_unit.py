"""Header fitting by priority and the context gauge (full-screen TUI)."""

from pathlib import Path

from prompt_toolkit.utils import get_cwidth

from phoson_cli.fullscreen.header_model import (
    _Segment,
    short_cwd,
    _fit_segments,
    _segments_width,
    _context_segment,
)


def _segments(title: str = "Refactor the auth middleware") -> list[_Segment]:
    return [
        _Segment("title", title, "header", shrink=True),
        _Segment("id", "(ac6c0ab0)", "header_dim", glue=True),
        _Segment("cwd", "Phoson/phoson-engine-minimal", "header_dim"),
        _context_segment(38_000, 128_000),
        _Segment("cost", "$0.0123", "header_dim"),
        _Segment("mode", "ask", "header"),
        _Segment("effort", "effort high", "header"),
        _Segment("agents_md", "📄 agents.md", "header_dim"),
        _Segment("update", "⬆ v0.57.0 available — /update", "header_dim"),
    ]


def _keys(segments: list[_Segment]) -> list[str]:
    return [s.key for s in segments]


def test_wide_terminal_keeps_everything() -> None:
    assert _keys(_fit_segments(_segments(), 300)) == _keys(_segments())


def test_narrow_terminal_drops_low_priority_first_and_keeps_mode_effort() -> None:
    fitted = _fit_segments(_segments(), 80)
    keys = _keys(fitted)
    assert {"mode", "effort", "context"} <= set(keys)
    assert "agents_md" not in keys and "update" not in keys
    assert _segments_width(fitted) <= 80


def test_title_is_squeezed_before_it_is_dropped() -> None:
    long_title = "A very long session title that keeps going and going"
    fitted = _fit_segments(_segments(long_title), 70)
    title = next(s for s in fitted if s.key == "title")
    assert title.text.endswith("…")
    assert get_cwidth(title.text) < get_cwidth(long_title)
    assert _segments_width(fitted) <= 70


def test_very_narrow_terminal_still_shows_the_essentials() -> None:
    keys = _keys(_fit_segments(_segments(), 30))
    assert keys == ["context", "mode", "effort"]


def test_context_gauge_tones() -> None:
    assert _context_segment(10_000, 100_000).style == "header_dim"
    warn = _context_segment(75_000, 100_000)
    assert (warn.style, warn.text) == ("header_warn", "75K/100K 75%")
    critical = _context_segment(170_300, 128_000)
    assert critical.style == "header_err"
    assert critical.text == "170.3K/128K 133% /compact"
    assert _context_segment(0, 0).text == "? tok"


def test_short_cwd_uses_tilde_for_home() -> None:
    home = Path.home()
    assert short_cwd(home) == "~"
    assert short_cwd(home / "proj") == "~/proj"
    assert short_cwd(home / "a" / "b" / "c") == "b/c"
