"""Unit tests for the frozen-binary helpers (IMPROVEMENTS.md I-93, #93).

Covers the runtime surface the standalone PyInstaller binary relies on:

1. ``asset_path`` — resolves package assets in both layouts:
   the source tree (next to ``__file__``) and the onefile bundle
   (``sys._MEIPASS/phoson_cli/``).
2. ``is_frozen`` — reports the frozen state from ``sys.frozen``.
3. ``frozen_version`` — prefers the build-injected
   ``_frozen_version.txt`` over the fallback.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from phoson_cli import _frozen

# ── asset_path ─────────────────────────────────────────────────────────────────


def test_asset_path_resolves_next_to_module(tmp_path, monkeypatch) -> None:
    """Source layout: the asset sits next to the module file."""
    monkeypatch.delattr("sys._MEIPASS", raising=False)
    asset = tmp_path / "phos-ascii.txt"
    asset.write_text("art", encoding="utf-8")
    with patch.object(_frozen, "__file__", str(tmp_path / "_frozen.py")):
        resolved = _frozen.asset_path("phos-ascii.txt")
    assert resolved == asset


def test_asset_path_prefers_meipass_bundle(monkeypatch) -> None:
    """Bundle layout: sys._MEIPASS/phoson_cli/<asset> wins over the
    module-adjacent copy (the source tree always has phos-ascii.txt)."""
    import sys as _sys
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        bundle = Path(td) / "phoson_cli"
        bundle.mkdir()
        (bundle / "phos-ascii.txt").write_text("bundled art", encoding="utf-8")
        monkeypatch.setattr(_sys, "_MEIPASS", td, raising=False)
        resolved = _frozen.asset_path("phos-ascii.txt")
        assert resolved == bundle / "phos-ascii.txt"
        assert resolved.read_text(encoding="utf-8") == "bundled art"


def test_asset_path_missing_raises(monkeypatch) -> None:
    monkeypatch.delattr("sys._MEIPASS", raising=False)
    with patch.object(_frozen, "__file__", "/nonexistent/dir/_frozen.py"):
        with pytest.raises(FileNotFoundError, match="phos-ascii.txt"):
            _frozen.asset_path("phos-ascii.txt")


def test_real_banner_asset_is_resolvable() -> None:
    """The actual banner art must be found from the real package layout."""
    art = _frozen.asset_path("phos-ascii.txt")
    assert art.exists()
    assert "═══" in art.read_text(encoding="utf-8")


# ── is_frozen ──────────────────────────────────────────────────────────────────


def test_is_frozen_reflects_sys_frozen(monkeypatch) -> None:
    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert _frozen.is_frozen() is True
    monkeypatch.delattr("sys.frozen", raising=False)
    assert _frozen.is_frozen() is False


# ── frozen_version ─────────────────────────────────────────────────────────────


def test_frozen_version_prefers_injected_file(tmp_path, monkeypatch) -> None:
    injected = tmp_path / "_frozen_version.txt"
    injected.write_text("0.15.0\n", encoding="utf-8")
    with patch.object(_frozen, "asset_path", return_value=injected):
        assert _frozen.frozen_version("1.2.3") == "0.15.0"


def test_frozen_version_falls_back_when_file_absent(monkeypatch) -> None:
    def missing(_name):
        raise FileNotFoundError("no bundle file")

    with patch.object(_frozen, "asset_path", side_effect=missing):
        assert _frozen.frozen_version("dev") == "dev"


def test_frozen_version_falls_back_on_empty_file(tmp_path, monkeypatch) -> None:
    empty = tmp_path / "_frozen_version.txt"
    empty.write_text("   \n", encoding="utf-8")
    with patch.object(_frozen, "asset_path", return_value=empty):
        assert _frozen.frozen_version("dev") == "dev"


# ── updater integration ────────────────────────────────────────────────────────


def test_get_current_version_uses_frozen_version(monkeypatch) -> None:
    from phoson_cli import updater

    monkeypatch.setattr(_frozen, "is_frozen", lambda: True)
    monkeypatch.setattr(_frozen, "frozen_version", lambda fb: "9.9.9")
    assert updater.get_current_version() == "9.9.9"


def test_get_current_version_ignores_injection_when_not_frozen(monkeypatch) -> None:
    from phoson_cli import updater

    monkeypatch.setattr(_frozen, "is_frozen", lambda: False)
    monkeypatch.setattr(_frozen, "frozen_version", lambda fb: "9.9.9")
    # Not frozen → the metadata path decides (whatever it is, the
    # injected 9.9.9 must not leak in).
    version = updater.get_current_version()
    assert version != "9.9.9"


def test_detect_install_mode_reports_frozen(monkeypatch) -> None:
    from phoson_cli import updater

    monkeypatch.setattr(_frozen, "is_frozen", lambda: True)
    assert updater.detect_install_mode() == updater.InstallMode.FROZEN


def test_manual_hint_for_frozen() -> None:
    from phoson_cli import updater

    hint = updater.manual_hint(updater.InstallMode.FROZEN)
    assert "Releases" in hint
