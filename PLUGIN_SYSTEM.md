# Sistema de Plugins de Phoson Agent 🔌

Un sistema modular y extensible para añadir funcionalidades al AgentEngine.

## 🚀 Uso Rápido

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        "phoson-plugin-mcp",
        "phoson-plugin-memory",
        {
            "name": "phoson-plugin-checkpoint",
            "config": {"save_interval": 100}
        },
    ],
)
```

## 📦 Arquitectura

### Componentes Principales

1. **`Plugin`** - Clase base abstracta para todos los plugins
2. **`PluginSpec`** - Especificación de cómo cargar un plugin
3. **`PluginRegistry`** - Registro de loaders de plugins
4. **`PluginLoader`** - Función que carga un plugin desde una fuente

### Flujo de Carga

```
Usuario → PluginSpec → PluginRegistry → PluginLoader → Plugin
                                            ↓
                                    configure()
                                            ↓
                                    initialize()
                                            ↓
                                    AgentEngine
```

## 🛠️ Crear un Plugin

### Estructura Básica

```python
from phoson_agent import Plugin, AgentTool, tool

class MyPlugin(Plugin):
    @property
    def name(self) -> str:
        return "my-plugin"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    def get_tools(self) -> list[AgentTool]:
        @tool
        def my_function(x: int) -> int:
            """Mi función personalizada."""
            return x * 2
        
        return [my_function]

# Exportar instancia
plugin = MyPlugin()
```

### Lifecycle Hooks

```python
class MyPlugin(Plugin):
    def configure(self, config: dict) -> None:
        """Llamado con la configuración del usuario."""
        self.setting = config.get("setting", "default")
    
    def initialize(self) -> None:
        """Llamado antes de que el agente empiece a correr."""
        # Setup: abrir conexiones, cargar recursos, etc.
        pass
    
    def cleanup(self) -> None:
        """Llamado al finalizar el agente."""
        # Cleanup: cerrar conexiones, guardar estado, etc.
        pass
```

### Proporcionar Middlewares

```python
from phoson_agent import AgentMiddleware
from phoson_llm.schemas import Message, ModelConfig

class MyPlugin(Plugin):
    def get_middlewares(self) -> list[AgentMiddleware]:
        class MyMiddleware(AgentMiddleware):
            async def on_before_llm(
                self, 
                messages: list[Message], 
                config: ModelConfig
            ) -> list[Message]:
                # Modificar mensajes antes del LLM
                return messages
        
        return [MyMiddleware()]
```

## 📥 Formatos de Carga

### 1. Package Instalado

```python
plugins=["phoson-plugin-memory"]
```

**Requisitos:**
- Instalado vía pip: `pip install phoson-plugin-memory`
- Tiene `plugin` en `__init__.py`

### 2. Path Local

```python
plugins=["path:./my_plugin.py"]
```

**Requisitos:**
- Archivo Python con atributo `plugin` o función `create_plugin()`

### 3. Entry Point

```python
plugins=["entrypoint:my-plugin"]
```

**Configuración en `pyproject.toml`:**
```toml
[project.entry-points."phoson.plugins"]
my-plugin = "my_package.plugin:create_plugin"
```

### 4. Con Configuración

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

### 5. Instancia Directa

```python
from my_plugin import MyPlugin

plugins=[MyPlugin()]
```

## 🔧 Loaders Personalizados

```python
from phoson_agent import register_loader, Plugin

def load_from_github(repo_url: str) -> Plugin:
    # Tu lógica para descargar y cargar desde GitHub
    ...
    return plugin_instance

register_loader("github", load_from_github)

# Ahora puedes usar:
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["github:user/repo/plugin.py"],
)
```

## 🧪 Testing

```python
import pytest
from phoson_agent import Plugin, AgentEngine
from unittest.mock import Mock

class TestMyPlugin:
    def test_plugin_provides_tools(self):
        plugin = MyPlugin()
        engine = AgentEngine(chat=Mock(), plugins=[plugin])
        
        assert len(engine.tools) > 0
        assert "my_tool" in [t.name for t in engine.tools]
    
    def test_plugin_cleanup(self):
        plugin = MyPlugin()
        engine = AgentEngine(chat=Mock(), plugins=[plugin])
        
        engine.cleanup()
        # Verificar que se hizo cleanup
```

## 📚 Ejemplos de Plugins

### Memory Plugin

Proporciona memoria persistente al agente:

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

Guarda checkpoints automáticos:

```python
class CheckpointPlugin(Plugin):
    @property
    def name(self) -> str:
        return "phoson-plugin-checkpoint"
    
    def get_middlewares(self) -> list[AgentMiddleware]:
        class CheckpointMiddleware(AgentMiddleware):
            async def on_agent_event(self, event: AgentEvent) -> None:
                if isinstance(event, AgentStepDoneEvent):
                    save_checkpoint(event)
        
        return [CheckpointMiddleware()]
```

### MCP Plugin

Integración con Model Context Protocol:

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
        return convert_mcp_tools(self.mcp_client.list_tools())
    
    def cleanup(self) -> None:
        if self.mcp_client:
            self.mcp_client.disconnect()
```

## ✨ Context Manager

```python
with AgentEngine(chat=OpenAIChat(), plugins=[...]) as engine:
    result = await engine.run(messages, config)
# cleanup() se llama automáticamente
```

## 📖 Mejores Prácticas

1. **Naming**: Usa `phoson-plugin-` como prefijo para plugins públicos
2. **Versioning**: Sigue semantic versioning (semver)
3. **Documentation**: Documenta todos los tools y parámetros
4. **Error Handling**: Maneja errores gracefully
5. **Cleanup**: Implementa `cleanup()` si usas recursos
6. **Testing**: Escribe tests para tus plugins
7. **Type Hints**: Usa type hints para mejor developer experience

## 🔍 API Reference

### Plugin

```python
class Plugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Identificador único del plugin."""
        ...
    
    @property
    def version(self) -> str:
        """Versión del plugin (semver)."""
        return "0.1.0"
    
    @property
    def description(self) -> str:
        """Descripción del plugin."""
        return ""
    
    def get_tools(self) -> list[AgentTool]:
        """Retorna tools proporcionados por el plugin."""
        return []
    
    def get_middlewares(self) -> list[AgentMiddleware]:
        """Retorna middlewares proporcionados por el plugin."""
        return []
    
    def configure(self, config: dict[str, Any]) -> None:
        """Configura el plugin con settings del usuario."""
        pass
    
    def initialize(self) -> None:
        """Inicializa el plugin (setup)."""
        pass
    
    def cleanup(self) -> None:
        """Limpia recursos del plugin."""
        pass
```

### Funciones Utilitarias

```python
def load_plugin(spec: str | dict | Plugin) -> Plugin:
    """Carga un plugin desde una especificación."""
    ...

def register_loader(prefix: str, loader: PluginLoader) -> None:
    """Registra un loader personalizado."""
    ...
```

## 📂 Estructura de Archivos

```
phoson_agent/
├── plugin.py              # Plugin base class
├── plugin_loader.py       # Plugin loading system
└── agent.py              # AgentEngine con soporte de plugins

examples/
├── plugin_example_memory.py    # Ejemplo de memory plugin
└── plugin_usage_example.py     # Ejemplos de uso

tests/
└── phoson_agent/
    └── test_plugin_system.py   # Tests del sistema

docs/
└── plugins.md            # Documentación detallada
```

## 🤝 Contribuir un Plugin

1. Crea tu plugin siguiendo la estructura
2. Escribe tests
3. Documenta el uso y configuración
4. Publica en PyPI con prefijo `phoson-plugin-`
5. Comparte en la comunidad

## 📝 Licencia

MIT - Ver LICENSE para más detalles.
