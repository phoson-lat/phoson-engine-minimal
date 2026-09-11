# phoson-cli on Harbor / Terminal-Bench

External yardstick for the agent (issue #139): run phoson-cli against the
official **Terminal-Bench** dataset via [Harbor](https://harborframework.com)
and get a number comparable to the public leaderboard (Claude Code, Codex,
...). The local `bench/` set stays the cheap nightly no-regression gate;
this is the occasional "where do we stand vs the frontier" measurement.

## Components

- `phoson_agent.py` — Harbor **installed agent** for phoson-cli: installs
  `phoson-engine-minimal` from PyPI inside the task container and runs the
  headless one-shot mode (`phoson-cli "<instruction>"`) — the same mode the
  local bench uses.

## Setup

```bash
uv tool install harbor          # v0.22.0 tested
docker pull <task image>        # harbor pulls per-task images on demand
```

Model + credentials are resolved by phoson-cli **inside the container**
(env → config.toml → defaults), so export the API key (or vLLM base URL)
on the harbor run.

## Run

From the repo root (custom agent = import path `module.path:ClassName`):

```bash
# 1) Sanity: oracles must pass in your sandbox first
harbor run -d "terminal-bench/terminal-bench@<version>" \
    --agent oracle --k 5 --env docker

# 2) phoson-cli on a small, hand-picked subset (the full 66+ task set is
#    $$$ and includes GPU tasks — pick ~10-15 CPU tasks for comparisons)
OPENROUTER_API_KEY=... harbor run \
    -d "terminal-bench/terminal-bench@<version>" \
    --agent bench.harbor.phoson_agent:PhosonAgent \
    --env docker
```

## Rules for comparable numbers

- **Pin the dataset version** in `-d` — Terminal-Bench is a *continuous*
  benchmark (tagged releases on the Harbor Hub); results are only
  comparable across the same tag.
- Record the **phoson release** (the agent reports its version in job
  metadata), the **model + provider**, and the **task subset** used.
- Run the same subset with a standard agent (e.g. `codex`) as a local
  control when in doubt — Harbor's parity workflow is built for exactly
  that.

## Notes

- Tasks that need GPU (vLLM/Jax/fp8 ones) need a GPU-enabled environment
  (Modal/Daytona); on a plain docker machine, exclude them and say so.
- phoson-cli one-shot has no ATIF trajectory export yet — Harbor still
  scores the task (verifier runs in the container); trajectory viewing
  via `harbor view` is limited until we emit the native trajectory.
