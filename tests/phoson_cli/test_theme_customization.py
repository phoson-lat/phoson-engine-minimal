"""Theme customization: palettes, [theme_overrides], accent, new/reload/export."""

import json
import tomllib
from types import SimpleNamespace
from pathlib import Path

import pytest

from phoson_cli import theme as theme_mod
from phoson_cli.theme import (
    DARK,
    SYSTEM,
    NO_COLOR,
    THEME_TOKENS,
    BUILTIN_THEMES,
    load_theme,
    theme_to_json,
    load_json_themes,
    build_theme_registry,
    load_theme_overrides,
    apply_theme_overrides,
    _validate_theme_styles,
    default_theme_registry,
)
from phoson_cli.config import PhosonConfigError, save_theme_overrides
from phoson_cli.theme_palettes import PALETTES, blend, to_hex, accent_family


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A private ~ with an empty ~/.phoson/themes/ for each test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PHOSON_THEME", raising=False)
    themes = tmp_path / ".phoson" / "themes"
    themes.mkdir(parents=True)
    monkeypatch.setattr(theme_mod, "JSON_THEMES_DIR", themes)
    return tmp_path


def _config(home: Path) -> Path:
    return home / ".phoson" / "config.toml"


# ── palettes ─────────────────────────────────────────────────────────────


def test_every_builtin_theme_is_valid() -> None:
    for theme in BUILTIN_THEMES.values():
        _validate_theme_styles(theme)


def test_palettes_are_registered_after_the_tiers(home) -> None:
    names = default_theme_registry().valid_names()
    assert names[:5] == ("system", "dark", "light", "ansi", "no-color")
    assert {p.name for p in PALETTES} <= set(names)
    rows = dict(default_theme_registry().rows())
    assert rows["dracula"].startswith("Dracula")


def test_palette_theme_uses_the_palette_colors() -> None:
    nord = BUILTIN_THEMES["nord"]
    assert nord.accent == "#88c0d0"
    assert nord.pt_accent == "#88c0d0"
    assert nord.ok == "#a3be8c"
    assert nord.code_theme == "nord"


def test_json_theme_can_start_from_a_palette(home) -> None:
    (theme_mod.JSON_THEMES_DIR / "mine.json").write_text(
        json.dumps({"base": "dracula", "ok": "#00ff00"})
    )
    mine = load_json_themes()["mine"]
    assert mine.ok == "#00ff00"
    assert mine.accent == BUILTIN_THEMES["dracula"].accent


def test_users_own_palette_named_json_wins(home) -> None:
    (theme_mod.JSON_THEMES_DIR / "nord.json").write_text(
        json.dumps({"base": "dark", "accent": "#123456"})
    )
    registry = default_theme_registry()
    assert registry.get("nord").accent == "#123456"
    assert dict(registry.rows())["nord"].startswith("JSON theme")


def test_tiers_stay_protected(home) -> None:
    (theme_mod.JSON_THEMES_DIR / "dark.json").write_text(
        json.dumps({"accent": "#123456"})
    )
    with pytest.warns(UserWarning, match="cannot be replaced"):
        assert "dark" not in load_json_themes()


def test_json_comment_keys_are_ignored(home) -> None:
    (theme_mod.JSON_THEMES_DIR / "c.json").write_text(
        json.dumps({"_comment": "hi", "_notes": ["x"], "accent": "#123456"})
    )
    assert load_json_themes()["c"].accent == "#123456"


def test_picker_scrolls_and_fits_long_names(home) -> None:
    from phoson_cli.theme_picker import build_theme_picker

    picker = build_theme_picker("system")
    frame = "".join(text for _s, text in picker._render())
    assert "catppuccin-mocha  " in frame  # label column fits the longest name
    assert "↓" in frame and "more" in frame
    assert "one-dark" not in frame.split("Preview")[0]  # beyond the window


# ── color helpers ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("#abc", "#aabbcc"), ("#FF79C6", "#ff79c6"), ("magenta", "#800080")],
)
def test_to_hex(raw, expected) -> None:
    assert to_hex(raw) == expected


@pytest.mark.parametrize("raw", ["", "nope", "default", "#12"])
def test_to_hex_rejects_non_colors(raw) -> None:
    with pytest.raises(ValueError):
        to_hex(raw)


def test_blend() -> None:
    assert blend("#ffffff", "#000000", 0.5) == "#808080"


def test_accent_family_on_a_palette_tints_from_its_background() -> None:
    tokens = accent_family(DARK, "#ff0000")
    assert tokens["accent"] == "#ff0000"
    assert tokens["pt_accent"] == "#ff0000"
    assert tokens["art"] == "#ff0000 bold"
    assert tokens["completion_current_bg"] == blend("#ff0000", "#150f24", 0.35)
    assert tokens["badge_user"].startswith("on #")


def test_accent_family_on_terminal_native_tiers_stays_native() -> None:
    tokens = accent_family(SYSTEM, "#ff0000")
    assert "completion_current_bg" not in tokens
    assert tokens["completion_current_fg"] == "#ff0000"
    assert "badge_user" not in tokens


# ── overrides ────────────────────────────────────────────────────────────


def test_no_overrides_keeps_the_same_objects(home) -> None:
    assert load_theme_overrides() == {}
    assert default_theme_registry().get("dark") is DARK
    assert load_theme() is SYSTEM


def test_overrides_are_read_and_bad_entries_skipped(home) -> None:
    _config(home).write_text(
        '[theme_overrides]\nok = "#00ff00"\nbogus = "#fff"\nerr = 3\n'
    )
    with pytest.warns(UserWarning):
        assert load_theme_overrides() == {"ok": "#00ff00"}


def test_overrides_apply_to_every_theme_but_no_color(home) -> None:
    _config(home).write_text('[theme_overrides]\nok = "#00ff00"\n')
    registry = default_theme_registry()
    assert registry.get("dark").ok == "#00ff00"
    assert registry.get("dracula").ok == "#00ff00"
    assert registry.get("no-color") is NO_COLOR
    # The default theme (nothing configured) goes through the registry too.
    assert load_theme().ok == "#00ff00"


def test_accent_override_derives_the_family_per_theme(home) -> None:
    _config(home).write_text('[theme_overrides]\naccent = "#ff0000"\n')
    registry = default_theme_registry()
    dark, nord = registry.get("dark"), registry.get("nord")
    assert dark.accent == nord.accent == "#ff0000"
    # Each theme tints its own background.
    assert dark.completion_current_bg != nord.completion_current_bg


def test_explicit_family_token_beats_the_derived_one(home) -> None:
    themed = apply_theme_overrides(
        DARK, {"accent": "#ff0000", "prompt_arrow": "#00ff00"}
    )
    assert themed.accent == "#ff0000"
    assert themed.prompt_arrow == "#00ff00"


def test_invalid_override_leaves_the_theme_untouched() -> None:
    with pytest.warns(UserWarning, match="Ignoring"):
        assert apply_theme_overrides(DARK, {"code_theme": "no-such-style"}) is DARK


def test_plugin_registry_applies_overrides_too(home) -> None:
    _config(home).write_text('[theme_overrides]\nwarn = "#ffff00"\n')
    assert build_theme_registry([]).get("light").warn == "#ffff00"


# ── save_theme_overrides ─────────────────────────────────────────────────


def test_save_overrides_adds_replaces_and_removes_the_table(home) -> None:
    path = _config(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '[defaults]\ntheme = "dark"  # keep me\n\n[keys]\nsubmit = "c-j"\n'
    path.write_text(original)

    save_theme_overrides({"accent": "#ff0000"})
    data = tomllib.loads(path.read_text())
    assert data["theme_overrides"] == {"accent": "#ff0000"}
    assert data["keys"] == {"submit": "c-j"}
    assert 'theme = "dark"  # keep me' in path.read_text()

    save_theme_overrides({"ok": 'quote"d'})
    data = tomllib.loads(path.read_text())
    assert data["theme_overrides"] == {"ok": 'quote"d'}
    assert path.read_text().count("[theme_overrides]") == 1

    save_theme_overrides({})
    assert path.read_text() == original.rstrip("\n") + "\n"
    assert (path.parent / "config.toml.bak").exists()


def test_save_overrides_rejects_a_malformed_file(home) -> None:
    path = _config(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[defaults\n")
    with pytest.raises(PhosonConfigError):
        save_theme_overrides({"ok": "#00ff00"})


# ── JSON export round trip ───────────────────────────────────────────────


def test_theme_to_json_round_trips(home) -> None:
    source = BUILTIN_THEMES["gruvbox-dark"]
    text = theme_to_json(source, name="copy")
    payload = json.loads(text)
    assert payload["base"] == "gruvbox-dark"
    assert set(THEME_TOKENS) <= set(payload)
    (theme_mod.JSON_THEMES_DIR / "copy.json").write_text(text)
    copy = load_json_themes()["copy"]
    for token in THEME_TOKENS:
        assert getattr(copy, token) == getattr(source, token)


# ── /theme subcommands ───────────────────────────────────────────────────


class _Host:
    def __init__(self) -> None:
        self.info: list[str] = []
        self.errors: list[str] = []
        self.applied: list[str] = []

    def print_info(self, message: str) -> None:
        self.info.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def apply_theme(self, theme) -> None:
        self.applied.append(theme.name)

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _handler(current: str = "dark"):
    from phoson_cli.commands import CommandHandler

    session = SimpleNamespace(
        theme=SimpleNamespace(name=current),
        theme_registry=default_theme_registry(),
        config=SimpleNamespace(theme=current),
    )

    def reload():
        session.theme_registry = default_theme_registry()
        session.theme = session.theme_registry.get(current)
        return session.theme_registry

    session.reload_themes = reload
    handler = CommandHandler.__new__(CommandHandler)
    handler.session = session
    handler.host = _Host()
    return handler


async def _theme(handler, args: str) -> None:
    from phoson_cli.commands import Command

    await handler._cmd_theme(Command(name="/theme", args=args))


async def test_accent_sets_saves_and_applies_live(home) -> None:
    handler = _handler("nord")
    await _theme(handler, "accent #f0f")
    assert load_theme_overrides() == {"accent": "#ff00ff"}
    assert handler.host.applied == ["nord"]
    assert handler.session.theme_registry.get("nord").accent == "#ff00ff"
    assert "Accent → #ff00ff" in handler.host.info[-1]


async def test_accent_reset_and_bad_color(home) -> None:
    handler = _handler()
    await _theme(handler, "accent magenta")
    await _theme(handler, "accent reset")
    assert load_theme_overrides() == {}
    await _theme(handler, "accent notacolor")
    assert handler.host.errors


async def test_accent_reset_keeps_unrelated_overrides(home) -> None:
    save_theme_overrides({"accent": "#ff0000", "prompt_arrow": "#00ff00", "ok": "#0f0"})
    await _theme(_handler(), "accent reset")
    assert load_theme_overrides() == {"ok": "#0f0"}


async def test_new_writes_a_starter_from_the_current_theme(home) -> None:
    handler = _handler("dracula")
    await _theme(handler, "new mine")
    target = theme_mod.JSON_THEMES_DIR / "mine.json"
    payload = json.loads(target.read_text())
    assert payload["name"] == "mine" and payload["base"] == "dracula"
    assert handler.session.theme_registry.get("mine") is not None
    assert any("/theme mine" in line for line in handler.host.info)


async def test_new_from_another_theme(home) -> None:
    await _theme(_handler(), "new warm --from gruvbox-light")
    payload = json.loads((theme_mod.JSON_THEMES_DIR / "warm.json").read_text())
    assert payload["base"] == "gruvbox-light"


@pytest.mark.parametrize(
    "args",
    [
        "new",
        "new Bad Name",
        "new dark",
        "new accent",
        "new x --from nope",
        "new x --from",
    ],
)
async def test_new_rejects_bad_input(home, args) -> None:
    handler = _handler()
    await _theme(handler, args)
    assert handler.host.errors
    assert not list(theme_mod.JSON_THEMES_DIR.glob("*.json"))


async def test_new_refuses_to_overwrite(home) -> None:
    (theme_mod.JSON_THEMES_DIR / "mine.json").write_text("{}")
    handler = _handler()
    await _theme(handler, "new mine")
    assert "already exists" in handler.host.errors[-1]
    assert (theme_mod.JSON_THEMES_DIR / "mine.json").read_text() == "{}"


async def test_reload_picks_up_new_json_themes(home) -> None:
    handler = _handler()
    (theme_mod.JSON_THEMES_DIR / "fresh.json").write_text(json.dumps({"base": "light"}))
    await _theme(handler, "reload")
    assert handler.session.theme_registry.get("fresh") is not None
    assert "Themes reloaded" in handler.host.info[-1]


async def test_export_writes_the_active_theme(home, tmp_path) -> None:
    handler = _handler("dark")
    handler.session.theme = DARK
    target = tmp_path / "out.json"
    await _theme(handler, f"export {target}")
    assert json.loads(target.read_text())["accent"] == DARK.accent
    await _theme(handler, f"export {target}")
    assert "already exists" in handler.host.errors[-1]


async def test_list_shows_overrides_and_descriptions(home) -> None:
    save_theme_overrides({"accent": "#ff0000"})
    handler = _handler("nord")
    await _theme(handler, "list")
    text = "\n".join(handler.host.info)
    assert "* nord" in text and "Nord" in text
    assert "accent=#ff0000" in text


def test_short_hex_overrides_are_expanded() -> None:
    themed = apply_theme_overrides(DARK, {"ok": "#0f0", "panel_bg": "on #123"})
    assert themed.ok == "#00ff00"
    assert themed.panel_bg == "on #112233"


def test_one_bad_override_does_not_drop_the_others(home) -> None:
    _config(home).write_text(
        '[theme_overrides]\nok = "#00ff00"\ncode_theme = "no-such-style"\n'
    )
    with pytest.warns(UserWarning) as caught:
        registry = default_theme_registry()
    assert registry.get("dark").ok == "#00ff00"
    assert registry.get("dark").code_theme == DARK.code_theme
    # Reported once for the token, not once per theme.
    messages = {str(w.message) for w in caught}
    assert len([m for m in messages if "code_theme" in m]) == 1
