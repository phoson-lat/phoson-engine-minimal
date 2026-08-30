# phoson_agent

Agent orchestration module providing the core agent engine, tool management, and session handling.

## Overview

`phoson_agent` provides:

- **AgentEngine** — Main ReAct loop with streaming events
- **Tool decorator** — Register functions as agent tools
- **Middleware** — Pre/post processing hooks for LLM calls and tool execution
- **Session management** — Branchable conversation trees with JSONL persistence

## Core Classes

### AgentEngine

Main engine for running LLM-based agents with support for tools and middleware.

```python
from phoson_agent import AgentEngine

agent = AgentEngine(
    chat=chat_adapter,
    tools=[get_weather, search_web],
    middlewares=[my_middleware],
    max_iterations=12,
    phoson_weight=1.0,
)
```

**Constructor Args:**

| Arg              | Type                  | Default        | Description                            |
|------------------|-----------------------|----------------|----------------------------------------|
| `chat`           | `BaseLLMChat`         | required       | LLM chat adapter                       |
| `tools`          | `list[AgentTool]`     | required       | Available tools                        |
| `middlewares`    | `list[AgentMiddleware]` | `[]`        | Middleware chain                       |
| `context`        | `AgentContext`        | `AgentContext()` | Agent context for tool execution     |
| `phoson_weight`  | `float`               | `1.0`          | Cost multiplier for credits            |
| `max_iterations` | `int`                 | `12`           | Max LLM->tool cycles                   |

**Methods:**

- `stream(messages, config)` — Execute agent, yield events as AsyncIterator
- `run(messages, config)` — Execute agent, return `AgentRunResult`
- `run_sync(messages, config)` — Synchronous version of `run()`
- `get_partial_history()` — Get current message history
- `is_running()` — Check if agent is currently executing

**Events Yielded:**

1. `AgentStartEvent` — Agent started
2. `AgentTokenEvent` — Text token generated
3. `AgentReasoningEvent` — Reasoning token (Anthropic/OpenAI o1)
4. `AgentToolComposingEvent` — The model is composing a tool call (throttled ~4/s)
5. `AgentToolStartEvent` — Tool call started
6. `AgentToolDoneEvent` — Tool call completed
7. `AgentStepDoneEvent` — Step completed (LLM or tool)
8. `AgentDoneEvent` — Agent finished (contains result)
9. `AgentErrorEvent` — Error occurred

### AgentContext

Context object passed to tool handlers, containing conversation state.

```python
from phoson_agent import AgentContext

ctx = AgentContext(extra={"session_id": "abc123"})
value = ctx.get("session_id")  # "abc123"
```

The `AgentContext` class holds arbitrary key-value pairs in its `extra` attribute and provides dictionary-like access via `get()`, `__getitem__`, and `__contains__`.

## Tool Decorator

Register a function as an agent tool using the `@tool` decorator.

```python
from phoson_agent import tool

@tool
def get_weather(location: str) -> str:
    """Get weather for a location."""
    return f"Weather in {location}: Sunny"
```

The decorator automatically:
- Extracts type hints → JSON Schema parameters
- Uses docstring as tool description
- Supports async functions
- Injects context via keyword-only args

**Advanced: Inject context**

```python
@tool(inject=["session_id"])
async def read_file(path: str, *, session_id: str | None = None) -> str:
    """Read a file from the session's working directory."""
    ...
```

### AgentTool Model

```python
@dataclass
class AgentTool:
    name: str                              # Tool name
    description: str                       # From docstring
    parameters: dict[str, Any]            # JSON Schema
    handler: ToolHandler                   # Callable
```

### Manual tools

When constructing an `AgentTool` directly (without `@tool`), the handler must accept **two positional arguments**: the tool's JSON args (`dict`) and the shared `AgentContext`. This is exactly how `AgentEngine` invokes every handler:

```python
from phoson_agent import AgentTool
from phoson_agent.context import AgentContext

def handle_echo(args: dict[str, Any], context: AgentContext) -> str:
    return args["message"]

echo = AgentTool(
    name="echo",
    description="Echo a message.",
    parameters={"type": "object", "properties": {"message": {"type": "string"}}},
    handler=handle_echo,
)
```

> ⚠️ A single-argument handler (only `args`) is **not** a supported contract: the engine always calls `handler(args, context)`, so a single-argument handler raises `TypeError` at execution time.

## Middleware

### AgentMiddleware

Abstract base class for agent middleware.

```python
from phoson_agent import AgentMiddleware

class MyMiddleware(AgentMiddleware):
    async def on_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        # Modify messages before LLM call
        return messages

    async def on_before_tool(
        self,
        call: ToolCallEvent,
    ) -> ToolCallEvent | None:
        # Modify or block tool call
        return call

    async def on_after_tool(
        self,
        call: ToolCallEvent,
        result: str,
        error: bool,
    ) -> str:
        # Modify tool result
        return result
```

### RetryMiddleware

Built-in middleware for automatic retry on errors.

```python
from phoson_agent import RetryMiddleware

middleware = RetryMiddleware(
    max_retries=3,
    base_delay_seconds=1.0,
    backoff_multiplier=2.0,
)
```

## Session Management

### ConversationTree

Branchable conversation history (not linear).

```python
from phoson_agent import ConversationTree

tree = ConversationTree.new(session_id="abc123")

# Add messages
node = tree.append(parent_id=None, message=Message(role="user", content="Hi"))
child = tree.append(parent_id=node.id, message=Message(role="assistant", content="Hello!"))

# Get path to a node
path = tree.get_path(child.id)  # [user_msg, assistant_msg]

# Branch
branch_node_id = tree.branch(from_node_id=node.id)
new_node = tree.append(parent_id=branch_node_id, message=Message(role="user", content="Branch!"))

# Get leaves (no children)
leaves = tree.get_leaves()

# Get branches from a node
branches = tree.get_branches(node.id)
```

**Methods:**

- `new(session_id)` — Create new tree
- `append(parent_id, message, metadata)` — Add message node
- `append_many(parent_id, messages)` — Add multiple messages
- `branch(from_node_id)` — Start new branch
- `get_path(node_id)` — Get message path to node
- `get_leaves()` — Get all leaf node IDs
- `get_branches(node_id)` — Get child node IDs
- `get_meta()` — Get `SessionMeta`
- `label(node_id, text)` — Add label to node
- `node_count()` — Number of nodes

### SessionMeta

Metadata for a session.

```python
@dataclass
class SessionMeta:
    id: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    message_count: int
    total_cost: float = 0.0
    total_tokens: int = 0
    step_count: int = 0
    last_model: str | None = None
```

### Storage

#### JsonlStorage

JSONL-based session persistence.

```python
from phoson_agent import JsonlStorage
from pathlib import Path

storage = JsonlStorage(base_path=Path("/path/to/sessions"))
await storage.save(tree)
loaded = await storage.load("session_id")
sessions = await storage.list_sessions()
await storage.delete("session_id")
```

#### SessionStorage

Abstract base for custom storage backends.

```python
class MyStorage(SessionStorage):
    async def save(self, tree: ConversationTree) -> None: ...
    async def load(self, session_id: str) -> ConversationTree: ...
    async def list_sessions(self) -> list[SessionMeta]: ...
    async def delete(self, session_id: str) -> None: ...
```

## Event Types

All events inherit from `AgentEvent` which has `timestamp`.

| Event                  | Fields                                           |
|------------------------|--------------------------------------------------|
| `AgentStartEvent`      | `model`, `message_count`, `max_iterations`      |
| `AgentTokenEvent`      | `content`                                       |
| `AgentReasoningEvent`  | `content`                                        |
| `AgentToolComposingEvent` | `index`, `tool_call_id` (always empty), `tool_name`, `args_chunk` (raw, partial JSON — do not parse) |
| `AgentToolStartEvent`  | `index`, `tool_call_id`, `tool_name`, `args`, `label` |
| `AgentToolDoneEvent`   | `index`, `tool_call_id`, `tool_name`, `result`, `error`, `duration_ms`, `label` |
| `AgentStepDoneEvent`   | `step: RunStep`                                 |
| `AgentDoneEvent`       | `result: AgentRunResult`                         |
| `AgentErrorEvent`      | `message`, `code`, `retryable`                 |
| `AgentSubagentResult`  | `index`, `task`, `result`, `cost_usd`, `credits`, `duration_ms`, `input_tokens`, `output_tokens`, `error` *(experimental — not yet emitted; subagent results currently travel as tool results)* |

> **`AgentToolComposingEvent` notes:** emitted while the provider streams
> tool-call deltas (OpenAI-compatible, Anthropic, Ollama), so consumers can
> show a "⚙ writing file…" style indicator before the call executes. It is
> **throttled leading-edge** (~250 ms between emissions, capped ~4/s): the
> first non-empty args chunk and the first known `tool_name` always emit;
> the rest are heartbeats. `tool_call_id` is always empty — the id only
> exists once `AgentToolStartEvent` arrives; correlate by `index`.
> `args_chunk` is a raw partial-JSON fragment: never parse it. Providers
> that do not stream tool-call deltas (e.g. Gemini) simply never emit it.

## RunStep Model

Tracks individual steps in agent execution.

```python
@dataclass
class RunStep:
    kind: Literal["llm", "tool"]
    started_at: datetime.datetime
    ended_at: datetime.datetime
    duration_ms: int
    model: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    usage: TokenUsage | None = None
    cost_usd: float = 0.0
    credits: float = 0.0
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
```

## AgentRunResult

Final result returned when agent completes.

```python
@dataclass
class AgentRunResult:
    final_content: str
    history: list[Message]
    input_messages: list[Message]
    steps: list[RunStep]
    total_cost_usd: float = 0.0
    total_credits: float = 0.0
```

## Public API

```python
from phoson_agent import (
    # Core
    AgentEngine, AgentContext, tool, AgentTool,
    # Events
    AgentEvent, AgentStartEvent, AgentTokenEvent, AgentReasoningEvent,
    AgentToolComposingEvent, AgentToolStartEvent, AgentToolDoneEvent,
    AgentStepDoneEvent,
    AgentDoneEvent, AgentErrorEvent, AgentSubagentResult,
    # Middleware
    AgentMiddleware, RetryMiddleware,
    # Session
    ConversationTree, ConversationNode, SessionMeta,
    SessionStorage, JsonlStorage,
    # Results
    RunStep, AgentRunResult,
)
```