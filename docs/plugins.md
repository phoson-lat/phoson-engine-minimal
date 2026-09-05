# Plugin System

The Phoson Agent plugin system lets you extend the agent's capabilities in a modular, reusable way.

## Canonical interface decision

There is a single supported plugin contract: the `Plugin` class (synchronous ABC) defined in `phoson_agent/plugin.py`, with the lifecycle `configure()` → `initialize()` → use → `cleanup()`.

An earlier external roadmap described a second contract, `PhosonPlugin` (async, `on_load`/`on_unload`), but it was never implemented: it does not exist in the code, there are no loaders for it, and no real plugin (`phoson_plugin_mcp`, the examples in `examples/`) uses it. It is formally discarded in favor of `Plugin` because:

- It is the interface already implemented by `PluginRegistry`/`load_plugin` (`phoson_agent/plugin_loader.py`) and by every existing plugin.
- The tools and middlewares a plugin exposes (`get_tools`, `get_middlewares`) do not require the plugin's own load/unload to be async — the async part lives inside the tools (`ToolHandler` already supports async handlers), not in the plugin lifecycle.
- Introducing a second contract would only duplicate loaders and documentation without enabling anything `initialize()`/`cleanup()` do not already allow (a plugin can create an async pool inside a synchronous `initialize()`, e.g. with `asyncio.get_event_loop().run_until_complete(...)` or by deferring the connection coroutine to first use — see `phoson_plugin_checkpoint` and `phoson_plugin_memory` as examples).

All new plugins (`phoson_plugin_checkpoint`, `phoson_plugin_memory`) implement `Plugin`, not `PhosonPlugin`.

## Basic Concepts

A **plugin** can provide:
- **Tools**: functions the agent can call
- **Middlewares**: hooks in the agent lifecycle
- **Configuration**: customizable options
- **CLI extensions**: slash commands, tool-card verbs/icons and one derived theme
- **UI interactions**: neutral notices, data cards, TODO/progress blocks and confirmations through the host-provided `plugin_ui` service

The UI contract is intentionally declarative: plugins return/use types from
`phoson_agent.cli_extensions` and **must not** import Rich, prompt_toolkit or
`phoson_cli` internals. Fullscreen and classic hosts render the same blocks;
one-shot hosts return `InteractionResult(status="unavailable")` for questions
and never read stdin implicitly.

## Installing community plugins

Use the plugin manager for packages published to PyPI or a trusted Git source:

```bash
phoson-cli plugin install "phoson-plugin-example==1.2.0"
phoson-cli plugin install github:owner/repository@v1.2.0
phoson-cli plugin list
phoson-cli plugin disable example
phoson-cli plugin enable example
phoson-cli plugin doctor example
```

`phoson-cli --install-plugin <source>` is an alias for `plugin install`. Add
`--yes` to intentionally skip its confirmation in automation. GitHub shorthand
is normalized to a standard `git+https://` requirement and Git sources are
resolved to a commit SHA recorded in `~/.phoson/plugins.lock.toml`. Installation
executes Python code with your user permissions, so inspect and pin sources to
a release tag or commit. A plugin package must export an entry point in the
existing `phoson.plugins` group. See `examples/PLUGIN_EXAMPLES.md` for a
complete installable plugin.

## Quick Usage

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        "phoson-plugin-mcp",      # Plugin installed via pip
        "phoson-plugin-memory",   # Another plugin
        {
            "name": "phoson-plugin-checkpoint",
            "config": {
                "save_interval": 100,
            }
        },
    ],
)
```

## Creating a Plugin

### Basic Plugin

```python
from phoson_agent import Plugin, AgentTool, tool

class MyPlugin(Plugin):
    @property
    def name(self) -> str:
        return "my-plugin"
    
    def get_tools(self) -> list[AgentTool]:
        @tool
        def my_function(x: int) -> int:
            """My custom function."""
            return x * 2
        
        return [my_function]

# Export the instance
plugin = MyPlugin()
```

### Plugin with Middleware

```python
from phoson_agent import Plugin, AgentMiddleware
from phoson_llm.schemas import Message, ModelConfig

class MyPlugin(Plugin):
    @property
    def name(self) -> str:
        return "my-plugin"
    
    def get_middlewares(self) -> list[AgentMiddleware]:
        class MyMiddleware(AgentMiddleware):
            async def on_before_llm(
                self,
                messages: list[Message],
                config: ModelConfig,
            ) -> list[Message]:
                # Modify messages before the LLM
                print("Before LLM call")
                return messages
        
        return [MyMiddleware()]
```

### Plugin with Configuration

```python
class MyPlugin(Plugin):
    def __init__(self):
        self.setting_value = "default"
    
    @property
    def name(self) -> str:
        return "my-plugin"
    
    def configure(self, config: dict) -> None:
        self.setting_value = config.get("setting", "default")
    
    def initialize(self) -> None:
        # Setup (connections, resources, etc.)
        print(f"Initialized with setting: {self.setting_value}")
    
    def cleanup(self) -> None:
        # Teardown (close connections, save state, etc.)
        print("Cleaning up...")
```

## Load Formats

### 1. Installed Plugin (Package)

```python
plugins=["phoson-plugin-memory"]
```

The plugin must be installed via pip and expose a `plugin` attribute in its `__init__.py`:

```python
# phoson_plugin_memory/__init__.py
from ._plugin import MemoryPlugin
plugin = MemoryPlugin()
```

> **Naming convention:** the module file must be `_plugin.py` (with the
> leading underscore), **not** `plugin.py`. A bare `plugin.py` would make
> the `plugin = MemoryPlugin()` assignment shadow the *submodule*
> attribute on the package, so `import phoson_plugin_memory.plugin as m`
> would bind the plugin instance instead of the module.

### 2. Plugin from a Local Path

```python
plugins=["path:./my_plugin.py"]
```

The file must expose a `plugin` attribute or a `create_plugin()` function.

### 3. Plugin from an Entry Point

```python
plugins=["entrypoint:my-plugin"]
```

Configured in `pyproject.toml`:

```toml
[project.entry-points."phoson.plugins"]
my-plugin = "my_package.plugin:create_plugin"
```

### 4. Plugin with Configuration

```python
plugins=[
    {
        "name": "phoson-plugin-memory",
        "config": {
            "max_memories": 100,
            "persist": True,
        }
    }
]
```

### 5. Direct Instance

```python
plugins=[MyPlugin()]
```

## Plugin Lifecycle

1. **Load**: the plugin is imported/instantiated
2. **Configuration**: `configure(config)` is called with the user config
3. **Initialization**: `initialize()` is called for setup
4. **Use**: the agent uses the plugin's tools and middlewares
5. **Cleanup**: `cleanup()` is called on exit

## Context Manager

```python
with AgentEngine(chat=OpenAIChat(), plugins=[...]) as engine:
    result = await engine.run(messages, config)
# cleanup() is called automatically
```

## Custom Loader

```python
from phoson_agent import register_loader, Plugin

def load_from_github(repo_url: str) -> Plugin:
    # Your logic to download and load from GitHub
    ...
    return plugin_instance

register_loader("github", load_from_github)

# Now you can use:
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["github:user/repo/plugin.py"],
)
```

## Best Practices

1. **Naming**: use the `phoson-plugin-` prefix for public plugins
2. **Versioning**: follow semantic versioning
3. **Documentation**: document all tools and their parameters
4. **Error Handling**: handle errors gracefully in tools
5. **Cleanup**: always implement `cleanup()` if you use resources
6. **Testing**: write tests for your plugins
7. **Type Hints**: use type hints for a better DX

## Bundled plugins

- `phoson_plugin_mcp`: integrates Model Context Protocol servers.
- `phoson_plugin_checkpoint`: Postgres-backed `SessionStorage` with its own schema (`phoson_checkpoint_*`). See `phoson_plugin_checkpoint/README.md`.
- `phoson_plugin_memory`: short-term (Redis, TTL) and long-term (Postgres) memory exposed as `memory_read`/`memory_write` tools (same `MemoryBackend` for both), plus a separate semantic tier (Qdrant) exposed as `memory_remember`/`memory_recall` — a different interface because it is similarity search, not exact lookup. See `phoson_plugin_memory/README.md`.
- `phoson_plugin_monitor`: long-running monitors (`register_monitor`/`list_monitors`/`stop_monitor`) that outlive a run and re-activate the agent. Kinds: `interval`, `file` (path or glob, polled), `command`. Wakes go to a persistent queue (`data_dir`, default `~/.phoson/monitors/`) and optionally an `on_wake` callback; the CLI drains pending wakes into the next user turn. See `phoson_plugin_monitor/README.md` and `examples/monitor_wake_host.py`.
- `phoson_plugin_otel`: traces every agent run (main engine and sub-agents) as an OTel span tree (`run → step → llm_call`/`tool_call`) and exports it in OTLP/HTTP JSON — to a local trace-file (`sink = "file"`) or a real collector (`sink = "otlp"`, with `headers` for auth; `enable_otel = true` opts in, `auto` picks OTLP when an endpoint is set). Stdlib-only at runtime; the wire format is pinned against the real `opentelemetry-proto` schema by a conformance test. See `phoson_plugin_otel/README.md`.

## Plugin Examples

### Memory Plugin

```python
class MemoryPlugin(Plugin):
    def __init__(self):
        self.store = {}
    
    @property
    def name(self) -> str:
        return "phoson-plugin-memory"
    
    def get_tools(self) -> list[AgentTool]:
        @tool
        def store_memory(key: str, value: str) -> str:
            """Store information in memory."""
            self.store[key] = value
            return f"Stored: {key}"
        
        @tool
        def retrieve_memory(key: str) -> str:
            """Retrieve information from memory."""
            return self.store.get(key, "Not found")
        
        return [store_memory, retrieve_memory]
```

### Checkpoint Plugin

```python
class CheckpointPlugin(Plugin):
    @property
    def name(self) -> str:
        return "phoson-plugin-checkpoint"
    
    def get_middlewares(self) -> list[AgentMiddleware]:
        class CheckpointMiddleware(AgentMiddleware):
            async def on_agent_event(self, event: AgentEvent) -> None:
                if isinstance(event, AgentStepDoneEvent):
                    # Save the checkpoint
                    save_checkpoint(event)
        
        return [CheckpointMiddleware()]
```

### MCP Plugin (Model Context Protocol)

```python
class MCPPlugin(Plugin):
    def __init__(self):
        self.mcp_client = None
    
    @property
    def name(self) -> str:
        return "phoson-plugin-mcp"
    
    def initialize(self) -> None:
        self.mcp_client = MCPClient()
        self.mcp_client.connect()
    
    def get_tools(self) -> list[AgentTool]:
        # Convert MCP tools to AgentTools
        return convert_mcp_tools(self.mcp_client.list_tools())
    
    def cleanup(self) -> None:
        if self.mcp_client:
            self.mcp_client.disconnect()
```

## API Reference

### Plugin

```python
class Plugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...
    
    @property
    def version(self) -> str: ...
    
    @property
    def description(self) -> str: ...
    
    def get_tools(self) -> list[AgentTool]: ...
    def get_middlewares(self) -> list[AgentMiddleware]: ...
    def configure(self, config: dict[str, Any]) -> None: ...
    def initialize(self) -> None: ...
    def cleanup(self) -> None: ...
    async def aclose(self) -> None: ...

    # Optional CLI hooks. They return neutral data from
    # phoson_agent.cli_extensions; do not import Rich/prompt_toolkit.
    def get_commands(self) -> list[CliCommandSpec]: ...
    def get_tool_render_specs(self) -> list[ToolRenderSpec]: ...
    def get_theme_extension(self) -> ThemeExtension | None: ...
```

### Community CLI and UI hooks

A plugin can expose slash commands through `get_commands()`. `handler` is the
name of an async method on the loaded plugin instance; native commands always
win and command aliases must be globally unique. The handler receives a
`CliCommandInvocation` and a narrow `CliCommandContext` (`cwd`, `session_id`,
`notify()` and `ui`). It never receives a `Renderer`, `PhosonApp` or a
prompt_toolkit widget.

`get_tool_render_specs()` may contribute an icon and verb for a plugin-owned
tool. Built-in tools cannot be overridden. `get_theme_extension()` contributes
one immutable theme derived from `dark`, `light`, `ansi` or `no-color`; only
known core tokens may be overridden.

Tools and commands can use `context.ui` / an injected `plugin_ui` service to
publish `NoticeBlock`, `KeyValueBlock`, `TodoListBlock` or `ProgressBlock`, or
to await `confirm()`, `select()` and `form()`. Fullscreen and classic hosts
adapt these requests natively. Non-interactive hosts never prompt and return
`InteractionResult(status="unavailable")`; plugins must handle that result.

Use stable, plugin-prefixed block IDs in shared sessions (for example,
`"my-plugin:sync-progress"`) so independently authored plugins never replace
one another's cards.

### PluginRegistry

```python
class PluginRegistry:
    def register_loader(self, prefix: str, loader: PluginLoader) -> None: ...
    def load(self, spec: PluginSpec) -> Plugin: ...
```

### Utility Functions

```python
def load_plugin(spec: str | dict | Plugin) -> Plugin: ...
def register_loader(prefix: str, loader: PluginLoader) -> None: ...
```
