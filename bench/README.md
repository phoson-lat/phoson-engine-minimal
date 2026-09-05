# Phoson CLI Benchmark

Lightweight agent-harness benchmark for `phoson-cli` one-shot mode,
inspired by Terminal-Bench: each task runs the CLI inside an isolated
temporary workspace, then a deterministic checker verifies the outcome.

This is also the **agent no-regression gate** (issue #139 / H-1): the
nightly workflow runs the set ≥3 times against a fixed local model,
measures the run-to-run noise, and fails when the pass rate drops below
the committed baseline minus that noise.

## Usage

```bash
uv run python bench/run_bench.py                 # run all tasks
uv run python bench/run_bench.py --filter git    # tasks matching substring
uv run python bench/run_bench.py --model openai/gpt-4o-mini
uv run python bench/run_bench.py --repeat 3      # stability runs
uv run python bench/run_bench.py --gate          # evaluate vs baseline.json
uv run python bench/run_bench.py --gate --bootstrap   # (re)seed the baseline
uv run python bench/run_bench.py --no-heldout    # train split only
```

Results are printed as a table and written to
`bench/results/bench-<timestamp>.json`.

## Task format

Each task is a Python file in `bench/tasks/` exposing:

```python
NAME = "task-name"
INSTRUCTION = "what the agent must do"

def setup(workspace: Path) -> None:
    """Optional: seed the workspace before the run."""

def check(workspace: Path, stdout: str, exit_code: int) -> tuple[bool, str]:
    """Return (passed, detail). Must be deterministic."""

def SOLVE(workspace: Path) -> None:
    """Reference solution (conformance oracle). Never seen by the agent."""
```

`SOLVE` is the model-free conformance oracle: the CI harness-quality test
asserts that `setup → SOLVE → check` passes for every task, so an
over-strict or broken checker is caught in CI instead of silently counting
against the model's nightly score. The agent never sees it — it runs in a
fresh temp workspace that only receives `setup`'s output.

## The no-regression gate (issue #139)

`run_gate()` in `run_bench.py` is pure and model-free; the logic it wraps
lives in `baseline.py` and is fully unit-tested (`tests/test_bench_baseline_gate.py`):

- **Bootstrap** — the committed `baseline.json` is a `pass_rate: null`
  sentinel, so the first real gated run self-seeds it (measured pass rate,
  noise, model, provider, commit, date). The nightly workflow commits that
  baseline back to `main`. A maintainer can re-seed deliberately with
  `--gate --bootstrap`.
- **Gate** — pass iff the current pass rate is *strictly* above
  `baseline − noise` (ties are rejected). `noise` is the population std of
  the current run's per-run pass rates (self-calibrating: a stable harness
  → tight floor, a flaky one → its own wider floor). The baseline may carry
  its own recorded noise and the larger of the two is used.
- **Per-task deltas** — `task_deltas()` reports per-task pass-rate movement
  vs the baseline, which is the falsifiable contract a harness PR declares
  (which tasks it predicts it fixes / puts at risk).
- **Held-out split** — `heldout.txt` names the subset a harness PR must
  *never* iterate against (`--no-heldout` runs the train split only). The
  gate reports the held-out pass rate on its own line, separately, so it can
  be watched for overfitting without affecting the verdict.

## Metrics captured

Per run: passed, duration, CLI exit code, stdout size. Cost/token metrics
require a `--json` output flag in one-shot mode (see ROADMAP suggestion):
one-shot currently prints only the final content and discards
`RunResult.steps` usage data.

## Notes

- Tasks run with the provider/model configured in `~/.phoson/config.toml`,
  unless `--provider`/`--model` pin them. The pin is applied by setting the
  `PHOSON_MODEL` / `PHOSON_PROVIDER` env vars on the one-shot subprocess
  (the CLI resolves these env → config.toml → default). Any value inherited
  from your own shell is dropped first, so a dev `PHOSON_MODEL` can't quietly
  re-target a baseline run (issue #138).
- The agent's `bash` tool inherits the benchmark process cwd, so all
  tasks execute inside the temp workspace.
- Each `bench/results/*.json` records the effective `model`, `provider` and
  the git `commit` it ran under, so a saved baseline is auditable.

## Nightly workflow

`.github/workflows/nightly-agent-eval.yml` runs on a schedule (and on
manual dispatch) and:

1. installs Ollama + pulls a **fixed local model** (default
   `qwen2.5:1.5b`; override via the `BENCH_MODEL` / `BENCH_PROVIDER`
   repo vars or the dispatch inputs — pass the bare model tag, the
   workflow strips any leading `ollama/` prefix),
2. runs the bench `--repeat 3 --gate`,
3. publishes the results as an artifact,
4. commits the self-seeded baseline back to `main` (only on the first real
   run, when the sentinel is replaced with data), and
5. fails the run if the gate reports a regression.

The baseline is tied to a specific local model + commit; re-seed it
(`--gate --bootstrap`) whenever you change the model or the eval set.
