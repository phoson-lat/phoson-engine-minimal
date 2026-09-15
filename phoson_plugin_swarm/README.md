# phoson-plugin-swarm

Orchestrate a **swarm of specialized sub-agents** from a Phoson session (issue
#232): roles with their own tool allowlists and model overrides, a shared
blackboard with message routing, and **star / mesh / pipeline** topologies —
the AutoGen / CrewAI / Camel multi-agent pattern, integrated into Phoson.

This is *not* a duplicate of the `agent`/`agents` sub-agent tools. Sub-agents
are one-way delegation that returns a single string; the swarm adds **roles**,
**shared/communicating state**, and a **topology** that feeds one member's
output into the next (pipeline) or merges several (star/mesh). Under the hood
each member is its own `AgentEngine`, built exactly the way the CLI builds its
sub-agents (cloned chat, injected tools, the same permission middleware).

## Tools

| Tool | Purpose |
|---|---|
| `swarm_create(agents, topology="star")` | Create the swarm from a list of agent roles. |
| `swarm_assign(task, target?)` | Give the whole swarm (by topology) or one named agent a task. Non-blocking. |
| `swarm_message(sender, recipient, content, topic?)` | Route a message to an agent's inbox, or broadcast to `"*"`. |
| `swarm_status()` | Agents, in-flight tasks, partial results, pending messages, tokens used. |
| `swarm_collect(timeout?)` | Wait for in-flight work and return every agent's result. |
| `swarm_dissolve()` | Cancel in-flight work and tear the swarm down. |

Each entry in `agents` is an object:

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Unique role name (e.g. `"security-reviewer"`). |
| `system_prompt` | yes | Specialized instructions for that role. |
| `model` | no | Per-agent model override (e.g. a cheaper model for a simple role). |
| `tools_allowlist` | no | The *only* tools this agent may call. `null` = every tool the host offers. Delegation (`agent`/`agents`) and all `swarm_*` tools are **always** stripped, so a member can never delegate or spawn a nested swarm. |
| `max_tokens` | no | That agent's cumulative token budget (tightens the plugin-wide cap). |

`topology` is one of:

- **`star`** (default) — every member runs in parallel on the task; results fan back to the orchestrator.
- **`mesh`** — like `star`, but every member is also seeded with the full shared blackboard (full peer visibility).
- **`pipeline`** — members run **sequentially** in declaration order; each stage's report is fed into the next stage's prompt.

## Configuration

```toml
[defaults]
enable_swarm = true          # opt-in (also PHOSON_ENABLE_SWARM)
swarm_max_agents = 5         # fan-out cap, enforced in swarm_create
swarm_max_tokens_per_agent = 4096
swarm_max_tokens_total = 32768
swarm_default_topology = "star"
```

Or, as a direct plugin spec:

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        {
            "name": "phoson-plugin-swarm",
            "config": {
                "max_agents": 5,
                "default_topology": "star",
                "max_tokens_per_agent": 4096,
                "max_tokens_total": 32768,
            },
        }
    ],
)
```

| Config key | Default | Meaning |
|---|---|---|
| `max_agents` | `5` | Max agents per swarm; `swarm_create` raises above this. |
| `default_topology` | `"star"` | Default topology (still overridable per `swarm_create`). |
| `max_tokens_per_agent` | `4096` | Swarm-wide per-agent cumulative token cap. |
| `max_tokens_total` | `32768` | Swarm-wide cumulative token cap. |

## How it works

- **Roles & allowlists.** Each member's engine gets only the tools in its
  `tools_allowlist` (minus the reserved delegation/swarm tools), so a member
  can never call a tool outside its allowlist or recurse.
- **Shared state.** A per-swarm blackboard (`SharedState`) holds shared facts
  and a message broker (per-agent inboxes + `"*"` broadcasts). The orchestrator
  seeds each member's prompt with its inbox and (for `mesh`/`pipeline`) the
  board, and writes each result back — the bounded form of inter-agent
  communication in a run-to-completion engine.
- **Cost control.** A shared `TokenBudget` tracks per-agent and swarm-wide
  tokens; a member that would exceed a cap is stopped **gracefully**
  (`status="budget_exhausted"` with its partial output) instead of running away.

## Security

- Members **do not inherit** the parent's tool set — each is configured with its
  own `tools_allowlist`.
- The host's permission middleware and runtime flags (`safe_mode`,
  `bash_confirmation`, `plugin_ui`) are forwarded, so `ask`/`deny` gates apply
  inside a member exactly as they do for the parent.
- Token caps (per-agent and total) bound runaway cost; `swarm_max_agents`
  bounds fan-out.

## Development

```bash
uv run pytest tests/phoson_plugin_swarm -q
uv run python examples/swarm_example.py
```
