"""Unit tests for the no-regression gate + baseline logic (issue #139 / H-1).

The gate is pure and model-free, so these tests run in CI without any
local model or API key. They pin the gate's decision table: bootstrap,
pass, regression, the tie-rejection rule, and the noise-floor behavior.
"""

import sys
from pathlib import Path

import pytest

_BENCH_DIR = Path(__file__).resolve().parent.parent / "bench"
sys.path.insert(0, str(_BENCH_DIR))
import baseline as B  # noqa: E402


def _Fake(name: str, passed: bool):
    """Minimal stand-in for ``bench.TaskResult`` (only .name/.passed)."""
    return type("R", (), {"name": name, "passed": passed})()


# ── noise ──────────────────────────────────────────────────────────────────────


def test_noise_zero_when_all_equal() -> None:
    assert B.noise([1.0, 1.0, 1.0]) == 0.0


def test_noise_single_value_is_zero() -> None:
    assert B.noise([1.0]) == 0.0


def test_noise_positive_on_spread() -> None:
    assert B.noise([1.0, 1.0, 0.5]) > 0.0


# ── gate decision table ────────────────────────────────────────────────────────


def test_bootstrap_when_no_baseline() -> None:
    v = B.evaluate([1.0, 1.0, 1.0], None)
    assert v.ok and v.status == "bootstrap" and v.baseline is None
    assert v.current == 1.0


def test_no_runs_is_a_regression() -> None:
    v = B.evaluate([], 1.0)
    assert not v.ok and v.status == "regression" and v.current is None


def test_pass_when_above_baseline_minus_noise() -> None:
    v = B.evaluate([1.0, 1.0, 1.0], 0.8)
    assert v.ok and v.status == "pass"
    assert v.threshold == 0.8  # noise 0 → threshold == baseline


def test_regression_when_below_baseline_minus_noise() -> None:
    v = B.evaluate([0.5, 0.5, 0.5], 1.0)
    assert not v.ok and v.status == "regression"
    assert v.threshold == 1.0


def test_tie_is_rejected() -> None:
    # current exactly equals baseline − noise (noise 0) → NOT a pass.
    v = B.evaluate([0.8, 0.8, 0.8], 0.8)
    assert not v.ok and v.status == "regression"


def test_noise_floor_widens_threshold() -> None:
    # Current run has spread; floor > 0 lowers the threshold, so a rate
    # that would fail against a zero-noise floor can pass.
    # runs [1,1,0.5] → mean 0.833, population std √(1/18) ≈ 0.2357
    # floor .2357, threshold 1 − .2357 = .7643 → 0.833 > .7643 → pass
    # (would fail against the zero-noise floor, since 0.833 < 1.0)
    import math

    v = B.evaluate([1.0, 1.0, 0.5], 1.0)
    assert v.ok and v.status == "pass"
    assert v.noise == pytest.approx(math.sqrt(1 / 18))
    assert v.threshold == pytest.approx(1.0 - math.sqrt(1 / 18))


def test_larger_of_baseline_noise_used() -> None:
    # baseline noise 0.3 > current noise 0 → floor 0.3
    v = B.evaluate([0.8, 0.8, 0.8], 1.0, baseline_noise=0.3)
    assert v.noise == pytest.approx(0.3)
    assert v.threshold == pytest.approx(0.7)
    assert v.ok  # 0.8 > 0.7


def test_min_margin_adds_strictness() -> None:
    v = B.evaluate([0.9, 0.9, 0.9], 0.8, min_margin=0.2)
    # floor .2, threshold 0.6, 0.9 > 0.6 → pass
    assert v.ok and v.threshold == pytest.approx(0.6)


# ── per-task deltas (falsifiable contract) ─────────────────────────────────────


def test_task_deltas_signs() -> None:
    d = B.task_deltas(
        current={"a": 1.0, "b": 0.5, "c": 1.0},
        baseline={"a": 0.0, "b": 0.5, "c": 1.0},
    )
    assert d["a"] == pytest.approx(1.0)  # fixed
    assert d["b"] == 0.0  # stable
    assert d["c"] == 0.0


def test_task_deltas_new_and_removed_tasks() -> None:
    d = B.task_deltas(current={"x": 1.0}, baseline={"y": 1.0})
    assert d["x"] == 1.0  # new task: 0 → 1
    assert d["y"] == -1.0  # removed: 1 → 0


# ── baseline I/O ───────────────────────────────────────────────────────────────


def test_bootstrap_baseline_document() -> None:
    doc = B.bootstrap_baseline("m", "p", [1.0, 1.0, 1.0], {"a": 1.0}, commit="abc")
    assert doc["model"] == "m" and doc["provider"] == "p"
    assert doc["commit"] == "abc"
    assert doc["pass_rate"] == 1.0 and doc["noise"] == 0.0
    assert doc["runs"] == 3 and doc["task_count"] == 1
    assert doc["per_task"] == {"a": 1.0}
    assert doc["status"] == "recorded"


def test_load_baseline_absent_returns_none(tmp_path: Path) -> None:
    assert B.load_baseline(tmp_path / "nope.json") is None


def test_load_and_save_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "baseline.json"
    doc = B.bootstrap_baseline("m", "p", [1.0, 0.5], {"a": 1.0, "b": 0.5})
    B.save_baseline(p, doc)
    loaded = B.load_baseline(p)
    assert loaded == doc
    assert B.baseline_rate(loaded) == doc["pass_rate"]
    assert B.baseline_noise(loaded) == doc["noise"]
    assert B.baseline_per_task(loaded) == doc["per_task"]


def test_load_baseline_invalid_json_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "baseline.json"
    p.write_text("{not json")
    assert B.load_baseline(p) is None


def test_baseline_rate_bootstrap_sentinel_is_none(tmp_path: Path) -> None:
    doc = {"pass_rate": None, "status": "pending"}
    assert B.baseline_rate(doc) is None
    assert B.baseline_noise(doc) is None
    assert B.baseline_per_task(doc) == {}


# ── run → per-run / per-task rates (runner seams) ─────────────────────────────


def test_run_pass_rates_infers_runs_from_repeat_labels() -> None:
    # 2 tasks × 3 repeats. The runner labels repeats 1-based: the run i
    # (0-based) carries label suffix "#(i+1)" (run_bench.main). Results
    # arrive interleaved as run0(task0..taskN), run1(...), ….
    results = []
    for i in range(1, 4):  # run labels #1..#3
        for t in ("a", "b"):  # tasks
            results.append(_Fake(f"{t}#{i}", passed=(t == "a")))
    rates = B.run_pass_rates(results)
    assert len(rates) == 3
    # each run: a passes (1), b fails (0) → 0.5
    assert rates == [0.5, 0.5, 0.5]


def test_run_pass_rates_single_run_bare_labels() -> None:
    results = [_Fake("a", True), _Fake("b", False)]
    assert B.run_pass_rates(results) == [0.5]


def test_run_per_task_rates_averages_across_repeats() -> None:
    results = [
        _Fake("a#1", True),
        _Fake("a#2", False),
        _Fake("b#1", True),
        _Fake("b#2", True),
    ]
    per = B.run_per_task_rates(results)
    assert per["a"] == pytest.approx(0.5)
    assert per["b"] == 1.0


def test_empty_results() -> None:
    assert B.run_pass_rates([]) == []
    assert B.run_per_task_rates([]) == {}
