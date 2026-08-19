# Plugin Examples for Phoson Agent

This folder contains examples of how to create and use plugins.

## 📁 Files

### `simple_plugin_demo.py`
Basic demo showing how to create an inline plugin (CalculatorPlugin) and use it with the AgentEngine.

**Run:**
```bash
python examples/simple_plugin_demo.py
```

### `plugin_example_memory.py`
End-to-end example of the real `phoson-plugin-memory` plugin (Redis tier, see `phoson_plugin_memory/`): two fully separate `AgentEngine` instances, where the second reads a memory written by the first — something an in-process dict could never survive.

**Requires Redis:**
```bash
docker compose -f docker-compose.test.yml up -d redis-test
python examples/plugin_example_memory.py
```

### `plugin_usage_example.py`
Collection of examples showing different ways to use plugins:
1. Plugin from a local file
2. Plugin with configuration
3. Inline plugin
4. Mixing plugins with regular tools
5. Context manager for automatic cleanup
6. Multiple plugins

**Run:**
```bash
python examples/plugin_usage_example.py
```

## 🎯 Use Cases

### Simple Plugin (Tools Only)

```python
from phoson_agent import Plugin, tool

class SimplePlugin(Plugin):
    @property
    def name(self) -> str:
        return "simple"
    
    def get_tools(self):
        @tool
        def greet(name: str) -> str:
            """Greet someone."""
            return f"Hello, {name}!"
        
        return [greet]

plugin = SimplePlugin()
```

### Stateful Plugin

```python
class StatefulPlugin(Plugin):
    def __init__(self):
        self.counter = 0
    
    @property
    def name(self) -> str:
        return "stateful"
    
    def get_tools(self):
        @tool
        def increment() -> int:
            """Increment the counter."""
            self.counter += 1
            return self.counter
        
        return [increment]
```

### Configurable Plugin

```python
class ConfigurablePlugin(Plugin):
    def __init__(self):
        self.api_key = None
        self.endpoint = None
    
    @property
    def name(self) -> str:
        return "configurable"
    
    def configure(self, config: dict):
        self.api_key = config.get("api_key")
        self.endpoint = config.get("endpoint", "https://api.example.com")
    
    def initialize(self):
        # Validate configuration
        if not self.api_key:
            raise ValueError("api_key is required")
```

### Plugin with Middleware

```python
class LoggingPlugin(Plugin):
    @property
    def name(self) -> str:
        return "logging"
    
    def get_middlewares(self):
        class LoggingMiddleware(AgentMiddleware):
            async def on_agent_event(self, event):
                print(f"[LOG] {type(event).__name__}")
        
        return [LoggingMiddleware()]
```

### Plugin with Resources

```python
class DatabasePlugin(Plugin):
    def __init__(self):
        self.connection = None
    
    @property
    def name(self) -> str:
        return "database"
    
    def initialize(self):
        # Open connection
        self.connection = connect_to_database()
    
    def cleanup(self):
        # Close connection
        if self.connection:
            self.connection.close()
    
    def get_tools(self):
        @tool
        def query(sql: str) -> dict:
            """Execute SQL query."""
            return self.connection.execute(sql)
        
        return [query]
```

## 🔌 Plugin Template

```python
"""
My Custom Plugin
Description of what it does.
"""

from typing import Any
from phoson_agent import Plugin, AgentTool, AgentMiddleware, tool
from phoson_llm.schemas import Message, ModelConfig


class MyPlugin(Plugin):
    """Plugin description."""
    
    def __init__(self):
        # Initialize state
        pass
    
    @property
    def name(self) -> str:
        return "my-plugin"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    @property
    def description(self) -> str:
        return "What this plugin does"
    
    def configure(self, config: dict[str, Any]) -> None:
        """Configure plugin with user settings."""
        # Extract config values
        pass
    
    def initialize(self) -> None:
        """Initialize plugin (setup resources)."""
        # Setup connections, load data, etc.
        pass
    
    def get_tools(self) -> list[AgentTool]:
        """Provide tools to the agent."""
        
        @tool
        def my_tool(param: str) -> str:
            """Tool description."""
            return f"Result: {param}"
        
        return [my_tool]
    
    def get_middlewares(self) -> list[AgentMiddleware]:
        """Provide middlewares to the agent."""
        
        class MyMiddleware(AgentMiddleware):
            async def on_before_llm(
                self,
                messages: list[Message],
                config: ModelConfig,
            ) -> list[Message]:
                # Modify messages before LLM
                return messages
        
        return [MyMiddleware()]
    
    def cleanup(self) -> None:
        """Cleanup plugin resources."""
        # Close connections, save state, etc.
        pass


# Export plugin instance
plugin = MyPlugin()
```

## 🧪 Testing

```python
import pytest
from phoson_agent import AgentEngine
from unittest.mock import Mock

def test_my_plugin():
    from my_plugin import MyPlugin
    
    plugin = MyPlugin()
    engine = AgentEngine(chat=Mock(), plugins=[plugin])
    
    # Verify tools loaded
    assert len(engine.tools) > 0
    assert "my_tool" in [t.name for t in engine.tools]
    
    # Test tool execution
    tool = engine._tools_by_name["my_tool"]
    result = tool.handler({"param": "test"}, engine.context)
    assert result == "Result: test"
    
    # Test cleanup
    engine.cleanup()
```

## 📚 Resources

- [Full documentation](../docs/plugins.md)
- [Plugin system](../docs/plugins.md)
- [Tests](../tests/phoson_agent/test_plugin_system.py)
