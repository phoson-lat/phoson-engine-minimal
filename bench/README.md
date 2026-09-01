# Phoson CLI Benchmark

Lightweight agent-harness benchmark for `phoson-cli` one-shot mode,
inspired by Terminal-Bench: each task runs the CLI inside an isolated
temporary workspace, then a deterministic checker verifies the outcome.

## Usage

```bash
uv run python bench/run_bench.py                 # run all tasks
uv run python bench/run_bench.py --filter git    # tasks matching substring
uv run python bench/run_bench.py --model openai/gpt-4o-mini
uv run python bench/run_bench.py --repeat 3      # stability runs
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
```

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
