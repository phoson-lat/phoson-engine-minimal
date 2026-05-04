# Ejemplos de Plugins para Phoson Agent

Esta carpeta contiene ejemplos de cómo crear y usar plugins.

## 📁 Archivos

### `simple_plugin_demo.py`
Demo básico que muestra cómo crear un plugin inline (CalculatorPlugin) y usarlo con el AgentEngine.

**Ejecutar:**
```bash
python examples/simple_plugin_demo.py
```

### `plugin_example_memory.py`
Plugin completo de ejemplo que proporciona:
- **Tools**: `store_memory`, `retrieve_memory`, `list_memories`
- **Middleware**: Inyecta contexto de memoria en los mensajes

**Usar:**
```python
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["path:./examples/plugin_example_memory.py"]
)
```

### `plugin_usage_example.py`
Colección de ejemplos mostrando diferentes formas de usar plugins:
1. Plugin desde archivo local
2. Plugin con configuración
3. Plugin inline
4. Mezclar plugins con tools regulares
5. Context manager para cleanup automático
6. Múltiples plugins

**Ejecutar:**
```bash
python examples/plugin_usage_example.py
```

## 🎯 Casos de Uso

### Plugin Simple (Solo Tools)

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

### Plugin con Estado

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

### Plugin con Configuración

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
        # Validar configuración
        if not self.api_key:
            raise ValueError("api_key is required")
```

### Plugin con Middleware

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

### Plugin con Recursos

```python
class DatabasePlugin(Plugin):
    def __init__(self):
        self.connection = None
    
    @property
    def name(self) -> str:
        return "database"
    
    def initialize(self):
        # Abrir conexión
        self.connection = connect_to_database()
    
    def cleanup(self):
        # Cerrar conexión
        if self.connection:
            self.connection.close()
    
    def get_tools(self):
        @tool
        def query(sql: str) -> dict:
            """Execute SQL query."""
            return self.connection.execute(sql)
        
        return [query]
```

## 🔌 Plantilla de Plugin

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

## 📚 Recursos

- [Documentación completa](../docs/plugins.md)
- [Sistema de plugins](../PLUGIN_SYSTEM.md)
- [Tests](../tests/phoson_agent/test_plugin_system.py)
