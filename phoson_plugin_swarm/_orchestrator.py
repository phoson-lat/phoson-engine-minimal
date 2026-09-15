"""Swarm runtime and orchestrators (star / mesh / pipeline).

:class:`SwarmRuntime` is the per-swarm state object the :mod:`_tools` handlers
mutate. It holds the member :class:`~_agent_role.AgentInstance` list, the shared
:class:`~_blackboard.SharedState`, the shared :class:`~_blackboard.TokenBudget`,
and the *runtime bindings* the host injects (``chat``, ``available_tools``, the
default model, the middleware gate, ...) so each member engine is built the way
the CLI builds its sub-agents — same context flags, same permission middleware
— minus the delegation tools.

Topologies
----------
* **star** (default) and **mesh** run every member *in parallel*; the only
  difference is how much of the blackboard each member is seeded with (mesh
  seeds every member with the whole board, star with the facts it owns).
* **pipeline** runs members *sequentially* in declaration order, feeding each
  member's report into the next member's prompt.

All member engines are run-to-completion :class:`phoson_agent.agent.AgentEngine`
instances; inter-agent communication happens at the *boundaries* (the broker
seeds prompts, collects replies), which is the bounded form that fits a
streaming engine.
"""

import copy
import asyncio
import logging
from typing import Any

from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import Message, ModelConfig
from phoson_agent.models import (
    AgentTool,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
)
from phoson_agent.context import AgentContext
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent.exceptions import PhosonAgentError, PhosonMaxIterationsError

from ._agent_role import (
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_ERROR,
    STATUS_BUDGET,
    STATUS_RUNNING,
    STATUS_DISSOLVED,
    RESERVED_TOOL_NAMES,
    AgentRole,
    SwarmError,
    AgentResult,
    AgentInstance,
)
from ._blackboard import SharedState, TokenBudget

logger = logging.getLogger(__name__)

_TOPOLOGIES = ("star", "mesh", "pipeline")

_SWARM_MEMBER_PREAMBLE = (
    "\n\n# Swarm member\n"
    "You are one specialized member of a coordinated agent swarm. Work only on "
    "your task using the tools available to you, then finish with a single "
    "concise, self-contained report of your findings or the work completed. "
    "You cannot delegate, spawn sub-agents, or create other swarms."
)


def _clone_chat(chat: BaseLLMChat) -> BaseLLMChat:
    """Shallow-copy the host chat so concurrent member runs do not share state."""
    return copy.copy(chat)


def _member_context(
    *,
    safe_mode: bool,
    bash_confirmation: Any,
    plugin_ui: Any,
) -> AgentContext:
    """A fresh :class:`AgentContext` carrying the host's runtime flags."""
    ctx = AgentContext()
    ctx.extra["safe_mode"] = safe_mode
    ctx.extra["bash_confirmation"] = bash_confirmation
    if plugin_ui is not None:
        ctx.extra["plugin_ui"] = plugin_ui
    return ctx


class SwarmRuntime:
    """Mutable per-swarm state shared by the ``swarm_*`` tool handlers."""

    def __init__(self) -> None:
        self.topology: str = "star"
        self.instances: list[AgentInstance] = []
        self.state = SharedState()
        self.budget = TokenBudget()

        # Runtime bindings, captured from the host context on each tool call.
        self._chat: BaseLLMChat | None = None
        self._available_tools: dict[str, AgentTool] | None = None
        self._default_model: str = ""
        self._middlewares: list[Any] | None = None
        self._max_iterations: int = 12
        self._safe_mode: bool = False
        self._bash_confirmation: Any = None
        self._plugin_ui: Any = None

        self._tasks: set[asyncio.Task] = set()
        self.dissolved = False
        self._per_agent_cap: int | None = None

    # ── Lifecycle / configuration ─────────────────────────────────────────

    def bind_runtime(
        self,
        *,
        chat: BaseLLMChat | None,
        available_tools: dict[str, AgentTool] | None,
        default_model: str,
        middlewares: list[Any] | None,
        max_iterations: int,
        safe_mode: bool,
        bash_confirmation: Any,
        plugin_ui: Any,
    ) -> None:
        """Capture the host's injection parameters (idempotent per run)."""
        if chat is not None:
            self._chat = chat
        if available_tools:
            self._available_tools = available_tools
        if default_model:
            self._default_model = default_model
        if middlewares:
            self._middlewares = middlewares
        if max_iterations > 0:
            self._max_iterations = int(max_iterations)
        self._safe_mode = bool(safe_mode)
        if bash_confirmation is not None:
            self._bash_confirmation = bash_confirmation
        if plugin_ui is not None:
            self._plugin_ui = plugin_ui

    @property
    def agent_names(self) -> list[str]:
        return [i.name for i in self.instances]

    def _instance(self, name: str | None) -> AgentInstance:
        if name is None:
            if len(self.instances) == 1:
                return self.instances[0]
            raise SwarmError(
                "swarm has multiple agents; pass 'target' to name one "
                f"({', '.join(self.agent_names)})"
            )
        for instance in self.instances:
            if instance.name == name:
                return instance
        raise SwarmError(
            f"unknown agent {name!r}; known agents: {', '.join(self.agent_names)}"
        )

    def create(
        self,
        roles: list[AgentRole],
        *,
        topology: str = "star",
        max_tokens_per_agent: int | None,
        max_tokens_total: int | None,
        max_agents: int | None = None,
    ) -> None:
        """(Re)build the swarm from ``roles`` under ``topology``.

        Raises:
            SwarmError: if ``roles`` exceeds ``max_agents`` (the fan-out cap),
                the topology is unknown, or a role name is duplicated.
        """
        if max_agents is not None and len(roles) > max_agents:
            raise SwarmError(
                f"swarm can have at most {max_agents} agents, "
                f"got {len(roles)} (raise swarm_max_agents to allow more)"
            )
        if topology not in _TOPOLOGIES:
            raise SwarmError(
                f"unknown topology {topology!r}; expected one of {_TOPOLOGIES}"
            )
        seen: set[str] = set()
        for role in roles:
            if role.name in seen:
                raise SwarmError(f"duplicate agent role name: {role.name!r}")
            seen.add(role.name)

        # A per-agent cap can be tightened per role; the plugin-wide cap
        # (max_tokens_per_agent) is the floor everyone must not exceed.
        self.instances = [AgentInstance(role=role) for role in roles]
        self.topology = topology
        self.dissolved = False
        self._per_agent_cap = max_tokens_per_agent
        self.budget = TokenBudget(
            max_per_agent=max_tokens_per_agent, max_total=max_tokens_total
        )
        self.state = SharedState()

    # ── Tool selection (DoD: allowlist is enforced) ───────────────────────

    def _resolve_tools(self, role: AgentRole) -> list[AgentTool]:
        """The tools a member may actually call, restricted by its allowlist.

        Reserved tools (delegation + swarm tools) are always stripped, so a
        member can never recurse or spawn a nested swarm. ``tools_allowlist``
        of ``None`` means "everything else".
        """
        available = self._available_tools or {}
        allowed = {
            name: tool
            for name, tool in available.items()
            if name not in RESERVED_TOOL_NAMES
        }
        if role.tools_allowlist is not None:
            requested = set(role.tools_allowlist) - RESERVED_TOOL_NAMES
            allowed = {
                name: tool for name, tool in allowed.items() if name in requested
            }
        return list(allowed.values())

    # ── Prompt building ───────────────────────────────────────────────────

    def _seed_prompt(
        self,
        instance: AgentInstance,
        task: str,
        *,
        previous_output: str | None = None,
        share_blackboard: bool,
    ) -> str:
        """Compose the user turn: task + inbox + (optional) blackboard facts."""
        parts: list[str] = []
        parts.append(f"## Your task\n{task}")

        pending = self.state.inbox(instance.name)
        if pending:
            lines = [f"[{m.sender} -> {m.recipient}] {m.content}" for m in pending]
            parts.append("## Messages waiting for you\n" + "\n".join(lines))

        if share_blackboard:
            facts = self.state.snapshot()["facts"]
            if facts:
                parts.append("## Shared blackboard\n" + "\n".join(facts))

        if previous_output:
            parts.append(
                f"## Upstream result (from the previous stage)\n{previous_output}"
            )

        return "\n\n".join(parts)

    # ── Running a single member (budget-aware streaming) ─────────────────

    async def _run_member(
        self,
        instance: AgentInstance,
        task: str,
        *,
        previous_output: str | None = None,
        share_blackboard: bool = False,
    ) -> AgentResult:
        """Run one member to completion (or until its budget is exhausted)."""
        instance.task = task
        instance.status = STATUS_RUNNING

        if self._chat is None:
            return self._finish(
                instance,
                STATUS_ERROR,
                error=(
                    "swarm has no LLM chat client; the swarm tools must run "
                    "inside a host that injects 'chat' into its tool context"
                ),
            )
        model = instance.role.model or self._default_model
        if not model:
            return self._finish(
                instance,
                STATUS_ERROR,
                error=f"no model configured for agent {instance.name!r}",
            )

        selected_tools = self._resolve_tools(instance.role)
        system_prompt = instance.role.system_prompt + _SWARM_MEMBER_PREAMBLE
        user_prompt = self._seed_prompt(
            instance,
            task,
            previous_output=previous_output,
            share_blackboard=share_blackboard,
        )
        engine = AgentEngine(
            chat=_clone_chat(self._chat),
            tools=selected_tools,
            middlewares=list(self._middlewares) if self._middlewares else [],
            context=_member_context(
                safe_mode=self._safe_mode,
                bash_confirmation=self._bash_confirmation,
                plugin_ui=self._plugin_ui,
            ),
            max_iterations=self._max_iterations,
        )
        messages = [Message(role="user", content=user_prompt)]
        config = ModelConfig(model=model, system=system_prompt)

        per_agent_cap = (
            instance.role.max_tokens
            if instance.role.max_tokens is not None
            else self._per_agent_cap
        )

        text_parts: list[str] = []
        tokens_in = 0
        tokens_out = 0
        cost = 0.0

        try:
            async for event in engine.stream(messages, config):
                if isinstance(event, AgentTokenEvent):
                    if event.content:
                        text_parts.append(event.content)
                elif isinstance(event, AgentStepDoneEvent):
                    step = event.step
                    if step.kind == "llm" and step.usage is not None:
                        used = int(step.usage.input or 0) + int(step.usage.output or 0)
                        # Enforce the per-agent cap (role or plugin floor) AND
                        # the shared swarm-wide cap; stop gracefully when hit.
                        if per_agent_cap is not None and (
                            (self.budget.used(instance.name)) + used > per_agent_cap
                        ):
                            self.budget.consume(instance.name, 0)
                            return self._finish(
                                instance,
                                STATUS_BUDGET,
                                content="".join(text_parts),
                                error=f"per-agent budget ({per_agent_cap}) hit",
                                input_tokens=tokens_in,
                                output_tokens=tokens_out,
                                cost_usd=cost,
                            )
                        if self.budget.exhausted(instance.name):
                            self.budget.consume(instance.name, 0)
                            return self._finish(
                                instance,
                                STATUS_BUDGET,
                                content="".join(text_parts),
                                error="swarm-wide token budget reached",
                                input_tokens=tokens_in,
                                output_tokens=tokens_out,
                                cost_usd=cost,
                            )
                        self.budget.consume(instance.name, used)
                        tokens_in += int(step.usage.input or 0)
                        tokens_out += int(step.usage.output or 0)
                        cost += float(step.cost_usd or 0.0)
                elif isinstance(event, AgentDoneEvent):
                    result = event.result
                    return self._finish(
                        instance,
                        STATUS_DONE,
                        content=result.final_content,
                        input_tokens=tokens_in,
                        output_tokens=tokens_out,
                        cost_usd=result.total_cost_usd or cost,
                    )
                elif isinstance(event, AgentErrorEvent):
                    return self._finish(
                        instance,
                        STATUS_ERROR,
                        error=event.message or "agent error",
                        input_tokens=tokens_in,
                        output_tokens=tokens_out,
                        cost_usd=cost,
                    )
        except PhosonMaxIterationsError as exc:
            return self._finish(
                instance,
                STATUS_ERROR,
                content="".join(text_parts),
                error=str(exc),
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=cost,
            )
        except PhosonAgentError as exc:
            return self._finish(
                instance,
                STATUS_ERROR,
                error=str(exc),
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=cost,
            )
        except asyncio.CancelledError:
            instance.status = STATUS_IDLE
            raise
        except Exception as exc:  # noqa: BLE001 - surface to the model, don't crash
            logger.debug("swarm member %s failed", instance.name, exc_info=True)
            return self._finish(
                instance,
                STATUS_ERROR,
                error=f"{type(exc).__name__}: {exc}",
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=cost,
            )

        # Stream ended without a terminal event.
        return self._finish(
            instance,
            STATUS_ERROR,
            content="".join(text_parts),
            error="agent stream ended without a result",
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=cost,
        )

    def _finish(
        self,
        instance: AgentInstance,
        status: str,
        *,
        content: str = "",
        error: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> AgentResult:
        result = AgentResult(
            agent=instance.name,
            status=status,
            content=content,
            task=instance.task or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            error=error,
        )
        instance.result = result
        instance.status = status
        return result

    # ── Assignment (topology-aware) ───────────────────────────────────────

    async def assign(self, task: str, target: str | None) -> dict[str, Any]:
        """Dispatch a task; returns a non-blocking summary of what started."""
        if self.dissolved:
            raise SwarmError("swarm is dissolved; call swarm_create first")
        if not task or not task.strip():
            raise SwarmError("swarm_assign 'task' must be a non-empty string")

        if target is not None:
            instance = self._instance(target)
            task_obj = asyncio.create_task(self._run_member(instance, task))
            self._tasks.add(task_obj)
            task_obj.add_done_callback(self._tasks.discard)
            return {
                "started": [instance.name],
                "topology": self.topology,
                "note": f"agent {instance.name!r} is working; call swarm_collect.",
            }

        # Whole swarm: dispatch by topology.
        if self.topology == "pipeline":
            task_obj = asyncio.create_task(self._run_pipeline(task))
            self._tasks.add(task_obj)
            task_obj.add_done_callback(self._tasks.discard)
            return {
                "started": self.agent_names,
                "topology": "pipeline",
                "note": "stages will run sequentially; call swarm_collect to wait.",
            }

        # star / mesh: run every member in parallel on the same task.
        share_blackboard = self.topology == "mesh"
        for instance in self.instances:
            task_obj = asyncio.create_task(
                self._run_member(instance, task, share_blackboard=share_blackboard)
            )
            self._tasks.add(task_obj)
            task_obj.add_done_callback(self._tasks.discard)
        return {
            "started": self.agent_names,
            "topology": self.topology,
            "note": "agents are working in parallel; call swarm_collect to wait.",
        }

    async def _run_pipeline(self, task: str) -> list[AgentResult]:
        """Run members sequentially, feeding each report into the next."""
        previous_output: str | None = None
        results: list[AgentResult] = []
        for index, instance in enumerate(self.instances):
            # The first stage gets the raw task; later stages still get the
            # task as framing plus the upstream result.
            result = await self._run_member(
                instance,
                task,
                previous_output=previous_output,
                share_blackboard=True,
            )
            results.append(result)
            if result.status in (STATUS_DONE, STATUS_BUDGET) and result.content:
                previous_output = result.content
            else:
                # A failed stage breaks the chain (fail fast, but record it).
                break
            # Publish the stage output on the blackboard so later stages (and
            # the collector) can see it.
            self.state.write(f"stage:{instance.name}", result.content)
            # The stage's own output becomes the upstream context; clear the
            # inbox so the same message is not re-fed within this run.
            self.state.drain_inbox(instance.name)
        return results

    # ── Collection / status / teardown ───────────────────────────────────

    async def collect(self, *, timeout: float | None = None) -> list[AgentResult]:
        """Wait for in-flight work and return every member's result so far."""
        if self._tasks:
            waitable = list(self._tasks)
            if timeout is not None and timeout > 0:
                await asyncio.wait(waitable, timeout=timeout)
            else:
                await asyncio.gather(*waitable, return_exceptions=True)
        # Return in declaration order; members that never ran report idle.
        results: list[AgentResult] = []
        for instance in self.instances:
            if instance.result is not None:
                results.append(instance.result)
        return results

    def status(self) -> dict[str, Any]:
        in_flight = [i.name for i in self.instances if i.status == STATUS_RUNNING]
        return {
            "topology": self.topology,
            "dissolved": self.dissolved,
            "agents": [i.to_dict() for i in self.instances],
            "in_flight": in_flight,
            "pending_messages": self.state.pending_count(),
            "tokens": {
                "total_used": self.budget.total_used(),
                "per_agent": {
                    name: self.budget.used(name) for name in self.agent_names
                },
            },
        }

    def dissolve(self) -> dict[str, Any]:
        """Cancel in-flight work and tear the swarm down."""
        cancelled = 0
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                cancelled += 1
        self._tasks.clear()
        for instance in self.instances:
            if instance.status in (STATUS_RUNNING, STATUS_IDLE):
                instance.status = STATUS_DISSOLVED
        self.dissolved = True
        return {
            "dissolved": True,
            "cancelled_tasks": cancelled,
            "tokens_total_used": self.budget.total_used(),
        }

    async def aclose(self) -> None:
        """Best-effort teardown (called by the plugin's ``aclose``)."""
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()


def build_runtime_from_roles(
    raw_roles: list[Any],
    *,
    topology: str,
    max_tokens_per_agent: int | None,
    max_tokens_total: int | None,
    max_agents: int | None = None,
) -> SwarmRuntime:
    """Validate ``raw_roles`` and build a fresh :class:`SwarmRuntime`."""
    if not isinstance(raw_roles, (list, tuple)) or not raw_roles:
        raise SwarmError("swarm_create 'agents' must be a non-empty list")
    roles = [AgentRole.from_dict(raw) for raw in raw_roles]
    runtime = SwarmRuntime()
    runtime.create(
        roles,
        topology=topology,
        max_tokens_per_agent=max_tokens_per_agent,
        max_tokens_total=max_tokens_total,
        max_agents=max_agents,
    )
    return runtime
