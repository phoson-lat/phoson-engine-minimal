#!/usr/bin/env python3
"""Generate sales-grade bench plots for the README / site (issue #139).

Reads a saved results JSON (``bench/results/bench-*.json``) and renders,
per figure, a sharp PNG (for the README) and an interactive HTML
(embed on the Phoson website):

* ``per-task-time.png`` / ``.html``
    Mean task duration across runs, gradient bars, pass-rate badges.
* ``per-task-stability.png`` / ``.html``
    Every individual run per task + the mean — run-to-run stability.

Usage:
    uv run python bench/make_plots.py bench/results/bench-<timestamp>.json
    uv run python bench/make_plots.py            # newest results file
"""

import sys
import json
from pathlib import Path
from collections import defaultdict

import plotly.graph_objects as go

RESULTS_DIR = Path(__file__).parent / "results"
ASSETS_DIR = Path(__file__).parent / "assets"

FONT = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
GRID = "#e5e7eb"
INK = "#111827"
MUTED = "#6b7280"
BAR_GRADIENT = ["#14b8a6", "#059669"]  # teal -> emerald
RUN_COLORS = ["#6366f1", "#a855f7", "#ec4899"]  # indigo / violet / pink


def load_results(path: Path) -> dict:
    return json.loads(path.read_text())


def per_task_stats(results: list[dict]) -> dict[str, dict[str, list]]:
    """task name -> {'durations': [...], 'passed': [...]}"""
    stats: dict[str, dict[str, list]] = defaultdict(
        lambda: {"durations": [], "passed": []}
    )
    for r in results:
        name = r["name"].rsplit("#", 1)[0]
        stats[name]["durations"].append(r["duration_s"])
        stats[name]["passed"].append(bool(r["passed"]))
    return stats


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _base_layout(title: str, subtitle: str, height: int = 640) -> go.Layout:
    return go.Layout(
        width=1040,
        height=height,
        margin=dict(l=24, r=24, t=96, b=56),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=INK, size=13),
        title=dict(
            text=title,
            x=0.012,
            xanchor="left",
            y=0.985,
            font=dict(family=FONT, size=22, color=INK),
            subtitle=dict(
                text=subtitle,
                font=dict(family=FONT, size=12.5, color=MUTED),
            ),
        ),
        showlegend=False,
        xaxis=dict(
            gridcolor=GRID,
            gridwidth=1,
            zeroline=False,
            linecolor="rgba(0,0,0,0)",
            ticks="outside",
            tickcolor="rgba(0,0,0,0)",
            tickfont=dict(size=11.5, color=MUTED),
            showgrid=True,
        ),
    )


def _save(fig: go.Figure, stem: str) -> None:
    png = ASSETS_DIR / f"{stem}.png"
    html = ASSETS_DIR / f"{stem}.html"
    fig.write_image(str(png), scale=2)
    fig.write_html(str(html), include_plotlyjs="cdn", full_html=True)
    print(f"saved: {png}")
    print(f"saved: {html}")


def plot_per_task_time(stats: dict[str, dict[str, list]], meta: dict) -> None:
    order = sorted(stats, key=lambda k: -_mean(stats[k]["durations"]))
    names = order[::-1]  # slowest on top after inversion
    means = [_mean(stats[k]["durations"]) for k in order][::-1]
    maxs = [max(stats[k]["durations"]) for k in order][::-1]
    rates = [sum(stats[k]["passed"]) / len(stats[k]["passed"]) for k in order][::-1]
    overall = sum(rates) / len(rates)

    fig = go.Figure(
        go.Bar(
            x=means,
            y=names,
            orientation="h",
            marker=dict(
                color=BAR_GRADIENT,
                line=dict(width=0),
                colorscale=None,
            ),
            width=0.62,
        )
    )
    for i, (mx, rt) in enumerate(zip(maxs, rates)):
        color = "#059669" if rt == 1 else ("#dc2626" if rt == 0 else "#d97706")
        badge = f"<span style='color:{color}'>{int(rt * 100)}%</span>"
        fig.add_annotation(
            x=mx + 0.25,
            y=i,
            text=f"<b>{mx:.1f}s</b>&ensp;{badge}",
            showarrow=False,
            font=dict(family=FONT, size=12),
        )

    layout = _base_layout(
        "Agent benchmark — 15 deterministic tasks",
        f"<b>{int(overall * 100)}% pass</b> · "
        f"{meta.get('model', '?')} on {meta.get('provider', '?')} · "
        f"3 runs · commit {meta.get('commit', '?')}",
    )
    layout.yaxis = dict(
        title=None,
        showgrid=False,
        zeroline=False,
        linecolor="rgba(0,0,0,0)",
        tickfont=dict(size=12, color=INK),
        automargin=True,
    )
    layout.xaxis.title = "mean duration (s) across runs"
    layout.xaxis.range = [0, max(maxs) * 1.32]
    fig.update_layout(layout)
    _save(fig, "per-task-time")


def plot_per_task_stability(stats: dict[str, dict[str, list]], meta: dict) -> None:
    order = sorted(stats, key=lambda k: -_mean(stats[k]["durations"]))
    n_runs = max(len(v["durations"]) for v in stats.values())

    fig = go.Figure()
    for i in range(n_runs):
        xs = []
        ys = []
        for j, k in enumerate(order):
            if i < len(stats[k]["durations"]):
                xs.append(j)
                ys.append(stats[k]["durations"][i])
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines+markers",
                name=f"run {i + 1}",
                line=dict(color=RUN_COLORS[i % len(RUN_COLORS)], width=1.2, dash="dot"),
                marker=dict(size=8, color=RUN_COLORS[i % len(RUN_COLORS)]),
                hovertemplate=f"run {i + 1}<extra></extra>",
            )
        )
    fig.add_trace(
        go.Scatter(
            x=list(range(len(order))),
            y=[_mean(stats[k]["durations"]) for k in order],
            mode="lines+markers",
            name="mean",
            line=dict(color=INK, width=3),
            marker=dict(size=9, symbol="line-ns-open", line=dict(width=3.5, color=INK)),
            hovertemplate="mean<extra></extra>",
        )
    )

    layout = _base_layout(
        "Run-to-run stability — per-task duration",
        f"{meta.get('model', '?')} on {meta.get('provider', '?')} · "
        f"{n_runs} repeated runs",
    )
    layout.xaxis = dict(
        gridcolor=GRID,
        gridwidth=1,
        zeroline=False,
        linecolor="rgba(0,0,0,0)",
        ticks="outside",
        tickcolor="rgba(0,0,0,0)",
        tickfont=dict(size=10.5, color=INK),
        showgrid=True,
        tickvals=list(range(len(order))),
        ticktext=order,
        tickangle=-42,
    )
    layout.yaxis = dict(
        title="duration (s)",
        showgrid=True,
        gridcolor=GRID,
        zeroline=False,
        linecolor="rgba(0,0,0,0)",
        tickfont=dict(size=11.5, color=MUTED),
    )
    layout.xaxis.title = None
    layout.showlegend = True
    layout.legend = dict(
        x=1.0,
        y=1.16,
        xanchor="right",
        bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=11.5),
        orientation="h",
    )
    fig.update_layout(layout)
    _save(fig, "per-task-stability")


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
    meta = {
        "model": doc.get("model"),
        "provider": doc.get("provider"),
        "commit": doc.get("commit"),
    }
    print(
        "model={} provider={} commit={}".format(
            meta["model"], meta["provider"], meta["commit"]
        )
    )
    plot_per_task_time(stats, meta)
    plot_per_task_stability(stats, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
