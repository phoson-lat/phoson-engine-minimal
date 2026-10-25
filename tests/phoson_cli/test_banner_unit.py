"""The animated phoson-cli welcome banner (``phoson_cli.banner``)."""

from rich.console import Console

from phoson_cli import banner
from phoson_cli.theme import DARK
from phoson_cli.banner import (
    LOGO_WIDTH,
    BLINK_LENGTH,
    BLINK_PERIOD,
    SHINE_PERIOD,
    WORDMARK_WIDTH,
    PhosonBanner,
    wordmark_rows,
    banner_frame_key,
    animations_enabled,
)


def _render(t: float | None, width: int = 100, color: bool = False) -> str:
    console = Console(
        width=width, highlight=False, color_system="truecolor" if color else None
    )
    with console.capture() as capture:
        console.print(PhosonBanner(DARK, "1.2.3", t))
    return capture.get()


def test_logo_rows_share_one_width() -> None:
    for eyes in (banner._EYES_OPEN, banner._EYES_SHUT):
        widths = {len(row.replace("{eyes}", eyes)) for row in banner._LOGO}
        assert widths == {LOGO_WIDTH}


def test_wordmark_is_five_even_rows() -> None:
    rows = wordmark_rows()
    assert len(rows) == 5
    assert {len(row) for row in rows} == {WORDMARK_WIDTH}


def test_wide_banner_has_wordmark_and_tagline() -> None:
    out = _render(None)
    assert wordmark_rows()[0].rstrip() in out
    assert "phoson-cli  v1.2.3  ·  terminal agent" in out


def test_narrow_banner_drops_the_wordmark() -> None:
    out = _render(None, width=50)
    assert wordmark_rows()[0].rstrip() not in out
    assert "phoson-cli  v1.2.3" in out


def test_blink_closes_the_eyes() -> None:
    assert "▓▓" in _render(BLINK_LENGTH + 0.5)
    assert "▂▂" in _render(BLINK_PERIOD * 2 + BLINK_LENGTH / 2)
    assert "▓▓" in _render(None)  # static: eyes open


def test_shine_sweeps_then_pauses() -> None:
    # Mid-sweep the wordmark is drawn in several colors; paused, in one.
    sweeping = _render(0.8, color=True)
    paused = _render(SHINE_PERIOD - 0.1, color=True)
    assert sweeping.count("38;2;") > paused.count("38;2;")


def test_frame_key_changes_only_when_the_picture_does() -> None:
    assert banner_frame_key(None) == ("static",)
    paused = [banner_frame_key(t) for t in (3.0, 3.5, 4.0)]
    assert len(set(paused)) == 1
    assert banner_frame_key(0.3) != banner_frame_key(0.9)


def test_animations_respect_no_color_and_opt_out(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PHOSON_NO_ANIMATION", raising=False)
    assert animations_enabled("dark")
    assert not animations_enabled("no-color")
    monkeypatch.setenv("PHOSON_NO_ANIMATION", "1")
    assert not animations_enabled("dark")
