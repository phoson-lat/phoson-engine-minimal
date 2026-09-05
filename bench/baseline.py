"""No-regression gate + baseline logic for the agent eval set (issue #139 / H-1).

This module is the *software-correctness* half of H-1: it is pure,
stdlib-only, and carries no LLM — so it is fully unit-testable without a
model. The nightly workflow feeds it the pass rates measured by
``run_bench.py --repeat N`` and gets a pass/fail verdict.

The gate is a **regression gate**, exactly as the issue specifies:

* **Fail** if the current pass rate drops *below* ``baseline − noise``.
* **Ties are rejected**: a current rate that merely *equals*
  ``baseline − noise`` does not pass (you must be strictly above the
  noise floor). This keeps a genuinely flat-but-noisy result from being
  waved through.
* **Bootstrap**: when there is no baseline yet (``pass_rate is None``),
  the run *records* the baseline instead of gating on it — the first
  real nightly run seeds ``bench/baseline.json``; later runs gate
  against it.

``noise`` is measured as the run-to-run spread of the current run's
per-run pass rates (population standard deviation). Using the current
run's own spread as the noise floor is the honest, self-calibrating
choice: a stable harness yields ``noise ≈ 0`` and the gate is tight; a
flaky harness widens its own floor so one dropped task doesn't
false-positive. The baseline may also carry its own recorded noise and
the larger of the two is used, so a noisier baseline can't make a
regression look acceptable.

The falsifiable contract (a harness PR declares which tasks it expects
to fix / put at risk) is supported by :func:`task_deltas`, which returns
per-task pass-rate movement so a PR's claims can be checked against the
next run.
"""

import json
import math
from typing import Any
from pathlib import Path
from datetime import UTC, datetime
from dataclasses import dataclass

#: Default location of the committed baseline.
DEFAULT_BASELINE = "baseline.json"

#: Below this, a difference is treated as zero (float-noise guard). The
#: "strictly above threshold" rule must not be defeated by a 1e-16
#: rounding artifact, so both the noise floor and the pass comparison
#: use this epsilon.
EPS = 1e-9


# ── Noise ──────────────────────────────────────────────────────────────────────


def population_std(values: list[float]) -> float:
    """Population standard deviation (0 for a single value or when the
    result is only float noise)."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    return sd if sd > EPS else 0.0


def noise(run_rates: list[float]) -> float:
    """The run-to-run noise floor for a set of per-run pass rates."""
    return population_std(run_rates)


# ── Gate verdict ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """Outcome of evaluating a run against a baseline.

    Attributes:
        ok: True when the run passes the gate (or is a bootstrap).
        status: ``"pass"`` | ``"regression"`` | ``"bootstrap"``.
        current: The current (mean) pass rate, or ``None`` if no runs.
        baseline: The baseline pass rate, or ``None`` during bootstrap.
        noise: The noise floor used for the comparison.
        threshold: ``baseline − noise`` (the floor the current rate must
            be strictly above), or ``None`` during bootstrap.
        detail: A one-line human explanation.
    """

    ok: bool
    status: str
    current: float | None
    baseline: float | None
    noise: float
    threshold: float | None
    detail: str


def evaluate(
    run_rates: list[float],
    baseline_rate: float | None,
    baseline_noise: float | None = None,
    min_margin: float = 0.0,
) -> Verdict:
    """Decide pass/fail for the current run against a baseline.

    Args:
        run_rates: Per-run pass rates from the current run (>=1; the
            nightly uses >=3 to measure noise).
        baseline_rate: The committed baseline pass rate, or ``None`` for
            bootstrap (no baseline yet).
        baseline_noise: The baseline's recorded noise floor; the larger
            of this and the current run's noise is used.
        min_margin: Optional extra strictness added to the noise floor.

    Rule: pass iff ``mean(current) > (baseline − floor)``; bootstrap
    (``baseline_rate is None``) always passes (and should be recorded).
    """
    if not run_rates:
        return Verdict(
            ok=False,
            status="regression",
            current=None,
            baseline=baseline_rate,
            noise=0.0,
            threshold=None,
            detail="no runs reported — nothing to evaluate",
        )

    current = sum(run_rates) / len(run_rates)

    if baseline_rate is None:
        return Verdict(
            ok=True,
            status="bootstrap",
            current=current,
            baseline=None,
            noise=noise(run_rates),
            threshold=None,
            detail=f"no baseline yet — bootstrapping at {current:.3f} "
            f"(noise {noise(run_rates):.3f})",
        )

    floor = max(noise(run_rates), baseline_noise or 0.0, 0.0) + min_margin
    threshold = baseline_rate - floor
    # Strictly-above with an epsilon: a rate merely *touching*
    # ``baseline − noise`` (within float noise) does not pass.
    ok = current > threshold + EPS
    status = "pass" if ok else "regression"
    detail = (
        f"current {current:.3f} vs baseline {baseline_rate:.3f} "
        f"(floor {floor:.3f}, threshold {threshold:.3f}) → {status}"
    )
    return Verdict(
        ok=ok,
        status=status,
        current=current,
        baseline=baseline_rate,
        noise=floor,
        threshold=threshold,
        detail=detail,
    )


# ── Per-task deltas (the falsifiable contract) ────────────────────────────────


def task_deltas(
    current: dict[str, float], baseline: dict[str, float]
) -> dict[str, float]:
    """Per-task pass-rate movement: ``current[t] − baseline[t]``.

    A harness PR declares which tasks it expects to fix (positive delta)
    and which it puts at risk (negative delta); this makes those claims
    machine-checkable against the next run. A task missing on either side
    is treated as 0 on that side (new task = 0→rate; removed = rate→0).
    """
    keys = set(current) | set(baseline)
    return {k: current.get(k, 0.0) - baseline.get(k, 0.0) for k in sorted(keys)}


# ── Baseline I/O ───────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def bootstrap_baseline(
    model: str,
    provider: str,
    run_rates: list[float],
    per_task: dict[str, float],
    commit: str = "unknown",
) -> dict[str, Any]:
    """Build a baseline document from the first real (bootstrap) run."""
    current = sum(run_rates) / len(run_rates) if run_rates else 0.0
    return {
        "model": model,
        "provider": provider,
        "commit": commit,
        "date": _now_iso(),
        "runs": len(run_rates),
        "pass_rate": current,
        "noise": noise(run_rates),
        "task_count": len(per_task),
        "per_task": dict(sorted(per_task.items())),
        "status": "recorded",
    }


def load_baseline(path: str | Path) -> dict[str, Any] | None:
    """Read a baseline JSON file; ``None`` when absent or unreadable.

    A bootstrap sentinel (``pass_rate`` is ``null``) is returned as-is so
    the caller can detect the "record me" state via ``pass_rate is None``.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_baseline(path: str | Path, document: dict[str, Any]) -> Path:
    """Atomically write a baseline document. Returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(p)
    return p


def baseline_rate(doc: dict[str, Any] | None) -> float | None:
    """The baseline pass rate, or ``None`` (absent / bootstrap sentinel)."""
    if not doc:
        return None
    rate = doc.get("pass_rate")
    return float(rate) if isinstance(rate, (int, float)) else None


def baseline_noise(doc: dict[str, Any] | None) -> float | None:
    if not doc:
        return None
    n = doc.get("noise")
    return float(n) if isinstance(n, (int, float)) else None


def baseline_per_task(doc: dict[str, Any] | None) -> dict[str, float]:
    if not doc:
        return {}
    per = doc.get("per_task") or {}
    return {k: float(v) for k, v in per.items()}


def run_pass_rates(results: list[Any]) -> list[float]:
    """Turn a flat list of per-(repeat × task) results into per-run rates.

    ``results`` is in the runner's order: run1(task0..taskN), run2(...),
    … (see ``run_bench.main``). Repeated runs are labelled
    ``"<task>#<i>"`` (1-based, matching the runner); a bare name
    (``repeat == 1``) is run 1. The per-run rate is the fraction of that
    run's tasks that passed.
    """
    if not results:
        return []

    # Repeated labels: "<task>#<i>" when repeat>1, else "<task>" → run 1.
    def run_index(label: str) -> int:
        if "#" in label:
            return int(label.rsplit("#", 1)[1])
        return 1

    run_of: dict[int, list[bool]] = {}
    for r in results:
        run_of.setdefault(run_index(r.name), []).append(bool(r.passed))
    rates = []
    for i in sorted(run_of):
        passed = sum(run_of[i])
        rates.append(passed / len(run_of[i]) if run_of[i] else 0.0)
    return rates


def run_per_task_rates(results: list[Any]) -> dict[str, float]:
    """Per-task pass rate across all repeats (mean of that task's runs)."""
    by_task: dict[str, list[bool]] = {}
    for r in results:
        base = r.name.rsplit("#", 1)[0]
        by_task.setdefault(base, []).append(bool(r.passed))
    return {name: (sum(v) / len(v) if v else 0.0) for name, v in by_task.items()}
