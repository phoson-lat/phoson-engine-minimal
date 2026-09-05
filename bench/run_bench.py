#!/usr/bin/env python3
"""Benchmark harness for phoson-cli one-shot mode.

Runs each task in an isolated temporary workspace, invoking the CLI the
same way a script would (`phoson-cli "task"`), then applies a
deterministic checker. Prints a summary table and saves JSON results.
"""

import os
import sys
import json
import time
import shutil
import argparse
import tempfile
import subprocess
import dataclasses
from pathlib import Path

TASKS_DIR = Path(__file__).parent / "tasks"
RESULTS_DIR = Path(__file__).parent / "results"
BASELINE_PATH = Path(__file__).parent / "baseline.json"
HELDOUT_PATH = Path(__file__).parent / "heldout.txt"


# Make ``baseline.py`` importable both when run as a script and when the
# tests import ``run_bench`` by path (bench is not a package).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import baseline as B  # noqa: E402


@dataclasses.dataclass
class TaskResult:
    name: str
    passed: bool
    duration_s: float
    exit_code: int
    detail: str = ""
    stdout_bytes: int = 0


def load_tasks(filter_sub: str | None, include_heldout: bool = True) -> list:
    """Load bench tasks.

    ``heldout.txt`` (one task name per line) names the held-out split —
    the subset a harness PR must *never* iterate against. When
    ``include_heldout`` is False those tasks are skipped, so a PR author
    can tune against the training split only and see the held-out result
    separately. Lines starting with ``#`` are comments.
    """
    heldout = _heldout_names() if not include_heldout else set()
    tasks = []
    for path in sorted(TASKS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        namespace: dict = {}
        exec(compile(path.read_text(), path.name, "exec"), namespace)  # noqa: S102
        name = namespace.get("NAME", path.stem)
        if not include_heldout and name in heldout:
            continue
        task = {
            "name": name,
            "module": namespace,
        }
        if filter_sub and filter_sub not in task["name"]:
            continue
        tasks.append(task)
    return tasks


def _heldout_names() -> set[str]:
    if not HELDOUT_PATH.exists():
        return set()
    names = set()
    for line in HELDOUT_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(line)
    return names


def _git_short_commit() -> str:
    """Short HEAD sha for result auditability (issue #138).

    Baselines must record *which* code was tested. Returns ``"unknown"``
    when the bench runs outside a git checkout (e.g. a source tarball).
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _build_env(model: str | None, provider: str | None) -> dict[str, str]:
    """Env for the one-shot subprocess (issue #138).

    The CLI resolves model/provider as **env → config.toml → default**
    (`phoson_cli/config.py::_resolve_str`), so the real knobs are
    ``PHOSON_MODEL`` / ``PHOSON_PROVIDER`` — there is no ``*_OVERRIDE``.
    We set them *and* pop any inherited value first, so a developer
    shell that happens to export ``PHOSON_MODEL`` cannot silently change
    the model a baseline was measured with: when ``--model``/``--provider``
    are given, the run uses exactly those; when they are absent, the run
    falls back to the user's config.toml deterministically (inherited
    env never leaks in).
    """
    env = os.environ.copy()
    for key in ("PHOSON_MODEL", "PHOSON_PROVIDER"):
        env.pop(key, None)
    if model:
        env["PHOSON_MODEL"] = model
    if provider:
        env["PHOSON_PROVIDER"] = provider
    return env


def run_task(task: dict, model: str | None, provider: str | None) -> TaskResult:
    workspace = Path(tempfile.mkdtemp(prefix="phoson-bench-"))
    name = task["name"]
    module = task["module"]
    try:
        setup = module.get("setup")
        if setup is not None:
            setup(workspace)

        env = _build_env(model, provider)

        start = time.perf_counter()
        proc = subprocess.run(
            ["uv", "run", "phoson-cli", module["INSTRUCTION"]],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        duration = time.perf_counter() - start

        check = module.get("check")
        if check is None:
            passed, detail = proc.returncode == 0, "no checker defined"
        else:
            try:
                passed, detail = check(workspace, proc.stdout, proc.returncode)
            except Exception as exc:  # noqa: BLE001
                passed, detail = False, f"checker raised: {exc}"

        return TaskResult(
            name=name,
            passed=bool(passed),
            duration_s=duration,
            exit_code=proc.returncode,
            detail=detail,
            stdout_bytes=len(proc.stdout.encode()),
        )
    except subprocess.TimeoutExpired:
        return TaskResult(
            name=name,
            passed=False,
            duration_s=600.0,
            exit_code=-1,
            detail="timeout (600s)",
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filter", help="run only tasks whose name contains it")
    parser.add_argument("--model", help="override model for this run")
    parser.add_argument("--provider", help="override provider for this run")
    parser.add_argument(
        "--repeat", type=int, default=1, help="runs per task (stability)"
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="evaluate the run against bench/baseline.json and exit non-zero "
        "on regression (the nightly no-regression gate)",
    )
    parser.add_argument(
        "--no-heldout",
        action="store_true",
        help="skip the tasks named in heldout.txt (train split only)",
    )
    parser.add_argument(
        "--baseline",
        default=str(BASELINE_PATH),
        help="path to the baseline JSON (default: bench/baseline.json)",
    )
    parser.add_argument(
        "--min-margin",
        type=float,
        default=0.0,
        help="extra strictness added to the gate's noise floor",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="deliberately (re)seed the baseline from this run, "
        "ignoring any existing baseline value",
    )
    args = parser.parse_args(argv)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(args.filter, include_heldout=not args.no_heldout)
    if not tasks:
        print("No tasks found.")
        return 1

    results: list[TaskResult] = []
    for i in range(args.repeat):
        for task in tasks:
            label = task["name"] if args.repeat == 1 else f"{task['name']}#{i + 1}"
            print(f"▶ running {label} ...", flush=True)
            r = run_task(task, args.model, args.provider)
            r.name = label
            results.append(r)
            mark = "✅" if r.passed else "❌"
            print(f"  {mark} {r.duration_s:.1f}s  {r.detail}")

    passed_count = sum(r.passed for r in results)
    total_duration = sum(r.duration_s for r in results)
    print(f"\n{'=' * 60}")
    print(
        f"Results: {passed_count}/{len(results)} passed "
        f"({100 * passed_count / len(results):.0f}%)  "
        f"total wall time: {total_duration:.1f}s"
    )
    print(f"{'=' * 60}\n")

    out_file = RESULTS_DIR / (f"bench-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out_file.write_text(
        json.dumps(_results_payload(args.model, args.provider, results), indent=2)
    )
    print(f"Saved: {out_file}")

    if not args.gate:
        return 0

    # ── no-regression gate (issue #139 / H-1) ──────────────────────────
    return run_gate(
        results,
        baseline_path=Path(args.baseline),
        model=args.model,
        provider=args.provider,
        min_margin=args.min_margin,
        bootstrap=args.bootstrap,
        heldout=_heldout_names(),
    )


def _print_heldout_line(results: list[TaskResult], heldout: set[str] | None) -> None:
    """Report the held-out subset's pass rate on its own line.

    The held-out split is the subset a harness PR must never iterate
    against; reporting it separately (issue #139: "subset held-out
    reportado aparte") lets a maintainer watch it for overfitting
    without it altering the full-set verdict.
    """
    if not heldout:
        return
    held: list[bool] = []
    for r in results:
        base = r.name.rsplit("#", 1)[0]
        if base in heldout:
            held.append(bool(r.passed))
    if not held:
        return
    rate = sum(held) / len(held)
    print(
        f"   🛡️  held-out ({len(heldout)} tasks): {rate:.3f}  "
        f"[report-only, excluded from the iteration set]"
    )


def run_gate(
    results: list[TaskResult],
    baseline_path: Path,
    model: str | None = None,
    provider: str | None = None,
    min_margin: float = 0.0,
    bootstrap: bool = False,
    heldout: set[str] | None = None,
) -> int:
    """Evaluate ``results`` against the committed baseline (issue #139).

    Pure orchestration over :mod:`baseline` so the runner↔gate seam is
    unit-testable without a model:

    * **Bootstrap** — no baseline (a ``pass_rate: null`` sentinel), or
      ``bootstrap=True`` → seed/overwrite the baseline from this run and
      return 0. The nightly's first real run self-seeds; a maintainer can
      re-seed deliberately with ``--bootstrap``.
    * **Gate** — return 0 when the current pass rate is strictly above
      ``baseline − noise``, else 1 (a regression).

    When ``heldout`` is given, the pass rate of that subset is reported
    as its own line (the issue's "held-out reportado aparte") so it can
    be watched for overfitting without affecting the verdict. Prints the
    verdict and the per-task movement (the falsifiable contract a
    harness PR declares).
    """
    run_rates = B.run_pass_rates(results)
    per_task = B.run_per_task_rates(results)
    doc = B.load_baseline(baseline_path)
    baseline = B.baseline_rate(doc)

    if bootstrap or baseline is None:
        new = B.bootstrap_baseline(
            model=model or _effective_model(),
            provider=provider or _effective_provider(),
            run_rates=run_rates,
            per_task=per_task,
            commit=_git_short_commit(),
        )
        B.save_baseline(baseline_path, new)
        print(
            f"🌱 No baseline found → bootstrapped {baseline_path.name} "
            f"(pass_rate {new['pass_rate']:.3f}, {len(per_task)} tasks). "
            "Next gated run will compare against it."
        )
        return 0

    verdict = B.evaluate(
        run_rates, baseline, B.baseline_noise(doc), min_margin=min_margin
    )
    print(
        f"\n📊 Gate: {verdict.detail} "
        f"[baseline {baseline:.3f} @ {doc.get('commit', '?')} / "
        f"{doc.get('model', '?')} {doc.get('provider', '?')}]"
    )
    _print_heldout_line(results, heldout)
    deltas = B.task_deltas(per_task, B.baseline_per_task(doc))
    moved = {k: v for k, v in deltas.items() if abs(v) > 1e-9}
    if moved:
        for k, v in sorted(moved.items()):
            print(f"   {'+' if v > 0 else ''}{v:.3f}  {k}")
    if not verdict.ok:
        print(
            "❌ REGRESSION: pass rate dropped below baseline − noise. "
            "Do not land a harness change that regresses the agent."
        )
        return 1
    print("✅ Gate passed.")
    return 0


def _effective_model() -> str:
    """Best-effort effective model when the runner didn't pin one."""
    return os.environ.get("PHOSON_MODEL") or "config-default"


def _effective_provider() -> str:
    return os.environ.get("PHOSON_PROVIDER") or "config-default"


def _results_payload(
    model: str | None, provider: str | None, results: list[TaskResult]
) -> dict:
    """JSON shape for `bench/results/*.json` (issue #138).

    Every baseline run records the **effective** model + provider + the
    git commit it ran under, so a saved result can be audited and two
    runs compared. ``pass_rate`` is over all results (repeat × tasks).
    """
    passed_count = sum(r.passed for r in results)
    return {
        "model": model,
        "provider": provider,
        "commit": _git_short_commit(),
        "pass_rate": passed_count / len(results),
        "results": [dataclasses.asdict(r) for r in results],
    }


if __name__ == "__main__":
    raise SystemExit(main())
