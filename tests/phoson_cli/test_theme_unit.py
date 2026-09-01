"""Unit tests for the theme system (phoson_cli.theme)."""

import pytest
from rich.console import Console
from prompt_toolkit.styles import Style

from phoson_cli.theme import (
    ANSI,
    DARK,
    LIGHT,
    SYSTEM,
    NO_COLOR,
    VALID_NAMES,
    load_theme,
    load_json_themes,
    build_prompt_style,
    default_theme_registry,
    build_picker_style_dict,
    build_wizard_prompt_style,
)

# ── Resolution ────────────────────────────────────────────────────────────────


def test_load_theme_defaults_to_system(monkeypatch) -> None:
    """T-8: with no env/config, the terminal's own colors win."""
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    assert load_theme() is SYSTEM
    assert load_theme(config_value=None) is SYSTEM


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


def test_system_tier_carries_no_backgrounds() -> None:
    """T-8: system inherits the terminal — no ``on #rrggbb`` anywhere."""
    for field in (
        "text",
        "muted",
        "muted_deep",
        "accent",
        "accent_soft",
        "art",
        "reasoning",
        "panel_bg",
        "badge_user",
        "badge_assistant",
        "badge_history",
        "diff_add_bg",
        "diff_del_bg",
    ):
        assert "on #" not in getattr(SYSTEM, field), field
    # Accent is reduced to the terminal's own state colors + focus.
    assert SYSTEM.ok == "green"
    assert SYSTEM.err == "red"
    assert SYSTEM.pt_accent == "cyan"


def test_system_tier_prompt_style_is_parseable() -> None:
    """``PHOSON_THEME=system`` must produce a valid prompt_toolkit style."""
    Style.from_dict(build_prompt_style(SYSTEM))
    Style.from_dict(build_picker_style_dict(SYSTEM))
    d = build_prompt_style(SYSTEM)
    # No hex backgrounds in the chrome: bg tokens are "" or "default".
    for value in d.values():
        assert "bg:#" not in value, value


# ── Drop-in JSON themes (T-8) ────────────────────────────────────────────────


def test_load_json_theme_from_dir(tmp_path, monkeypatch) -> None:
    import phoson_cli.theme as theme_mod

    (tmp_path / "nord.json").write_text(
        '{"name": "nord", "base": "dark", "accent": "#88c0d0", "muted": "#7b88a1"}'
    )
    monkeypatch.setattr(theme_mod, "JSON_THEMES_DIR", tmp_path)

    themes = load_json_themes()
    assert set(themes) == {"nord"}
    nord = themes["nord"]
    assert nord.name == "nord"
    assert nord.accent == "#88c0d0"
    assert nord.muted == "#7b88a1"
    # Untouched tokens inherit the base.
    assert nord.panel_bg == DARK.panel_bg


def test_json_theme_appears_in_registry_and_loads(tmp_path, monkeypatch) -> None:
    import phoson_cli.theme as theme_mod

    (tmp_path / "catppuccin.json").write_text(
        '{"name": "catppuccin", "base": "light", "accent": "#cba6f7"}'
    )
    monkeypatch.setattr(theme_mod, "JSON_THEMES_DIR", tmp_path)
    monkeypatch.delenv("PHOSON_THEME", raising=False)

    registry = default_theme_registry()
    assert "catppuccin" in registry.valid_names()
    assert load_theme(config_value="catppuccin", registry=registry).accent == "#cba6f7"


def test_broken_json_theme_is_skipped(tmp_path, monkeypatch) -> None:
    """A bad user file must never break startup — it is skipped, not fatal."""
    import phoson_cli.theme as theme_mod

    (tmp_path / "bad1.json").write_text("{not json")
    (tmp_path / "bad2.json").write_text('{"name": "x", "base": "nope"}')
    (tmp_path / "good.json").write_text('{"name": "ok", "base": "dark"}')
    monkeypatch.setattr(theme_mod, "JSON_THEMES_DIR", tmp_path)

    themes = load_json_themes()
    assert set(themes) == {"ok"}


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
    for theme in (SYSTEM, DARK, LIGHT, ANSI, NO_COLOR):
        Style.from_dict(build_prompt_style(theme))  # raises on bad colors


def test_picker_style_builds_for_every_theme() -> None:
    for theme in (SYSTEM, DARK, LIGHT, ANSI, NO_COLOR):
        Style.from_dict(build_picker_style_dict(theme))


def test_wizard_style_builds_for_every_theme() -> None:
    for theme in (SYSTEM, DARK, LIGHT, ANSI, NO_COLOR):
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


def test_renderer_user_turn_has_no_background_chip() -> None:
    """T-2: the user turn is a › gutter, not a filled badge chip — no
    background (48;…) SGR code in either theme."""
    import re

    from phoson_cli.renderer import Renderer

    def _codes(theme):
        def build(console):
            Renderer(console=console, theme=theme).print_user_turn("hello")

        raw = _render_to_raw(build)
        codes = re.findall(r"\x1b\[(\d+(?:;\d+)*)m", raw)
        return codes

    # No background-color (48;) SGR code in either palette.
    assert not any(c.startswith("48;") for c in _codes(LIGHT))
    assert not any(c.startswith("48;") for c in _codes(DARK))


def test_renderer_user_turn_shows_gutter_and_text() -> None:
    """T-2: the user turn renders a › gutter followed by the message text."""
    from phoson_cli.renderer import Renderer

    def build(console):
        Renderer(console=console, theme=DARK).print_user_turn("hello")

    raw = _render_to_raw(build)
    assert "›" in raw
    assert "hello" in raw
