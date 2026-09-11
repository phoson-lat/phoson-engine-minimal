"""Regression tests for the offline bench runner (issue #138 / H-0).

``bench/run_bench.py`` must actually apply ``--model``/``--provider`` to
the one-shot subprocess it spawns. The original bug: it injected
``PHOSON_MODEL_OVERRIDE`` / ``PHOSON_PROVIDER_OVERRIDE``, env vars that do
not exist anywhere in the CLI — so the flags were silently ignored and
any model comparison made through the runner was invalid (it pinned
nothing). The real knobs are ``PHOSON_MODEL`` / ``PHOSON_PROVIDER``
(``phoson_cli/config.py`` resolves them env → config.toml → default).

We test the runner's seams directly (``_build_env`` and
``_results_payload``) without spawning the CLI: the contract is that the
subprocess env carries the effective model/provider — and that a value
inherited from the developer's own shell cannot leak in and quietly
re-target a baseline run.
"""

import json
import importlib.util
from pathlib import Path

import pytest

_BENCH_DIR = Path(__file__).resolve().parent.parent / "bench"


@pytest.fixture(scope="module")
def bench():
    """Load ``bench/run_bench.py`` by path (bench is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "run_bench", _BENCH_DIR / "run_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_env_pins_model_and_provider(bench) -> None:
    """--model/--provider must land in the subprocess env as the env vars
    the CLI actually reads (issue #138)."""
    env = bench._build_env("openai/gpt-4o-mini", "openrouter")
    assert env["PHOSON_MODEL"] == "openai/gpt-4o-mini"
    assert env["PHOSON_PROVIDER"] == "openrouter"
    # The dead override vars must be gone.
    assert "PHOSON_MODEL_OVERRIDE" not in env
    assert "PHOSON_PROVIDER_OVERRIDE" not in env


def test_build_env_without_flags_injects_nothing(bench) -> None:
    """No flags → the run deterministically uses the user's config.toml
    (no env pin), so a bare baseline isn't re-targeted by the runner."""
    env = bench._build_env(None, None)
    assert "PHOSON_MODEL" not in env
    assert "PHOSON_PROVIDER" not in env


def test_build_env_pins_win_over_inherited_shell_value(bench, monkeypatch) -> None:
    """A developer shell exporting PHOSON_MODEL must not contaminate a
    baseline: the runner's --model wins, and with no flag the inherited
    value is dropped so config.toml is the deterministic fallback."""
    monkeypatch.setenv("PHOSON_MODEL", "leak-from-dev-shell")

    pinned = bench._build_env("pinned/model", None)
    assert pinned["PHOSON_MODEL"] == "pinned/model"

    unflagged = bench._build_env(None, None)
    assert "PHOSON_MODEL" not in unflagged  # the leak is popped


def test_cli_resolves_pinned_env_over_config(bench, monkeypatch, tmp_path) -> None:
    """End-to-end on the real config seam: with the bench's env applied,
    ``load_config`` returns exactly the pinned model/provider — proving
    the override the runner injects is the one the CLI honors."""
    monkeypatch.setenv("PHOSON_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("PHOSON_PROVIDER", "openrouter")
    # A config.toml that *would* win if the env did not.
    (tmp_path / "config.toml").write_text(
        '[defaults]\nmodel = "ollama/qwen3"\nprovider = "ollama"\n'
    )

    from phoson_cli import config as phoson_config

    fd = phoson_config._load_file_defaults(tmp_path / "config.toml")
    d = phoson_config.PhosonConfig()
    model = phoson_config._resolve_str("PHOSON_MODEL", "model", fd, d.model)
    provider = phoson_config._resolve_str(
        "PHOSON_PROVIDER", "provider", fd, d.provider
    ).lower()

    assert model == "openai/gpt-4o-mini"  # env beats config.toml
    assert provider == "openrouter"


def test_results_payload_records_effective_model_and_commit(bench) -> None:
    """Every saved JSON must carry the effective model/provider + the git
    commit it ran under, so a baseline is auditable (issue #138)."""
    r = bench.TaskResult(
        name="csv-stats",
        passed=True,
        duration_s=1.0,
        exit_code=0,
        detail="ok",
    )
    payload = bench._results_payload("openai/gpt-4o-mini", "openrouter", [r])
    assert payload["model"] == "openai/gpt-4o-mini"
    assert payload["provider"] == "openrouter"
    assert payload["commit"]  # non-empty short sha (or "unknown" off-repo)
    assert payload["pass_rate"] == 1.0
    assert payload["results"][0]["name"] == "csv-stats"


def _res(bench, name: str, passed: bool):
    """A minimal TaskResult (only the gate reads .name/.passed)."""
    return bench.TaskResult(
        name=name, passed=passed, duration_s=0.0, exit_code=0, detail=""
    )


def test_run_gate_bootstraps_when_no_baseline(bench, tmp_path) -> None:
    """First real run seeds the baseline and passes (issue #139)."""
    p = tmp_path / "baseline.json"
    results = [
        _res(bench, "a#1", True),
        _res(bench, "b#1", False),
        _res(bench, "a#2", True),
        _res(bench, "b#2", False),
    ]
    rc = bench.run_gate(results, baseline_path=p, model="m", provider="p")
    assert rc == 0

    doc = json.loads(p.read_text())
    assert doc["model"] == "m" and doc["provider"] == "p"
    assert doc["pass_rate"] == 0.5  # a passes, b fails every run
    assert doc["per_task"] == {"a": 1.0, "b": 0.0}


def test_run_gate_passes_when_above_baseline(bench, tmp_path) -> None:
    p = tmp_path / "baseline.json"
    p.write_text(
        json.dumps(
            {
                "pass_rate": 0.5,
                "noise": 0.0,
                "commit": "abc",
                "model": "m",
                "provider": "p",
                "per_task": {"a": 1.0, "b": 0.0},
            }
        )
    )
    # current: a + b both pass now → 1.0 > 0.5
    results = [_res(bench, "a#1", True), _res(bench, "b#1", True)]
    assert bench.run_gate(results, baseline_path=p) == 0


def test_run_gate_fails_on_regression(bench, tmp_path) -> None:
    p = tmp_path / "baseline.json"
    p.write_text(
        json.dumps(
            {
                "pass_rate": 1.0,
                "noise": 0.0,
                "commit": "abc",
                "model": "m",
                "provider": "p",
                "per_task": {"a": 1.0, "b": 1.0},
            }
        )
    )
    # current drops to 0.5 → below baseline 1.0 − 0 → regression
    results = [_res(bench, "a#1", True), _res(bench, "b#1", False)]
    assert bench.run_gate(results, baseline_path=p) == 1


def test_run_gate_perfect_baseline_tie_passes(bench, tmp_path) -> None:
    """A perfect (1.0) baseline with a perfect current run and no noise must
    PASS, not false-positive as a regression. The old strictly-greater rule
    could never exceed 1.0, so a stable-perfect model red-flagged every run
    (the symmetric twin of the bottomed-out 0.0 case)."""
    p = tmp_path / "baseline.json"
    p.write_text(
        json.dumps(
            {
                "pass_rate": 1.0,
                "noise": 0.0,
                "commit": "abc",
                "model": "m",
                "provider": "p",
                "per_task": {"a": 1.0, "b": 1.0},
            }
        )
    )
    # current stays perfect (1.0) → at the floor (1.0 − 0) → pass
    results = [_res(bench, "a#1", True), _res(bench, "b#1", True)]
    assert bench.run_gate(results, baseline_path=p) == 0


def test_run_gate_bottomed_baseline_tie_passes(bench, tmp_path) -> None:
    """A bottomed-out (0.0) baseline with a 0.0 current run must PASS, not
    false-positive. A model that reliably scores 0 (e.g. a too-small local
    model) used to red-flag every run because the strict rule demanded
    current > 0.0 — nothing can score below 0."""
    p = tmp_path / "baseline.json"
    p.write_text(
        json.dumps(
            {
                "pass_rate": 0.0,
                "noise": 0.0,
                "commit": "abc",
                "model": "m",
                "provider": "p",
                "per_task": {"a": 0.0, "b": 0.0},
            }
        )
    )
    # current stays 0.0 → at the floor (0.0 − 0) → pass
    results = [_res(bench, "a#1", False), _res(bench, "b#1", False)]
    assert bench.run_gate(results, baseline_path=p) == 0


def test_run_gate_bootstraps_on_null_sentinel(bench, tmp_path) -> None:
    """A committed sentinel (pass_rate: null) is treated as 'no baseline'."""
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"pass_rate": None, "status": "pending"}))
    results = [_res(bench, "a#1", True), _res(bench, "b#1", True)]
    assert bench.run_gate(results, baseline_path=p, model="m", provider="p") == 0
    assert json.loads(p.read_text())["pass_rate"] == 1.0  # seeded


def test_run_gate_bootstrap_flag_deliberately_reseeds(bench, tmp_path) -> None:
    """--bootstrap overwrites an existing baseline (maintainer re-seed)."""
    p = tmp_path / "baseline.json"
    p.write_text(
        json.dumps(
            {
                "pass_rate": 0.2,
                "noise": 0.0,
                "commit": "old",
                "model": "m",
                "provider": "p",
                "per_task": {"a": 0.0, "b": 0.0},
            }
        )
    )
    # Without --bootstrap this would GATE against 0.2; with it, re-seed.
    results = [_res(bench, "a#1", True), _res(bench, "b#1", True)]
    assert bench.run_gate(results, baseline_path=p, bootstrap=True) == 0
    assert json.loads(p.read_text())["pass_rate"] == 1.0  # re-seeded


def test_run_gate_without_bootstrap_flags_against_existing(bench, tmp_path) -> None:
    """A real (non-null) baseline is gated, never silently re-seeded."""
    p = tmp_path / "baseline.json"
    p.write_text(
        json.dumps(
            {
                "pass_rate": 1.0,
                "noise": 0.0,
                "commit": "abc",
                "model": "m",
                "provider": "p",
                "per_task": {"a": 1.0},
            }
        )
    )
    # current 0.0 < 1.0 → regression; and the baseline must NOT change.
    results = [_res(bench, "a#1", False)]
    assert bench.run_gate(results, baseline_path=p) == 1
    assert json.loads(p.read_text())["pass_rate"] == 1.0  # untouched


def test_run_gate_reports_heldout_separately(bench, tmp_path, capsys) -> None:
    """The held-out subset's pass rate is reported on its own line (issue
    #139: 'held-out reportado aparte'), without changing the verdict."""
    p = tmp_path / "baseline.json"
    p.write_text(
        json.dumps(
            {
                "pass_rate": 0.5,
                "noise": 0.0,
                "commit": "abc",
                "model": "m",
                "provider": "p",
                "per_task": {},
            }
        )
    )
    # a passes, b fails → full rate 0.5; held-out = {b} → 0.0
    results = [_res(bench, "a#1", True), _res(bench, "b#1", False)]
    rc = bench.run_gate(results, baseline_path=p, heldout={"b"})
    # 0.5 reaches the 0.5 baseline — equality at the floor now passes (the
    # gate only rejects a strict drop below it). The held-out line is
    # reported independently of the verdict.
    assert rc == 0
    out = capsys.readouterr().out
    assert "held-out (1 tasks): 0.000" in out


def test_committed_baseline_is_well_formed(bench) -> None:
    """The committed bench/baseline.json must be in a valid lifecycle state.

    issue #139: the committed file is the *live* baseline — the nightly
    overwrites the bootstrap sentinel (``pass_rate: null``) with a recorded
    baseline on its first real run. So the committed file is valid in
    EITHER state: the bootstrap sentinel (no baseline yet → first real run
    self-seeds) or a recorded baseline (a pass rate in [0, 1]). The old
    ``..._is_none`` assertion only held pre-bootstrap and broke main the
    moment the nightly seeded a real baseline."""
    src = _BENCH_DIR / "baseline.json"
    if not src.exists():
        pytest.skip("committed baseline.json not present")
    doc = json.loads(src.read_text())
    rate = bench.B.baseline_rate(doc)  # B is run_bench's baseline mod
    assert rate is None or 0.0 <= rate <= 1.0
    assert doc.get("status") in (None, "pending", "recorded")


def test_load_tasks_heldout_split(bench, monkeypatch, tmp_path) -> None:
    """heldout.txt names the never-iterate subset; --no-heldout skips it
    (issue #139)."""
    (tmp_path / "heldout.txt").write_text("# comment\nrename-symbol\n\n")
    monkeypatch.setattr(bench, "HELDOUT_PATH", tmp_path / "heldout.txt")

    all_names = [t["name"] for t in bench.load_tasks(None)]
    train = [t["name"] for t in bench.load_tasks(None, include_heldout=False)]
    assert "rename-symbol" in all_names
    assert "rename-symbol" not in train
    assert len(train) == len(all_names) - 1


# ── effective model/provider for the audit record (issue #139) ─────────────


def test_effective_target_pinned_wins_over_config(bench, tmp_path) -> None:
    """--model/--provider pin the audit record; config.toml is not read."""
    (tmp_path / "config.toml").write_text(
        '[defaults]\nmodel = "ollama/qwen3"\nprovider = "ollama"\n'
    )
    assert bench._effective_target(
        "openai/gpt-4o", "openrouter", tmp_path / "config.toml"
    ) == ("openai/gpt-4o", "openrouter")
    # Partial pin: the unpinned side still resolves from config.toml.
    assert bench._effective_target("openai/gpt-4o", None, tmp_path / "config.toml") == (
        "openai/gpt-4o",
        "ollama",
    )


def test_effective_target_resolves_config_when_unpinned(
    bench, tmp_path, monkeypatch
) -> None:
    """No flags → the record carries what the child CLI actually resolves
    (config.toml). A dev-shell PHOSON_MODEL/PHOSON_PROVIDER must NOT leak in:
    the runner pops those from the child env, so the record must ignore them
    too (issue #139: baselines auditable)."""
    (tmp_path / "config.toml").write_text(
        '[defaults]\nmodel = "omni/route"\nprovider = "OmniRoute"\n'
    )
    monkeypatch.setenv("PHOSON_MODEL", "leak-from-dev-shell")
    monkeypatch.setenv("PHOSON_PROVIDER", "leak-from-dev-shell")
    model, provider = bench._effective_target(None, None, tmp_path / "config.toml")
    assert model == "omni/route"
    assert provider == "omniroute"  # the CLI lowercases the provider


def test_effective_target_missing_config_uses_built_in_defaults(
    bench,
    tmp_path,
) -> None:
    """No config.toml → the built-in PhosonConfig defaults, exactly what the
    child CLI falls back to."""
    from phoson_cli import config as pc

    d = pc.PhosonConfig()
    assert bench._effective_target(None, None, tmp_path / "config.toml") == (
        d.model,
        d.provider.lower(),
    )


def test_effective_target_malformed_config_degrades_to_unknown(bench, tmp_path) -> None:
    """A malformed config.toml must not kill the bench; the record degrades
    to 'unknown' (the child run itself would fail loudly anyway)."""
    (tmp_path / "config.toml").write_text("[defaults\nmodel = oops")
    assert bench._effective_target(None, None, tmp_path / "config.toml") == (
        "unknown",
        "unknown",
    )
