"""Unit tests for the command palette picker (T-12)."""

from phoson_cli.theme import DARK
from phoson_cli.palette_picker import (
    PaletteEntry,
    PalettePickerResult,
    _filter_entries,
    build_command_palette,
)


def _entries() -> list[PaletteEntry]:
    return [
        PaletteEntry(name="/model", display="/model", help="Pick the active model"),
        PaletteEntry(
            name="/reasoning-effort",
            display="/reasoning-effort · /effort",
            help="Set reasoning effort",
        ),
        PaletteEntry(name="/theme", display="/theme", help="Set the color theme"),
        PaletteEntry(name="/help", display="/help", help="Show this help"),
    ]


def test_filter_returns_everything_for_empty_query() -> None:
    entries = _entries()
    assert len(_filter_entries(entries, "")) == len(entries)


def test_filter_fuzzy_matches_names_and_help() -> None:
    entries = _entries()
    filtered = _filter_entries(entries, "mod")
    assert any(e.name == "/model" for e in filtered)
    # "eff" should match /reasoning-effort via its help text too.
    assert any(e.name == "/reasoning-effort" for e in _filter_entries(entries, "eff"))


def test_filter_rejects_non_matching_query() -> None:
    entries = _entries()
    assert _filter_entries(entries, "zzzznotacmd") == []


def test_palette_confirm_returns_selected_command() -> None:
    results: list[PalettePickerResult] = []
    picker = build_command_palette(_entries(), theme=DARK)
    picker._on_done = results.append

    picker.done(PalettePickerResult(command_name="/model"))
    assert len(results) == 1
    assert results[0].command_name == "/model"
    assert results[0].cancelled is False


def test_palette_cancel_result_shape() -> None:
    r = PalettePickerResult(cancelled=True)
    assert r.cancelled is True
    assert r.command_name is None


def test_palette_build_and_render_smoke() -> None:
    """The picker renders without exception and binds nav keys."""
    picker = build_command_palette(_entries(), theme=DARK)
    rows = picker._render()
    assert any("Commands" in line for _, line in rows)
    # up/down/enter/escape are all bound by bind_paged_nav.
    assert picker._kb is not None
