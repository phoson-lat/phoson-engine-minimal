# Plugin System

El sistema de plugins de Phoson Agent permite extender las capacidades del agente de forma modular y reutilizable.

## Decisión de interfaz canónica

Existe un único contrato de plugin soportado: la clase `Plugin` (ABC síncrona) definida en `phoson_agent/plugin.py`, con el ciclo de vida `configure()` → `initialize()` → uso → `cleanup()`.

Anteriormente el roadmap externo describía un segundo contrato, `PhosonPlugin` (async, `on_load`/`on_unload`), pero nunca llegó a implementarse: no existe en el código, no hay loaders para él, y ningún plugin real (`phoson_plugin_mcp`, los ejemplos en `examples/`) lo usa. Se descarta formalmente en favor de `Plugin` porque:

- Es la interfaz que ya implementan `PluginRegistry`/`load_plugin` (`phoson_agent/plugin_loader.py`) y todos los plugins existentes.
- Los tools y middlewares que un plugin expone (`get_tools`, `get_middlewares`) no requieren que la carga/descarga del propio plugin sea async — la parte async vive dentro de los tools (`ToolHandler` ya soporta handlers async), no en el lifecycle del plugin.
- Introducir un segundo contrato solo duplicaría loaders y documentación sin habilitar nada que `initialize()`/`cleanup()` no permitan ya (un plugin puede crear un pool async dentro de `initialize()` de forma síncrona, p.ej. con `asyncio.get_event_loop().run_until_complete(...)` o guardando la corrutina de conexión para el primer uso — ver `phoson_plugin_checkpoint` y `phoson_plugin_memory` como ejemplos).

Todos los plugins nuevos (`phoson_plugin_checkpoint`, `phoson_plugin_memory`) implementan `Plugin`, no `PhosonPlugin`.

## Conceptos Básicos

Un **plugin** puede proporcionar:
- **Tools**: Funciones que el agente puede llamar
- **Middlewares**: Hooks en el ciclo de vida del agente
- **Configuración**: Opciones personalizables

## Uso Rápido

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        "phoson-plugin-mcp",      # Plugin instalado vía pip
        "phoson-plugin-memory",   # Otro plugin
        {
            "name": "phoson-plugin-checkpoint",
            "config": {
                "save_interval": 100,
            }
        },
    ],
)
```

## Crear un Plugin

### Plugin Básico

```python
from phoson_agent import Plugin, AgentTool, tool

class MyPlugin(Plugin):
    @property
    def name(self) -> str:
        return "my-plugin"
    
    def get_tools(self) -> list[AgentTool]:
        @tool
        def my_function(x: int) -> int:
            """Mi función personalizada."""
            return x * 2
        
        return [my_function]

# Exportar instancia
plugin = MyPlugin()
```

### Plugin con Middleware

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
                # Modificar mensajes antes del LLM
                print("Before LLM call")
                return messages
        
        return [MyMiddleware()]
```

### Plugin con Configuración

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
        # Setup (conexiones, recursos, etc)
        print(f"Initialized with setting: {self.setting_value}")
    
    def cleanup(self) -> None:
        # Limpieza (cerrar conexiones, guardar estado, etc)
        print("Cleaning up...")
```

## Formatos de Carga

### 1. Plugin Instalado (Package)

```python
plugins=["phoson-plugin-memory"]
```

El plugin debe estar instalado vía pip y tener un atributo `plugin` en su `__init__.py`:

```python
# phoson_plugin_memory/__init__.py
from .plugin import MemoryPlugin
plugin = MemoryPlugin()
```

### 2. Plugin desde Path Local

```python
plugins=["path:./my_plugin.py"]
```

El archivo debe tener un atributo `plugin` o función `create_plugin()`.

### 3. Plugin desde Entry Point

```python
plugins=["entrypoint:my-plugin"]
```

Configurado en `pyproject.toml`:

```toml
[project.entry-points."phoson.plugins"]
my-plugin = "my_package.plugin:create_plugin"
```

### 4. Plugin con Configuración

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
plugins=[MyPlugin()]
```

## Lifecycle de un Plugin

1. **Carga**: El plugin es importado/instanciado
2. **Configuración**: Se llama `configure(config)` con la config del usuario
3. **Inicialización**: Se llama `initialize()` para setup
4. **Uso**: El agente usa los tools y middlewares del plugin
5. **Cleanup**: Se llama `cleanup()` al finalizar

## Context Manager

```python
with AgentEngine(chat=OpenAIChat(), plugins=[...]) as engine:
    result = await engine.run(messages, config)
# cleanup() se llama automáticamente
```

## Loader Personalizado

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

## Mejores Prácticas

1. **Naming**: Usa el prefijo `phoson-plugin-` para plugins públicos
2. **Versioning**: Sigue semantic versioning
3. **Documentation**: Documenta todos los tools y sus parámetros
4. **Error Handling**: Maneja errores gracefully en tools
5. **Cleanup**: Siempre implementa `cleanup()` si usas recursos
6. **Testing**: Escribe tests para tus plugins
7. **Type Hints**: Usa type hints para mejor DX

## Plugins incluidos

- `phoson_plugin_mcp`: integra servidores Model Context Protocol.
- `phoson_plugin_checkpoint`: `SessionStorage` respaldado en Postgres, esquema propio (`phoson_checkpoint_*`). Ver `phoson_plugin_checkpoint/README.md`.
- `phoson_plugin_memory`: memoria de corto plazo (Redis, TTL) y largo plazo (Postgres) expuesta como tools `memory_read`/`memory_write`, mismo `MemoryBackend` para ambos tiers. Ver `phoson_plugin_memory/README.md`.

## Ejemplos de Plugins

### Plugin de Memoria

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

### Plugin de Checkpoint

```python
class CheckpointPlugin(Plugin):
    @property
    def name(self) -> str:
        return "phoson-plugin-checkpoint"
    
    def get_middlewares(self) -> list[AgentMiddleware]:
        class CheckpointMiddleware(AgentMiddleware):
            async def on_agent_event(self, event: AgentEvent) -> None:
                if isinstance(event, AgentStepDoneEvent):
                    # Guardar checkpoint
                    save_checkpoint(event)
        
        return [CheckpointMiddleware()]
```

### Plugin MCP (Model Context Protocol)

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
        # Convertir herramientas MCP a AgentTools
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
```

### PluginRegistry

```python
class PluginRegistry:
    def register_loader(self, prefix: str, loader: PluginLoader) -> None: ...
    def load(self, spec: PluginSpec) -> Plugin: ...
```

### Funciones Utilitarias

```python
def load_plugin(spec: str | dict | Plugin) -> Plugin: ...
def register_loader(prefix: str, loader: PluginLoader) -> None: ...
```
