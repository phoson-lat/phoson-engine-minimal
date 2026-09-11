#!/usr/bin/env python3
"""Generate bench plots for the README (issue #139).

Reads a saved results JSON (``bench/results/bench-*.json``) and renders
two PNGs into ``bench/assets/``:

* ``per-task-time.png``    — mean task duration across runs (sorted),
  with min–max whiskers; bar color encodes the pass rate of the task.
* ``per-task-stability.png`` — every individual run per task, to show
  run-to-run stability (noise) on the same task order.

Usage:
    uv run python bench/make_plots.py bench/results/bench-<timestamp>.json
    uv run python bench/make_plots.py            # newest results file
"""

import sys
import json
from pathlib import Path
from collections import defaultdict

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg", force=True)  # headless: save PNGs only, never open a window
RESULTS_DIR = Path(__file__).parent / "results"
ASSETS_DIR = Path(__file__).parent / "assets"

PASS_COLOR = "#2f9e6e"
PARTIAL_COLOR = "#e0a832"
FAIL_COLOR = "#d0543c"
RUN_COLORS = ["#4f7cff", "#9b6cff", "#e0639e", "#e0a832", "#2f9e6e"]


def load_results(path: Path) -> dict:
    return json.loads(path.read_text())


def per_task_stats(results: list[dict]) -> dict[str, dict[str, list]]:
    """task name -> {'durations': [...], 'passed': [...], 'runs': [i, ...]}"""
    stats: dict[str, dict[str, list]] = defaultdict(
        lambda: {"durations": [], "passed": [], "runs": []}
    )
    for r in results:
        name = r["name"].rsplit("#", 1)[0]
        run = int(r["name"].rsplit("#", 1)[1]) if "#" in r["name"] else 1
        stats[name]["durations"].append(r["duration_s"])
        stats[name]["passed"].append(bool(r["passed"]))
        stats[name]["runs"].append(run)
    return stats


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def plot_per_task_time(stats: dict[str, dict[str, list]], out: Path) -> None:
    order = sorted(stats, key=lambda k: -_mean(stats[k]["durations"]))
    names = [k for k in order]
    means = [_mean(stats[k]["durations"]) for k in order]
    mins = [min(stats[k]["durations"]) for k in order]
    maxs = [max(stats[k]["durations"]) for k in order]
    # xerr wants deviations FROM the bar value (the mean), not absolute ends.
    lo = [m - mn for m, mn in zip(means, mins)]  # how far min dips below the mean
    hi = [mx - m for m, mx in zip(means, maxs)]  # how far max rises above it
    rates = [sum(stats[k]["passed"]) / len(stats[k]["passed"]) for k in order]
    colors = [
        PASS_COLOR if rt == 1 else (FAIL_COLOR if rt == 0 else PARTIAL_COLOR)
        for rt in rates
    ]

    fig, ax = plt.subplots(figsize=(8.5, 6.2), dpi=150)
    y = range(len(order))
    ax.barh(
        list(y),
        means,
        xerr=[lo, hi],
        color=colors,
        alpha=0.9,
        height=0.62,
        error_kw={"lw": 1.4, "capsize": 3, "color": "#333"},
    )
    for yi, mx, rt in zip(y, maxs, rates):
        ax.text(
            mx + 0.3,
            yi,
            f"{mx:.1f}s  {int(rt * 100)}%",
            va="center",
            fontsize=8.5,
            color="#222",
        )
    ax.set_yticks(list(y))
    ax.set_yticklabels(names, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("duration (s) across runs — bar: mean, whiskers: min–max")
    ax.set_xlim(0, max(maxs) * 1.28)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", alpha=0.25)
    ax.set_title("Bench task durations (mean of runs, min–max whiskers)", pad=12)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def plot_per_task_stability(stats: dict[str, dict[str, list]], out: Path) -> None:
    order = sorted(stats, key=lambda k: -_mean(stats[k]["durations"]))
    n_runs = max(len(v["durations"]) for v in stats.values())

    fig, ax = plt.subplots(figsize=(8.5, 6.2), dpi=150)
    for i in range(n_runs):
        xs = []
        ys = []
        for j, k in enumerate(order):
            if i < len(stats[k]["durations"]):
                xs.append(j)
                ys.append(stats[k]["durations"][i])
        ax.scatter(
            xs,
            ys,
            s=26,
            color=RUN_COLORS[i % len(RUN_COLORS)],
            label=f"run {i + 1}",
            zorder=3,
        )
        ax.plot(
            xs, ys, color=RUN_COLORS[i % len(RUN_COLORS)], alpha=0.35, lw=1, zorder=2
        )

    means = [_mean(stats[k]["durations"]) for k in order]
    ax.scatter(
        range(len(order)),
        means,
        marker="_",
        s=900,
        linewidths=1.6,
        color="#222",
        zorder=4,
        label="mean",
    )
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([k for k in order], rotation=48, ha="right", fontsize=8)
    ax.set_ylabel("duration (s)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8.5, framealpha=0.9, loc="upper right")
    ax.set_title("Per-task duration across repeated runs (stability)", pad=12)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        path = Path(args[0])
    else:
        files = sorted(RESULTS_DIR.glob("bench-*.json"))
        if not files:
            print(f"no results files in {RESULTS_DIR}")
            return 1
        path = files[-1]
    doc = load_results(path)
    stats = per_task_stats(doc["results"])

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    p1 = ASSETS_DIR / "per-task-time.png"
    p2 = ASSETS_DIR / "per-task-stability.png"
    plot_per_task_time(stats, p1)
    plot_per_task_stability(stats, p2)
    print(
        "model={} provider={} commit={}".format(
            doc.get("model"), doc.get("provider"), doc.get("commit")
        )
    )
    print(f"saved: {p1}")
    print(f"saved: {p2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
