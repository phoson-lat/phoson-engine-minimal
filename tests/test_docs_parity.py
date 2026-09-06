"""Tests de ``scripts/check_docs_parity.py`` (issue #146).

El script vive en ``scripts/`` (no es un paquete), así que se importa vía
``importlib`` desde su ruta. ``httpx.get`` se mockea en todos los tests que
toca la API: no se hace ninguna llamada real a GitHub.
"""

import importlib.util
from types import SimpleNamespace
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_docs_parity.py"
_spec = importlib.util.spec_from_file_location("check_docs_parity", _SCRIPT)
assert _spec and _spec.loader
check_docs_parity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docs_parity)

ROADMAP_LINE = (
    "| [#138](https://github.com/phoson-lat/phoson-engine-minimal/issues/138) "
    "| Bench ignora `--model` | H-0 | — | — | ✅ **cerrado** |"
)
ROADMAP_OPEN_LINE = (
    "| [#142](https://github.com/phoson-lat/phoson-engine-minimal/issues/142) "
    "| Doom loop detection | H-3 | 🟠 | S-M | **D** | Hipótesis; medir contra #139. |"
)


def _fake_get(handler):
    """Construye un sustituto de ``httpx.get`` que delega en ``handler(url)``."""

    def _get(url, **kwargs):
        return handler(url)

    return _get


def test_parse_roadmap_extracts_issues():
    state = check_docs_parity.parse_roadmap(f"{ROADMAP_LINE}\n{ROADMAP_OPEN_LINE}\n")
    assert state[138] is True
    assert state[142] is False


def test_parse_ignores_unrecognized_lines():
    # No recognizable issue links → nothing extracted (no crash, no false positive).
    assert check_docs_parity.parse_roadmap("no issues here, just text\n") == {}
    # Label and URL number disagree → skipped.
    text = "[#138](https://github.com/phoson-lat/phoson-engine-minimal/issues/999) ✅"
    assert check_docs_parity.parse_roadmap(text) == {}


def test_parse_improvements_extracts_issues():
    text = (
        "| **H-0** | Bug de verificación | P0 | S | 🔴 | "
        "[#138](https://github.com/phoson-lat/phoson-engine-minimal/issues/138) "
        "| ✅ Resuelto (post-v0.20.0) |\n"
        "| **H-3** | Doom loops | P1 | S-M | 🟠 | "
        "[#142](https://github.com/phoson-lat/phoson-engine-minimal/issues/142) "
        "| Sprint 2 |\n"
    )
    state = check_docs_parity.parse_improvements(text)
    assert state == {138: True, 142: False}


def test_detects_docs_closed_github_open():
    docs = {138: True}
    github = {138: False}
    issues = check_docs_parity.reconcile(docs, github)
    assert any("138" in i and "OPEN" in i for i in issues)


def test_detects_github_closed_docs_open():
    docs = {99: False}
    github = {99: True}
    issues = check_docs_parity.reconcile(docs, github)
    assert any("99" in i for i in issues)


def test_no_issues_when_in_sync():
    docs = {1: True, 2: False}
    github = {1: True, 2: False}
    assert check_docs_parity.reconcile(docs, github) == []


def test_reconcile_multiple_issues():
    docs = {1: True, 2: False, 3: True, 4: False}
    github = {1: True, 2: True, 3: False, 4: False}
    issues = check_docs_parity.reconcile(docs, github)
    assert len(issues) == 2
    assert any("#2" in i for i in issues)  # GitHub closed, docs open
    assert any("#3" in i and "OPEN" in i for i in issues)  # docs ✅, GitHub open


def test_reconcile_skips_issues_missing_on_one_side():
    # Issue that 404s on GitHub (absent from github_state) → not reported.
    assert check_docs_parity.reconcile({50: True}, {}) == []


def test_check_github_maps_state(monkeypatch):
    def handler(url):
        num = url.rsplit("/", 1)[-1]
        state = "closed" if num == "138" else "open"
        return SimpleNamespace(status_code=200, json=lambda: {"state": state})

    monkeypatch.setattr(check_docs_parity.httpx, "get", _fake_get(handler))
    result = check_docs_parity.check_github({138: True, 142: False}, "token")
    assert result == {138: True, 142: False}


def test_check_github_skips_404(monkeypatch):
    def handler(url):
        return SimpleNamespace(status_code=404, json=lambda: {})

    monkeypatch.setattr(check_docs_parity.httpx, "get", _fake_get(handler))
    result = check_docs_parity.check_github({138: True}, "token")
    assert result == {}


def test_check_github_raises_on_http_error(monkeypatch):
    def handler(url):
        return SimpleNamespace(status_code=500, json=lambda: {})

    monkeypatch.setattr(check_docs_parity.httpx, "get", _fake_get(handler))
    with pytest.raises(check_docs_parity.InfrastructureError):
        check_docs_parity.check_github({138: True}, "token")


def test_check_github_raises_on_transport_error(monkeypatch):
    def handler(url):
        raise check_docs_parity.httpx.ConnectError("no route to host")

    monkeypatch.setattr(check_docs_parity.httpx, "get", _fake_get(handler))
    with pytest.raises(check_docs_parity.InfrastructureError):
        check_docs_parity.check_github({138: True}, "token")


def test_main_dry_run_without_docs(tmp_path, capsys):
    rc = check_docs_parity.main(["--dry-run", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ROADMAP.md not found" in out


def test_main_dry_run_with_docs(tmp_path, capsys):
    (tmp_path / "ROADMAP.md").write_text(f"{ROADMAP_LINE}\n", encoding="utf-8")
    rc = check_docs_parity.main(["--dry-run", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 issues referenced" in out
    assert "--dry-run" in out


def test_main_no_token_falls_back_to_dry_run(tmp_path, monkeypatch, capsys):
    (tmp_path / "ROADMAP.md").write_text(f"{ROADMAP_LINE}\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    rc = check_docs_parity.main(["--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "GITHUB_TOKEN is not set" in out
    assert "dry-run" in out


def test_main_reports_drift_exit_1(tmp_path, monkeypatch, capsys):
    (tmp_path / "ROADMAP.md").write_text(f"{ROADMAP_LINE}\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setattr(
        check_docs_parity.httpx,
        "get",
        _fake_get(
            lambda url: SimpleNamespace(status_code=200, json=lambda: {"state": "open"})
        ),
    )
    rc = check_docs_parity.main(["--repo-root", str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "#138" in out and "OPEN" in out


def test_main_infra_error_exit_2(tmp_path, monkeypatch, capsys):
    (tmp_path / "ROADMAP.md").write_text(f"{ROADMAP_LINE}\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")

    def handler(url):
        return SimpleNamespace(status_code=500, json=lambda: {})

    monkeypatch.setattr(check_docs_parity.httpx, "get", _fake_get(handler))
    rc = check_docs_parity.main(["--repo-root", str(tmp_path)])
    assert rc == 2
    assert "ERROR" in capsys.readouterr().err
