"""Harness quality tests (issue #139 / H-1).

These run in CI *without* a model and guard the eval set itself:

* every task module loads and exposes the contract (``INSTRUCTION``, and
  where used, ``setup``/``check`` callables);
* with a real ``setup`` applied and the agent having produced *nothing*,
  ``check`` must return a ``(False, detail)`` verdict, not raise — a
  checker that raised on a clean workspace would mean the task's check
  is coupled to setup state it doesn't restore;
* ``setup`` runs cleanly from an empty workspace.

The tasks' *solvability* (a perfect agent passes each checker) is
verified by ``bench/selftest.py``, run on the model-bearing nightly
runner, since it requires executing the agent.
"""

import shutil
import tempfile
import importlib.util
from pathlib import Path

_BENCH_DIR = Path(__file__).resolve().parent.parent / "bench"

# Load bench/run_bench.py by path (bench is not a package), same as
# tests/test_bench_runner.py, so the harness quality tests exercise the
# real runner without depending on sys.path.
_spec = importlib.util.spec_from_file_location(
    "run_bench_harness_qa", _BENCH_DIR / "run_bench.py"
)
RB = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RB)


def _load_tasks() -> list[dict]:
    return RB.load_tasks(None)


def _ws() -> Path:
    return Path(tempfile.mkdtemp(prefix="harness-"))


def test_all_tasks_load_with_contract() -> None:
    tasks = _load_tasks()
    # The eval set target (issue #139): 15–25 deterministic tasks.
    assert len(tasks) >= 15, f"eval set too small: {len(tasks)} tasks"
    for t in tasks:
        mod = t["module"]
        assert isinstance(mod.get("INSTRUCTION"), str) and mod["INSTRUCTION"].strip()
        if "check" in mod:
            assert callable(mod["check"])


def test_check_verdicts_false_on_clean_workspace() -> None:
    """setup applied, agent produced nothing → check must return
    ``(False, detail)``, never raise (the runner would otherwise score
    a checker crash as a task failure with no reason)."""
    for t in _load_tasks():
        mod = t["module"]
        ws = _ws()
        try:
            setup = mod.get("setup")
            if setup is not None:
                setup(ws)
            try:
                passed, detail = mod["check"](ws, "", 1)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"{t['name']}: check raised {type(exc).__name__}: {exc} "
                    "on a clean (set-up, untouched) workspace"
                ) from exc
            assert passed is False, f"{t['name']}: check passed on clean workspace"
            assert isinstance(detail, str) and detail, f"{t['name']}: empty detail"
        finally:
            shutil.rmtree(ws, ignore_errors=True)


def test_setup_runs_from_empty_workspace() -> None:
    for t in _load_tasks():
        mod = t["module"]
        setup = mod.get("setup")
        if setup is None:
            continue
        ws = _ws()
        try:
            setup(ws)  # must not raise
        finally:
            shutil.rmtree(ws, ignore_errors=True)


def test_every_task_is_solvable_via_reference_solution() -> None:
    """setup → SOLVE → check must pass for every task (model-free).

    This is the conformance oracle: it proves each checker actually
    accepts a correct solution, so a task bug (an over-strict or broken
    check) is caught in CI instead of silently counting against the
    model's score on the nightly run. The agent never sees ``SOLVE``.
    """
    for t in _load_tasks():
        mod = t["module"]
        solve = mod.get("SOLVE")
        assert callable(solve), f"{t['name']}: missing SOLVE reference solution"
        ws = _ws()
        try:
            if mod.get("setup") is not None:
                mod["setup"](ws)
            solve(ws)
            passed, detail = mod["check"](ws, "", 0)
            assert passed, f"{t['name']}: SOLVE did not satisfy check: {detail}"
        finally:
            shutil.rmtree(ws, ignore_errors=True)


def test_task_names_are_unique_and_stable() -> None:
    names = [t["name"] for t in _load_tasks()]
    assert len(names) == len(set(names)), f"duplicate task names: {names}"
    assert all(n == n.strip() for n in names)
