"""Harbor installed agent for phoson-cli (Terminal-Bench interop, #139).

phoson-cli ships a headless one-shot mode (`phoson-cli "task"`), which is
exactly what Harbor's *installed agents* expect: install the agent in the
container, run it against the task instruction, done. This module is a
thin adapter so phoson-cli can be evaluated on Harbor datasets, most
importantly `terminal-bench/terminal-bench` (see https://tbench.ai).

Usage (from this repo):

    uv tool install harbor
    harbor run -d "terminal-bench/terminal-bench@<version>" \\
        --agent bench/harbor/phoson_agent.py:PhosonAgent \\
        --model <provider/model> \\
        --env docker

Model/provider/credentials are resolved by phoson-cli's normal chain
(env -> config.toml -> defaults) INSIDE the container, so forward the
API key as an env var on the harbor run (e.g. OPENROUTER_API_KEY=...
harbor run ...) or pre-seed a config.toml in the container image.

Note: pin the dataset version in `-d` — Terminal-Bench is a continuous
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
#: so no extras are needed for terminal tasks.
_INSTALL_CMD = "pip install --quiet --disable-pip-version-check phoson-engine-minimal"


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
        await self.exec_as_agent(
            environment,
            command=f"phoson-cli {shlex.quote(instruction)}",
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        # phoson-cli prints the final content to stdout, which Harbor
        # captures from the exec stream; no separate trajectory file to
        # parse yet (sessions live in ~/.phoson/sessions if enabled).
        return None
