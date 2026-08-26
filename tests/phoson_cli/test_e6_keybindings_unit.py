"""Unit tests for customizable key bindings (IMPROVEMENTS.md E6).

Covers the UI-independent pieces first — the built-in key map
(``DEFAULT_KEY_BINDINGS``), the defaults+overrides merge
(``resolve_key_bindings``), and the config-layer validation
(``load_key_bindings`` / ``load_config``) — then the wiring into the
full-screen TUI (``build_key_bindings`` remap/unbind/chord/eager-escape,
``PhosonApp`` construction failing on conflicts), the friendly
``main()`` error path, and the ``/keys`` command.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from phoson_cli.config import (
    KNOWN_KEY_ACTIONS,
    PhosonConfig,
    PhosonConfigError,
    PhosonKeyBindingsError,
    load_config,
    save_config,
    load_key_bindings,
)
from phoson_cli.fullscreen.keys import (
    DEFAULT_KEY_BINDINGS,
    listing_for_config,
    resolve_key_bindings,
    key_bindings_for_config,
)

# ── Built-in map shape ─────────────────────────────────────────────────────────


def test_default_key_bindings_covers_every_known_action() -> None:
    assert set(DEFAULT_KEY_BINDINGS) == set(KNOWN_KEY_ACTIONS)


def test_default_key_bindings_match_the_historical_hardcoded_set() -> None:
    """E6 must not silently change any default binding."""
    assert DEFAULT_KEY_BINDINGS["submit"] == ["enter"]
    assert DEFAULT_KEY_BINDINGS["newline"] == ["c-j"]
    assert DEFAULT_KEY_BINDINGS["page_up"] == ["pageup"]
    assert DEFAULT_KEY_BINDINGS["page_down"] == ["pagedown"]
    assert DEFAULT_KEY_BINDINGS["line_up"] == ["s-up", "c-up"]
    assert DEFAULT_KEY_BINDINGS["line_down"] == ["s-down", "c-down"]
    assert DEFAULT_KEY_BINDINGS["scroll_home"] == ["home"]
    assert DEFAULT_KEY_BINDINGS["scroll_end"] == ["end"]
    assert DEFAULT_KEY_BINDINGS["clear"] == ["c-l"]
    assert DEFAULT_KEY_BINDINGS["toggle_reasoning"] == ["c-t"]
    assert DEFAULT_KEY_BINDINGS["ctrl_d"] == ["c-d"]
    assert DEFAULT_KEY_BINDINGS["paste_image"] == ["c-v"]
    assert DEFAULT_KEY_BINDINGS["escape"] == ["escape"]
    assert set(DEFAULT_KEY_BINDINGS["exit"]) == {"c-q", "c-c"}


def test_every_default_sequence_is_parseable() -> None:
    from prompt_toolkit.key_binding.key_bindings import _parse_key

    for sequences in DEFAULT_KEY_BINDINGS.values():
        for sequence in sequences:
            for key in sequence.split():
                _parse_key(key)  # raises ValueError on garbage


# ── resolve_key_bindings (merge + conflict detection) ─────────────────────────


def test_resolve_without_overrides_returns_the_defaults() -> None:
    resolved = resolve_key_bindings()
    assert resolved == DEFAULT_KEY_BINDINGS
    # Must be a copy — mutating the result must not touch the defaults.
    resolved["submit"].append("f13")
    assert DEFAULT_KEY_BINDINGS["submit"] == ["enter"]


def test_resolve_applies_overrides() -> None:
    resolved = resolve_key_bindings(overrides={"toggle_reasoning": ["c-x"]})
    assert resolved["toggle_reasoning"] == ["c-x"]
    assert resolved["submit"] == ["enter"]  # untouched actions keep defaults


def test_resolve_unbinds_on_empty_list() -> None:
    resolved = resolve_key_bindings(overrides={"newline": []})
    assert resolved["newline"] == []


def test_resolve_ignores_unknown_actions_silently() -> None:
    # Unknown actions are a load-time error; a stray entry here must not
    # crash or materialize a new action.
    resolved = resolve_key_bindings(overrides={"bogus": ["f13"]})
    assert "bogus" not in resolved


def test_resolve_rejects_cross_action_conflicts() -> None:
    with pytest.raises(ValueError, match="c-t"):
        resolve_key_bindings(overrides={"submit": ["c-t"]})
    with pytest.raises(ValueError, match="enter"):
        resolve_key_bindings(overrides={"paste_image": ["enter"]})


def test_resolve_allows_rebinding_onto_own_default() -> None:
    # Remapping an action onto the sequence it already owns is a no-op,
    # not a conflict.
    resolved = resolve_key_bindings(overrides={"toggle_reasoning": ["c-t"]})
    assert resolved["toggle_reasoning"] == ["c-t"]


def test_resolve_rejects_two_overrides_on_one_sequence() -> None:
    with pytest.raises(ValueError):
        resolve_key_bindings(overrides={"toggle_reasoning": ["c-x"], "clear": ["c-x"]})


# ── build_key_bindings (TUI wiring) ───────────────────────────────────────────


def _bound_sequences(app) -> dict[tuple[str, ...], bool]:
    """sequence tuple -> eager flag, over the app's merged bindings."""
    out: dict[tuple[str, ...], bool] = {}
    for binding in app.app.key_bindings.bindings:
        keys = tuple(getattr(k, "value", str(k)) for k in binding.keys)
        if len(keys) == 1:
            out.setdefault(keys, binding.eager())
    return out


@pytest.fixture
def app(tmp_path) -> "object":
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
        )
        from phoson_cli.fullscreen.app import PhosonApp

        return PhosonApp(config)


def test_default_app_binds_the_builtin_sequences(app) -> None:
    bound = {
        tuple(getattr(k, "value", str(k)) for k in b.keys)
        for b in app.app.key_bindings.bindings
    }

    def expect(sequence: str) -> tuple[str, ...]:
        # prompt_toolkit normalizes aliases ('enter' -> 'c-m').
        from prompt_toolkit.key_binding.key_bindings import _parse_key

        return tuple(
            getattr(_parse_key(part), "value", part) for part in sequence.split()
        )

    for action, sequences in DEFAULT_KEY_BINDINGS.items():
        for sequence in sequences:
            assert expect(sequence) in bound, f"{action}: {sequence} missing"


def test_default_app_escape_binding_stays_eager(app) -> None:
    bound = _bound_sequences(app)
    assert bound.get(("escape",)) is True


def test_remapped_app_moves_the_binding(tmp_path) -> None:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            key_bindings={"toggle_reasoning": ["c-x"]},
        )
        from phoson_cli.fullscreen.app import PhosonApp

        remapped = PhosonApp(config)

    bound = _bound_sequences(remapped)
    assert ("c-x",) in bound
    assert ("c-t",) not in bound


def test_unbound_action_disappears(tmp_path) -> None:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            key_bindings={"paste_image": []},
        )
        from phoson_cli.fullscreen.app import PhosonApp

        no_paste = PhosonApp(config)

    bound = _bound_sequences(no_paste)
    assert ("c-v",) not in bound


def test_chord_remap_binds_the_full_sequence(tmp_path) -> None:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            key_bindings={"toggle_reasoning": ["c-x c-e"]},
        )
        from phoson_cli.fullscreen.app import PhosonApp

        chord_app = PhosonApp(config)

    keys = [
        tuple(getattr(k, "value", str(k)) for k in b.keys)
        for b in chord_app.app.key_bindings.bindings
    ]
    assert ("c-x", "c-e") in keys
    assert ("c-t",) not in keys


def test_conflicting_remap_raises_at_construction(tmp_path) -> None:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            key_bindings={"submit": ["c-t"]},  # steals toggle_reasoning's key
        )
        from phoson_cli.fullscreen.app import PhosonApp

        with pytest.raises(PhosonKeyBindingsError, match="two actions"):
            PhosonApp(config)


def test_escape_remap_keeps_eager(tmp_path) -> None:
    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            key_bindings={"escape": ["c-@"]},  # Ctrl+Space
        )
        from phoson_cli.fullscreen.app import PhosonApp

        esc_app = PhosonApp(config)

    bound = _bound_sequences(esc_app)
    assert bound.get(("c-@",)) is True
    assert ("escape",) not in bound


# ── listing_for_config (display) ──────────────────────────────────────────────


def test_listing_shows_every_action_in_default_order() -> None:
    rows = listing_for_config(None)
    assert [action for action, _ in rows] == list(DEFAULT_KEY_BINDINGS)
    display = dict(rows)
    assert display["submit"] == "Enter"
    assert display["newline"] == "Ctrl+J"
    assert display["page_up"] == "PgUp"
    assert display["toggle_reasoning"] == "Ctrl+T"
    assert display["line_up"] == "Shift+Up, Ctrl+Up"
    assert display["escape"] == "Esc"
    assert display["exit"] == "Ctrl+Q, Ctrl+C"


def test_listing_reflects_overrides_and_unbinds(tmp_path) -> None:
    config = PhosonConfig(
        provider="ollama",
        sessions_dir=tmp_path,
        key_bindings={"toggle_reasoning": ["c-x"], "paste_image": []},
    )
    display = dict(listing_for_config(config))
    assert display["toggle_reasoning"] == "Ctrl+X"
    assert display["paste_image"] == "(off)"
    # key_bindings_for_config treats {} as "no overrides"
    assert key_bindings_for_config(config) == {
        "toggle_reasoning": ["c-x"],
        "paste_image": [],
    }
    assert key_bindings_for_config(None) is None
    assert key_bindings_for_config(PhosonConfig()) is None


def test_listing_display_for_chords_and_shift() -> None:
    from phoson_cli.fullscreen.keys import _display_sequence

    assert _display_sequence("c-x c-e") == "Ctrl+X Ctrl+E"
    assert _display_sequence("s-up") == "Shift+Up"
    assert _display_sequence("c-up") == "Ctrl+Up"


# ── config layer: load_key_bindings ───────────────────────────────────────────


def _write_keys(home: Path, keys_section: str) -> Path:
    (home / ".phoson").mkdir(parents=True, exist_ok=True)
    content = f'[defaults]\ntheme = "dark"\n\n[keys]\n{keys_section}'
    path = home / ".phoson" / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_key_bindings_no_file_returns_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_key_bindings() == {}


def test_load_key_bindings_no_keys_section(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".phoson").mkdir(parents=True)
    (tmp_path / ".phoson" / "config.toml").write_text(
        '[defaults]\ntheme = "dark"\n', encoding="utf-8"
    )
    assert load_key_bindings() == {}


def test_load_key_bindings_single_string_and_list(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_keys(
        tmp_path, 'toggle_reasoning = "c-x"\nline_up = ["s-up", "c-up"]\n'
    )
    loaded = load_key_bindings(path)
    assert loaded == {"toggle_reasoning": ["c-x"], "line_up": ["s-up", "c-up"]}


def test_load_key_bindings_chord_sequence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_keys(tmp_path, 'submit = "c-x c-e"\n')
    assert load_key_bindings(path) == {"submit": ["c-x c-e"]}


def test_load_key_bindings_empty_string_unbinds(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_keys(tmp_path, 'newline = ""\n')
    assert load_key_bindings(path) == {"newline": []}


def test_load_key_bindings_empty_table_is_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_keys(tmp_path, "")
    assert load_key_bindings(path) == {}


def test_load_key_bindings_unknown_action(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_keys(tmp_path, 'bogus = "c-x"\n')
    with pytest.raises(PhosonKeyBindingsError, match="Unknown key action 'bogus'"):
        load_key_bindings(path)


def test_load_key_bindings_invalid_sequence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_keys(tmp_path, 'toggle_reasoning = "ctrl+shift"\n')
    with pytest.raises(PhosonKeyBindingsError, match="Invalid key sequence"):
        load_key_bindings(path)


def test_load_key_bindings_wrong_type(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_keys(tmp_path, "newline = 42\n")
    with pytest.raises(PhosonKeyBindingsError, match="expected a string"):
        load_key_bindings(path)


def test_load_key_bindings_non_string_list_entry(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_keys(tmp_path, 'newline = ["c-j", 7]\n')
    with pytest.raises(PhosonKeyBindingsError, match="must be strings"):
        load_key_bindings(path)


def test_load_key_bindings_empty_list_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_keys(tmp_path, "newline = []\n")
    with pytest.raises(PhosonKeyBindingsError, match="empty list"):
        load_key_bindings(path)


# ── config layer: load_config integration ─────────────────────────────────────


def test_load_config_key_bindings_none_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_config().key_bindings is None


def test_load_config_reads_keys_section(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_keys(tmp_path, 'toggle_reasoning = "c-x"\n')
    cfg = load_config()
    assert cfg.key_bindings == {"toggle_reasoning": ["c-x"]}


def test_load_config_empty_keys_table_is_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_keys(tmp_path, "")
    assert load_config().key_bindings is None


def test_load_config_keys_error_surfaces_as_config_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_keys(tmp_path, 'bogus = "c-x"\n')
    with pytest.raises(PhosonKeyBindingsError):
        load_config()
    # PhosonKeyBindingsError *is* a PhosonConfigError (friendly in main)
    assert issubclass(PhosonKeyBindingsError, PhosonConfigError)


def test_save_config_preserves_user_keys_section(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = _write_keys(tmp_path, 'toggle_reasoning = "c-x"\n')
    cfg = load_config()
    save_config(cfg)
    text = cfg_path.read_text(encoding="utf-8")
    # The [keys] section is user-managed: a full save must keep it verbatim.
    assert 'toggle_reasoning = "c-x"' in text
    reloaded = load_config()
    assert reloaded.key_bindings == {"toggle_reasoning": ["c-x"]}


def test_save_config_narrow_save_preserves_user_keys_section(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = _write_keys(tmp_path, 'toggle_reasoning = "c-x"\n')
    cfg = load_config()
    cfg.theme = "light"
    save_config(cfg, only_fields={"theme"})
    text = cfg_path.read_text(encoding="utf-8")
    assert 'toggle_reasoning = "c-x"' in text
    reloaded = load_config()
    assert reloaded.key_bindings == {"toggle_reasoning": ["c-x"]}
    assert reloaded.theme == "light"


def test_key_bindings_never_written_by_save_config(monkeypatch, tmp_path) -> None:
    """``key_bindings`` is not a managed [defaults] key (E6).

    The [keys] section is user-managed (like permissions.json): a save
    must never emit a ``key_bindings = ...`` line, so a stale managed
    value can never shadow a hand-edited [keys] table.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = _write_keys(tmp_path, 'toggle_reasoning = "c-x"\n')
    cfg = load_config()
    save_config(cfg)
    text = cfg_path.read_text(encoding="utf-8")
    assert "key_bindings =" not in text
    # The hand-written [keys] section survives the save.
    assert 'toggle_reasoning = "c-x"' in text


# ── main(): friendly failure on a bad [keys] ──────────────────────────────────


def test_main_fails_friendly_on_conflicting_keys(monkeypatch, tmp_path, capsys) -> None:
    import sys

    import phoson_cli.__main__ as main_module

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    (home / ".phoson" / "config.toml").write_text(
        '[defaults]\ntheme = "dark"\n\n[keys]\nsubmit = "c-t"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "argv", ["phoson-cli"])
    monkeypatch.setattr(main_module, "load_config", lambda: load_config())
    monkeypatch.setattr(main_module, "build_chat", lambda config: None)
    monkeypatch.setattr(main_module, "has_configured_provider", lambda c: True)
    monkeypatch.setattr(main_module, "_should_use_classic", lambda opts: False)

    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Conflicting key bindings" in err
    assert "Traceback" not in err


# ── /keys command ─────────────────────────────────────────────────────────────


def test_keys_command_registered() -> None:
    from phoson_cli.commands import COMMANDS, COMMAND_SPECS

    assert "/keys" in COMMANDS
    spec = next(s for s in COMMAND_SPECS if "/keys" in s.names)
    assert spec.method == "_cmd_keys"


def test_keys_in_help_config_category() -> None:
    from phoson_cli.commands import get_grouped_command_help

    grouped = dict(get_grouped_command_help())
    config_cmds = {name for name, _ in grouped["Config & System"]}
    assert "/keys" in config_cmds


def test_cmd_keys_prints_the_effective_map(tmp_path) -> None:
    import asyncio

    from phoson_cli.repl import PhosonRepl
    from phoson_cli.commands import Command, CommandHandler

    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=tmp_path,
            history_file=tmp_path / "history.txt",
            key_bindings={"toggle_reasoning": ["c-x"], "paste_image": []},
        )
        repl = PhosonRepl(config)

    lines: list[str] = []

    class _Host:
        def print_info(self, message: str) -> None:
            lines.append(message)

        def print_warn(self, message: str) -> None:
            lines.append(message)

        def print_error(self, message: str) -> None:
            lines.append(message)

        def print_help(self, entries: object) -> None:
            lines.append(str(entries))

        def print_renderable(self, renderable: object) -> None:
            lines.append(str(renderable))

    handler = CommandHandler(repl, host=_Host())
    asyncio.run(handler.handle(Command(name="/keys", args="")))

    joined = "\n".join(lines)
    assert "Key bindings (full-screen TUI):" in joined
    assert "toggle_reasoning" in joined
    # The remap is what gets listed, not the default.
    assert "Ctrl+X" in joined
    # The unbound action shows as off.
    assert "(off)" in joined
    # Remap instructions are shown.
    assert "[keys]" in joined
    assert "config.toml" in joined


def test_cmd_keys_dispatchable_in_both_front_ends(tmp_path) -> None:
    """Same CommandHandler in the classic REPL — /keys dispatches there too."""
    from phoson_cli.repl import PhosonRepl
    from phoson_cli.commands import CommandHandler

    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(provider="ollama", sessions_dir=tmp_path)
        repl = PhosonRepl(config)
        handler = CommandHandler(repl)
    assert "/keys" in handler._dispatch
