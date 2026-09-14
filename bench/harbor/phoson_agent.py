"""Harbor installed agent for phoson-cli (Terminal-Bench interop, #139).

phoson-cli ships a headless one-shot mode (`phoson-cli "task"`), which is
exactly what Harbor's *installed agents* expect: install the agent in the
container, run it against the task instruction, done. This module is a
thin adapter so phoson-cli can be evaluated on Harbor datasets, most
importantly `terminal-bench/terminal-bench` (see https://tbench.ai).

Usage (from this repo):

    uv tool install harbor
    harbor run -d "terminal-bench/terminal-bench@<version>" \\
        --agent bench.harbor.phoson_agent:PhosonAgent \\
        --env docker \\
        --ae PHOSON_PROVIDER=vllm \\
        --ae PHOSON_MODEL=<model> \\
        --ae VLLM_BASE_URL=<openai-compatible base url>

Model/provider/credentials are resolved by phoson-cli's normal chain
(env -> config.toml -> defaults) INSIDE the container. Harbor does **not**
forward the host environment, so they must be passed with
``--ae/--agent-env KEY=VALUE`` (host ``export``s never cross into the
container). Export ``PYTHONPATH`` to the repo root so this module is
importable when ``--agent`` is given as a module path.

Install uses ``uv tool install`` (an isolated venv, so it works on the
PEP 668 "externally managed" task images where a bare ``pip install``
fails), with a ``pip --break-system-packages`` fallback.

Note: pin the dataset version in ``-d`` — Terminal-Bench is a continuous
benchmark, and results are only comparable across the same dataset tag.
"""

import shlex

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.agents.installed.base import (
    BaseInstalledAgent,
    with_prompt_template,
)

#: phoson-engine-minimal is published to PyPI; core tools are stdlib-only,
#: so no extras are needed for terminal tasks. ``uv tool install`` puts an
#: isolated venv + a ``phoson-cli`` shim in ``~/.local/bin`` and sidesteps
#: PEP 668 (images that mark the system Python "externally managed" reject a
#: plain ``pip install`` — e.g. terminal-bench/data-anonymization). Fall back
#: to ``pip --break-system-packages`` when ``uv`` cannot be bootstrapped.
_INSTALL_CMD = (
    "set -uo pipefail; "
    "command -v uv >/dev/null 2>&1 || "
    "  curl -LsSf https://astral.sh/uv/install.sh | sh || true; "
    '[ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"; '
    'export PATH="$HOME/.local/bin:$PATH"; '
    "if command -v uv >/dev/null 2>&1; then "
    "  uv tool install phoson-engine-minimal; "
    "else "
    "  pip install --quiet --disable-pip-version-check "
    "    --break-system-packages phoson-engine-minimal; "
    "fi; "
    "command -v phoson-cli"
)

#: ``phoson-cli`` lives in ``~/.local/bin`` after a ``uv tool install``; make
#: sure every agent exec sees it on PATH (``docker exec`` starts a fresh
#: shell that does not source the uv env).
_PATH_PRELUDE = (
    '[ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"; '
    'export PATH="$HOME/.local/bin:$PATH"; '
)


class PhosonAgent(BaseInstalledAgent):
    """phoson-cli as a Harbor installed agent (one-shot headless mode)."""

    @staticmethod
    def name() -> str:
        return "phoson"

    def version(self) -> str | None:
        # Keep in sync with the release being tested; harbor records it in
        # job metadata so results are auditable.
        return "0.31.2"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("curl",))
        await self.exec_as_agent(environment, command=_INSTALL_CMD)

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # One-shot mode: no TTY, no session — the CLI resolves
        # PHOSON_MODEL/PHOSON_PROVIDER (env -> config.toml -> default) and
        # runs the task until it finishes or the budget is hit.
        #
        # No ``cwd`` is passed on purpose: Harbor defaults to the task's
        # workdir, or the image's WORKDIR when unset — which for
        # terminal-bench is ``/app``. Hardcoding ``/app`` would break tasks
        # whose image uses a different WORKDIR.
        #
        # ``tee`` the full transcript into the environment's agent logs dir
        # (/logs/agent, mounted back to the trial's ``agent/`` dir). The
        # one-shot mode prints only the final content to stdout, so without
        # this the run is invisible. ``_exec`` prepends ``set -o pipefail``,
        # so a non-zero phoson-cli exit still fails the command through the
        # pipe.
        transcript = self.environment_logs_dir / "phoson.txt"
        await self.exec_as_agent(
            environment,
            command=(
                f"{_PATH_PRELUDE}"
                f"phoson-cli {shlex.quote(instruction)} "
                f"2>&1 | tee {shlex.quote(str(transcript))}"
            ),
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        # ``run`` persists the full transcript to /logs/agent/phoson.txt via
        # ``tee`` (one-shot prints only the final content to stdout), so
        # there is no structured trajectory yet to parse into token/cost
        # metrics here.
        return None
