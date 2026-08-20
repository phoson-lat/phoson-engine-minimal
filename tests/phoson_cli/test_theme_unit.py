"""Unit tests for the theme system (phoson_cli.theme)."""

import pytest
from rich.console import Console
from prompt_toolkit.styles import Style

from phoson_cli.theme import (
    ANSI,
    DARK,
    LIGHT,
    NO_COLOR,
    VALID_NAMES,
    load_theme,
    build_prompt_style,
    build_picker_style_dict,
    build_wizard_prompt_style,
)

# ── Resolution ────────────────────────────────────────────────────────────────


def test_load_theme_defaults_to_dark(monkeypatch) -> None:
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    assert load_theme() is DARK
    assert load_theme(config_value=None) is DARK


def test_load_theme_from_env(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    monkeypatch.setenv("PHOSON_THEME", "light")
    assert load_theme() is LIGHT
    monkeypatch.setenv("PHOSON_THEME", "ANSI")  # case-insensitive
    assert load_theme() is ANSI


def test_load_theme_env_beats_config(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("PHOSON_THEME", "light")
    assert load_theme(config_value="ansi") is LIGHT


def test_load_theme_from_config_value(monkeypatch) -> None:
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert load_theme(config_value="light") is LIGHT
    assert load_theme(config_value="  ansi  ") is ANSI


def test_load_theme_no_color_env_wins(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("PHOSON_THEME", "light")
    assert load_theme(config_value="light") is NO_COLOR
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("NO_COLOR", "")  # empty is *not* set
    assert load_theme(config_value="light") is LIGHT


def test_load_theme_clicolor_zero(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("CLICOLOR", "0")
    assert load_theme() is NO_COLOR


def test_load_theme_unknown_name_warns_and_falls_back(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("PHOSON_THEME", "neon")
    with pytest.warns(UserWarning, match="Unknown theme"):
        assert load_theme() is DARK


# ── Theme tokens ──────────────────────────────────────────────────────────────


def test_theme_is_dark_flag() -> None:
    assert DARK.is_dark and ANSI.is_dark
    assert not LIGHT.is_dark and not NO_COLOR.is_dark


def test_no_color_tier_is_plain() -> None:
    for field in (
        "text",
        "muted",
        "accent",
        "accent_soft",
        "ok",
        "err",
        "warn",
        "panel_bg",
    ):
        assert getattr(NO_COLOR, field) == ""
    assert NO_COLOR.code_theme == "none"
    assert NO_COLOR.name in VALID_NAMES


# ── prompt_toolkit style builders ─────────────────────────────────────────────


def test_prompt_style_builds_for_every_theme() -> None:
    for theme in (DARK, LIGHT, ANSI, NO_COLOR):
        Style.from_dict(build_prompt_style(theme))  # raises on bad colors


def test_picker_style_builds_for_every_theme() -> None:
    for theme in (DARK, LIGHT, ANSI, NO_COLOR):
        Style.from_dict(build_picker_style_dict(theme))


def test_wizard_style_builds_for_every_theme() -> None:
    for theme in (DARK, LIGHT, ANSI, NO_COLOR):
        Style.from_dict(build_wizard_prompt_style(theme))


def test_no_color_style_dicts_are_blank() -> None:
    for builder in (
        build_prompt_style,
        build_picker_style_dict,
        build_wizard_prompt_style,
    ):
        assert all(v == "" for v in builder(NO_COLOR).values())


def test_prompt_style_has_expected_keys() -> None:
    d = build_prompt_style(DARK)
    for key in (
        "prompt.prefix",
        "prompt.model",
        "completion-menu",
        "completion-menu.completion.current",
        "scrollbar.background",
    ):
        assert key in d


def test_dark_prompt_style_keeps_historical_hex_colors() -> None:
    # The theme must not drift from the shipped look without a design review.
    d = build_prompt_style(DARK)
    assert d["prompt.prefix"] == "#b57bee bold"
    assert d["prompt.model"] == "#e0d0ff bold"
    assert d["completion-menu"] == "bg:#1e1530 #9a8faa"


# ── Renderer integration (smoke) ──────────────────────────────────────────────


def _render_to_raw(renderer_factory) -> str:
    """Render one user turn through a recording console, return raw SGR output."""
    import io

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=80)
    renderer_factory(console)
    return buf.getvalue()


def test_renderer_no_color_output_has_no_sgr_colors() -> None:
    from phoson_cli.renderer import Renderer

    def build(console):
        renderer = Renderer(console=console, theme=NO_COLOR)
        renderer.print_user_turn("hello")
        renderer.print_info("info line")
        renderer.print_warn("warn line")
        renderer.print_error("error line")

    raw = _render_to_raw(build)
    # Only bold (1) and reset (0) may remain — no SGR color codes.
    import re

    codes = re.findall(r"\x1b\[(\d+(?:;\d+)*)m", raw)
    assert all(c in {"1", "0"} for c in codes), f"unexpected SGR codes: {codes}"
    assert "hello" in raw


def test_renderer_light_theme_uses_light_badge() -> None:
    from phoson_cli.renderer import Renderer

    def build(console):
        Renderer(console=console, theme=LIGHT).print_user_turn("hello")

    # Light badge background #ddd0f0 = (221, 208, 240) as a truecolor SGR.
    assert "48;2;221;208;240" in _render_to_raw(build)


def test_renderer_dark_theme_unchanged_look() -> None:
    from phoson_cli.renderer import Renderer

    def build(console):
        Renderer(console=console, theme=DARK).print_user_turn("hello")

    # Dark badge background #23192f = (35, 25, 47) — the historical look.
    assert "48;2;35;25;47" in _render_to_raw(build)
