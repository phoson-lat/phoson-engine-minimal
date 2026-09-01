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

- Tasks run with the provider/model configured in `~/.phoson/config.toml`
  unless `--provider/--model` override them (via env vars).
- The agent's `bash` tool inherits the benchmark process cwd, so all
  tasks execute inside the temp workspace.
