# Cómo se Cargan los MCPs en el CLI

## 📍 Ubicación del Archivo de Configuración

El CLI de Phoson busca la configuración de MCPs en:

**`~/.phoson/mcps.json`** (default)

Esta ubicación se puede cambiar con:
- Variable de entorno: `PHOSON_MCP_CONFIG`
- Archivo de config: `~/.phoson/config.toml`
- Comando runtime: `/mcp config <path>`

## 🔄 Flujo de Carga

```
1. Usuario inicia CLI
   ↓
2. CLI lee ~/.phoson/config.toml
   ↓
3. Si enable_mcp = true
   ↓
4. CLI carga phoson_plugin_mcp
   ↓
5. Plugin lee ~/.phoson/mcps.json
   ↓
6. Plugin crea tools para cada servidor
   ↓
7. Tools disponibles en AgentEngine
   ↓
8. Usuario puede usarlos naturalmente
```

## 📝 Paso a Paso

### 1. Crear Configuración

**Opción A: Comando automático**
```bash
phoson-cli
> /mcp init
✅ Created MCP config: ~/.phoson/mcps.json
```

**Opción B: Manual**
```bash
mkdir -p ~/.phoson
cat > ~/.phoson/mcps.json << 'EOF'
{
  "mcpServers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    }
  }
}
EOF
```

### 2. Habilitar MCP

```bash
phoson-cli
> /mcp enable
MCP enabled  ·  saved
```

Esto hace:
1. Actualiza `~/.phoson/config.toml` con `enable_mcp = true`
2. Recarga el `AgentEngine` con el plugin MCP
3. El plugin lee `~/.phoson/mcps.json`
4. Crea tools para cada servidor configurado

### 3. Verificar Carga

```
> /mcp status
MCP: enabled
Config file: ~/.phoson/mcps.json
Loaded 1 MCP tool(s):
  • mcp_filesystem_call
```

### 4. Usar

```
> List files in /tmp
```

El agente automáticamente:
1. Ve que necesita listar archivos
2. Encuentra la tool `mcp_filesystem_call`
3. La ejecuta con los parámetros apropiados
4. Retorna el resultado

## 🔍 Detalles Técnicos

### Carga del Plugin

En `phoson_cli/repl.py`:

```python
# Load plugins
plugins = []
if config.enable_mcp:
    try:
        from phoson_plugin_mcp import MCPPlugin
        mcp_plugin = MCPPlugin()
        mcp_plugin.configure({"config_file": str(config.mcp_config_file)})
        plugins.append(mcp_plugin)
    except ImportError:
        # Fallback para desarrollo
        plugins.append({
            "name": "path:./phoson_plugin_mcp/plugin.py",
            "config": {"config_file": str(config.mcp_config_file)}
        })

self.engine = AgentEngine(
    chat=self.chat,
    tools=self.tools,
    middlewares=[self.summarizer],
    plugins=plugins,  # ← Plugin MCP se carga aquí
    max_iterations=config.max_iterations,
)
```

### Inicialización del Plugin

En `phoson_plugin_mcp/plugin.py`:

```python
def initialize(self):
    # 1. Lee el archivo de configuración
    if self.config_file.exists():
        with open(self.config_file) as f:
            config_data = json.load(f)
        
        # 2. Extrae los servidores
        if "mcpServers" in config_data:
            self.servers.update(config_data["mcpServers"])
    
    # 3. Crea tools para cada servidor
    self._load_tools_from_servers()
```

### Creación de Tools

Para cada servidor en `mcps.json`, el plugin crea una tool:

```python
def _create_server_tools(self, server_name, server_config):
    @tool
    def mcp_call_tool(tool_name: str, arguments: dict = None):
        # Ejecuta la tool en el servidor MCP
        result = asyncio.run(
            self._execute_mcp_tool(server_name, tool_name, arguments)
        )
        return result
    
    # Nombra la tool según el servidor
    mcp_call_tool.name = f"mcp_{server_name}_call"
    
    return [mcp_call_tool]
```

## 🗂️ Estructura de Archivos

```
~/.phoson/
├── config.toml          # Configuración general
│   [defaults]
│   enable_mcp = true
│   mcp_config_file = "~/.phoson/mcps.json"
│
├── mcps.json           # ← Configuración de MCPs
│   {
│     "mcpServers": {
│       "filesystem": {...},
│       "github": {...}
│     }
│   }
│
└── sessions/           # Sesiones guardadas
    └── ...
```

## 🔄 Cuándo se Cargan los MCPs

Los MCPs se cargan en estos momentos:

1. **Al iniciar el CLI**
   - Si `enable_mcp = true` en config
   - Lee `~/.phoson/mcps.json`
   - Crea tools disponibles

2. **Al ejecutar `/mcp enable`**
   - Actualiza config a `enable_mcp = true`
   - Recarga el engine con el plugin
   - Tools inmediatamente disponibles

3. **Al cambiar de modelo**
   - El engine se reconstruye
   - Si MCP está habilitado, se vuelve a cargar
   - Mantiene la misma configuración

4. **Al ejecutar `/mcp config <path>`**
   - Cambia la ruta del archivo
   - Si MCP está habilitado, recarga
   - Lee el nuevo archivo

## 💡 Ejemplo Práctico

```bash
# Primera vez usando MCP
$ phoson-cli

> /mcp init
✅ Created MCP config: ~/.phoson/mcps.json

> /mcp enable
MCP enabled  ·  saved

> /mcp status
MCP: enabled
Loaded 2 MCP tool(s):
  • mcp_filesystem_call
  • mcp_memory_call

> List Python files in my home directory
[Agente usa mcp_filesystem_call automáticamente]

# Próximas veces
$ phoson-cli
[MCP se carga automáticamente porque enable_mcp = true]

> /mcp status
MCP: enabled
Loaded 2 MCP tool(s):
  • mcp_filesystem_call
  • mcp_memory_call

> Remember that my name is Alice
[Agente usa mcp_memory_call]
```

## 🔧 Configuraciones Avanzadas

### Usar Archivo Específico del Proyecto

```bash
# En tu proyecto
cat > ./project-mcps.json << 'EOF'
{
  "mcpServers": {
    "project-files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
    }
  }
}
EOF

# En el CLI
> /mcp config ./project-mcps.json
MCP config file → ./project-mcps.json  ·  saved

> /mcp enable
MCP enabled  ·  saved
```

### Múltiples Configuraciones

```bash
# Desarrollo
> /mcp config ~/.phoson/mcps-dev.json
> /mcp enable

# Producción
> /mcp config ~/.phoson/mcps-prod.json
> /mcp enable
```

### Deshabilitar Temporalmente

```bash
> /mcp disable
MCP disabled  ·  saved

[Trabaja sin MCP]

> /mcp enable
MCP enabled  ·  saved
[Vuelve con la misma configuración]
```

## 🐛 Troubleshooting

### "No MCP tools loaded"

**Causa**: El archivo `~/.phoson/mcps.json` no existe o está vacío.

**Solución**:
```
> /mcp init
✅ Created MCP config: ~/.phoson/mcps.json
```

### "MCP: disabled"

**Causa**: MCP no está habilitado.

**Solución**:
```
> /mcp enable
```

### "Config file: /path/to/file not found"

**Causa**: El archivo de configuración especificado no existe.

**Solución**:
```
> /mcp config ~/.phoson/mcps.json
> /mcp init
```

## 📋 Checklist de Configuración

- [ ] Instalar Node.js (`node --version`)
- [ ] Instalar servidores MCP (`npm install -g @modelcontextprotocol/server-*`)
- [ ] Crear `~/.phoson/mcps.json` (o usar `/mcp init`)
- [ ] Habilitar MCP (`/mcp enable`)
- [ ] Verificar carga (`/mcp status`)
- [ ] ¡Usar! (`List files...`)

## 🎯 Resumen

**Los MCPs se cargan así:**

1. **Archivo de configuración**: `~/.phoson/mcps.json`
2. **Habilitación**: `/mcp enable` o `enable_mcp = true` en config
3. **Carga**: Automática al iniciar CLI si está habilitado
4. **Uso**: Transparente - el agente los usa cuando los necesita

**Comandos clave:**
- `/mcp init` - Crear config de ejemplo
- `/mcp enable` - Habilitar MCPs
- `/mcp status` - Ver qué está cargado
- `/mcp config <path>` - Cambiar ubicación del archivo

---

**Ubicación default**: `~/.phoson/mcps.json`  
**Formato**: JSON con estructura `mcpServers`  
**Carga**: Automática cuando `enable_mcp = true`
