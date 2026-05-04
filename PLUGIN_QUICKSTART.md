# Plugin System - Quickstart 🚀

## TL;DR

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        "phoson-plugin-mcp",
        "phoson-plugin-memory",
        "phoson-plugin-checkpoint",
    ],
)
```

**¡Eso es todo!** Los plugins automáticamente:
- ✅ Se cargan e inicializan
- ✅ Proporcionan sus tools al agente
- ✅ Inyectan sus middlewares
- ✅ Se limpian al finalizar

## ¿Qué es un Plugin?

Un plugin es un módulo que **extiende las capacidades** del AgentEngine:

- **Tools**: Funciones que el agente puede llamar
- **Middlewares**: Hooks en el ciclo de vida del agente
- **Configuración**: Settings personalizables

## Crear un Plugin en 30 segundos

```python
from phoson_agent import Plugin, tool

class MyPlugin(Plugin):
    @property
    def name(self) -> str:
        return "my-plugin"
    
    def get_tools(self):
        @tool
        def hello(name: str) -> str:
            """Say hello to someone."""
            return f"Hello, {name}!"
        
        return [hello]

plugin = MyPlugin()
```

**Guardar como** `my_plugin.py` y usar:

```python
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["path:./my_plugin.py"],
)
```

## Formatos Soportados

```python
plugins=[
    # String simple
    "phoson-plugin-mcp",
    
    # Con configuración
    {
        "name": "phoson-plugin-memory",
        "config": {"max_memories": 100}
    },
    
    # Desde archivo local
    "path:./my_plugin.py",
    
    # Instancia directa
    MyPlugin(),
]
```

## Ejemplos Incluidos

### 🧪 Demo Simple
```bash
python examples/simple_plugin_demo.py
```

### 📚 Todos los Ejemplos
```bash
python examples/usage_as_requested.py
```

### 💾 Memory Plugin
Ver: `examples/plugin_example_memory.py`

## Lifecycle

```
1. Load      → Plugin se importa/instancia
2. Configure → configure(config) se llama
3. Initialize → initialize() se llama
4. Use       → Agent usa tools/middlewares
5. Cleanup   → cleanup() se llama
```

## Context Manager

```python
with AgentEngine(chat=OpenAIChat(), plugins=[...]) as engine:
    result = await engine.run(messages, config)
# cleanup() automático
```

## Estructura Mínima

```python
from phoson_agent import Plugin

class MyPlugin(Plugin):
    @property
    def name(self) -> str:
        return "my-plugin"  # Único requerido
```

## Estructura Completa

```python
from phoson_agent import Plugin, AgentTool, AgentMiddleware, tool

class MyPlugin(Plugin):
    # Requerido
    @property
    def name(self) -> str:
        return "my-plugin"
    
    # Opcional
    @property
    def version(self) -> str:
        return "1.0.0"
    
    @property
    def description(self) -> str:
        return "What this plugin does"
    
    def configure(self, config: dict) -> None:
        self.setting = config.get("setting", "default")
    
    def initialize(self) -> None:
        # Setup: abrir conexiones, cargar recursos
        pass
    
    def get_tools(self) -> list[AgentTool]:
        @tool
        def my_tool(x: int) -> int:
            """Tool description."""
            return x * 2
        
        return [my_tool]
    
    def get_middlewares(self) -> list[AgentMiddleware]:
        class MyMiddleware(AgentMiddleware):
            async def on_before_llm(self, messages, config):
                return messages
        
        return [MyMiddleware()]
    
    def cleanup(self) -> None:
        # Cleanup: cerrar conexiones, guardar estado
        pass

plugin = MyPlugin()
```

## Tests

```bash
pytest tests/phoson_agent/test_plugin_system.py -v
```

## Documentación

- 📖 **Guía completa**: `docs/plugins.md`
- 🔧 **Sistema**: `PLUGIN_SYSTEM.md`
- 💡 **Ejemplos**: `examples/PLUGIN_EXAMPLES.md`
- 📊 **Resumen**: `PLUGIN_IMPLEMENTATION_SUMMARY.md`

## Próximos Pasos

1. **Implementar plugins reales**:
   - `phoson-plugin-mcp` - Model Context Protocol
   - `phoson-plugin-memory` - Vector store memory
   - `phoson-plugin-checkpoint` - State management

2. **Publicar en PyPI**:
   ```bash
   pip install phoson-plugin-mcp
   pip install phoson-plugin-memory
   pip install phoson-plugin-checkpoint
   ```

3. **Usar**:
   ```python
   engine = AgentEngine(
       chat=OpenAIChat(),
       plugins=[
           "phoson-plugin-mcp",
           "phoson-plugin-memory",
           "phoson-plugin-checkpoint",
       ],
   )
   ```

## FAQ

**Q: ¿Puedo mezclar plugins con tools normales?**  
A: ¡Sí! Los plugins y tools conviven perfectamente.

**Q: ¿Los plugins pueden tener estado?**  
A: Sí, puedes guardar estado en atributos de instancia.

**Q: ¿Qué pasa si un plugin falla en cleanup?**  
A: Se ignora el error y se continúa con otros plugins.

**Q: ¿Puedo crear loaders personalizados?**  
A: Sí, usa `register_loader("prefix", loader_function)`.

**Q: ¿Los plugins comparten el AgentContext?**  
A: Sí, todos tienen acceso al mismo contexto.

## Ayuda

- 🐛 **Issues**: GitHub Issues
- 💬 **Discusiones**: GitHub Discussions
- 📧 **Email**: team@phoson.lat

---

**¡Listo para crear plugins!** 🎉
