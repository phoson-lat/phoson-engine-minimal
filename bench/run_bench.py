#!/usr/bin/env python3
"""Benchmark harness for phoson-cli one-shot mode.

Runs each task in an isolated temporary workspace, invoking the CLI the
same way a script would (`phoson-cli "task"`), then applies a
deterministic checker. Prints a summary table and saves JSON results.
"""

import os
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


@dataclasses.dataclass
class TaskResult:
    name: str
    passed: bool
    duration_s: float
    exit_code: int
    detail: str = ""
    stdout_bytes: int = 0


def load_tasks(filter_sub: str | None) -> list:
    tasks = []
    for path in sorted(TASKS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        namespace: dict = {}
        exec(compile(path.read_text(), path.name, "exec"), namespace)  # noqa: S102
        task = {
            "name": namespace.get("NAME", path.stem),
            "module": namespace,
        }
        if filter_sub and filter_sub not in task["name"]:
            continue
        tasks.append(task)
    return tasks


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filter", help="run only tasks whose name contains it")
    parser.add_argument("--model", help="override model for this run")
    parser.add_argument("--provider", help="override provider for this run")
    parser.add_argument(
        "--repeat", type=int, default=1, help="runs per task (stability)"
    )
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(args.filter)
    if not tasks:
        print("No tasks found.")
        return

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
    main()
