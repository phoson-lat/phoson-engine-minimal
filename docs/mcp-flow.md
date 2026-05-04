# Flujo de Carga de MCPs en el CLI

## 🔄 Diagrama de Flujo Completo

```
┌─────────────────────────────────────────────────────────────────────┐
│  1. Usuario ejecuta: phoson-cli                                     │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│  2. CLI carga configuración                                         │
│     • Lee ~/.phoson/config.toml                                     │
│     • Lee variables de entorno                                      │
│     • Crea PhosonConfig                                             │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│  3. PhosonRepl.__init__() se ejecuta                                │
│     • Crea chat, tools, middlewares                                 │
│     • Verifica: config.enable_mcp == True?                          │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
                        ┌───────┴───────┐
                        │               │
                    ❌ No           ✅ Sí
                        │               │
                        │               ↓
                        │   ┌─────────────────────────────────────────┐
                        │   │  4. Cargar Plugin MCP                   │
                        │   │     from phoson_plugin_mcp import       │
                        │   │     MCPPlugin                           │
                        │   └─────────────────────────────────────────┘
                        │               ↓
                        │   ┌─────────────────────────────────────────┐
                        │   │  5. Configurar Plugin                   │
                        │   │     plugin.configure({                  │
                        │   │       "config_file":                    │
                        │   │         "~/.phoson/mcps.json"           │
                        │   │     })                                  │
                        │   └─────────────────────────────────────────┘
                        │               ↓
                        │   ┌─────────────────────────────────────────┐
                        │   │  6. Inicializar Plugin                  │
                        │   │     plugin.initialize()                 │
                        │   │     • Lee ~/.phoson/mcps.json           │
                        │   │     • Parsea JSON                       │
                        │   │     • Extrae mcpServers                 │
                        │   └─────────────────────────────────────────┘
                        │               ↓
                        │   ┌─────────────────────────────────────────┐
                        │   │  7. Crear Tools                         │
                        │   │     Para cada servidor:                 │
                        │   │     • Crea tool mcp_{name}_call         │
                        │   │     • Configura transport (stdio/sse/http)│
                        │   └─────────────────────────────────────────┘
                        │               │
                        └───────────────┘
                                        ↓
┌─────────────────────────────────────────────────────────────────────┐
│  8. AgentEngine se crea con plugins                                 │
│     engine = AgentEngine(                                           │
│         chat=self.chat,                                             │
│         tools=self.tools,                                           │
│         plugins=[mcp_plugin],  # ← Plugin con tools                 │
│     )                                                               │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│  9. Tools MCP disponibles                                           │
│     • mcp_filesystem_call                                           │
│     • mcp_github_call                                               │
│     • mcp_memory_call                                               │
│     • etc...                                                        │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│  10. Usuario interactúa                                             │
│      > List files in /tmp                                           │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│  11. Agente ejecuta tool                                            │
│      • LLM decide usar mcp_filesystem_call                          │
│      • Tool se ejecuta                                              │
│      • Conecta al servidor MCP (según transport)                    │
│      • Retorna resultado                                            │
└─────────────────────────────────────────────────────────────────────┘
```

## 📂 Estructura de Archivos

```
~/.phoson/
├── config.toml          # Configuración general del CLI
│   [defaults]
│   enable_mcp = true                    ← Habilita MCPs
│   mcp_config_file = "~/.phoson/mcps.json"  ← Ubicación de MCPs
│   model = "gpt-4o"
│   provider = "openai"
│
├── mcps.json           # ← CONFIGURACIÓN DE MCPs (este es el importante)
│   {
│     "mcpServers": {
│       "filesystem": {
│         "transport": "stdio",
│         "command": "npx",
│         "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
│       },
│       "github": {
│         "transport": "stdio",
│         "command": "npx",
│         "args": ["-y", "@modelcontextprotocol/server-github"],
│         "env": {
│           "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."
│         }
│       }
│     }
│   }
│
└── sessions/           # Sesiones guardadas
    └── ...
```

## 🎬 Ejemplo Completo de Inicio a Fin

```bash
# 1. Primera vez - Crear configuración
$ phoson-cli

> /mcp init
✅ Created MCP config: ~/.phoson/mcps.json
Configured servers:
  • filesystem (STDIO) - Access to home directory
  • memory (STDIO) - Knowledge storage

Next steps:
  1. Edit the file to add your servers
  2. Run: /mcp enable
  3. Run: /mcp status

# 2. (Opcional) Editar configuración
$ nano ~/.phoson/mcps.json
# Agregar más servidores, cambiar paths, etc.

# 3. Habilitar MCP
> /mcp enable
MCP enabled  ·  saved

# 4. Verificar que se cargaron
> /mcp status
MCP: enabled
Config file: ~/.phoson/mcps.json
Loaded 2 MCP tool(s):
  • mcp_filesystem_call
  • mcp_memory_call

# 5. ¡Usar!
> List all Python files in my home directory

[El agente automáticamente:]
1. LLM decide que necesita listar archivos
2. Ve la tool "mcp_filesystem_call" disponible
3. La llama con los parámetros apropiados
4. Plugin MCP:
   - Ejecuta el servidor filesystem vía STDIO
   - Pasa el comando "list files"
   - Retorna los resultados
5. LLM procesa los resultados
6. Responde al usuario

# 6. Próximas veces
$ phoson-cli
[MCP se carga automáticamente porque enable_mcp = true en config.toml]

> /mcp status
MCP: enabled
[Ya está listo para usar]
```

## 🔀 Cambiar Configuración en Runtime

```bash
> /mcp status
MCP: enabled
Config file: ~/.phoson/mcps.json
Loaded 2 MCP tool(s)

# Cambiar a configuración de proyecto
> /mcp config ./project-mcps.json
MCP config file → ./project-mcps.json  ·  saved

# Se recarga automáticamente
> /mcp status
MCP: enabled
Config file: ./project-mcps.json
Loaded 1 MCP tool(s):
  • mcp_project-files_call

# Volver a default
> /mcp config ~/.phoson/mcps.json
MCP config file → ~/.phoson/mcps.json  ·  saved
```

## 🚀 Optimizaciones

### Carga Lazy

El plugin MCP solo se carga cuando:
- `enable_mcp = true` en la configuración
- El archivo `mcps.json` existe (opcional, puede estar vacío)

### Recarga Eficiente

Al cambiar configuración:
- Solo se recarga el engine si MCP está habilitado
- Se mantiene el estado del chat y la sesión
- Las tools se actualizan inmediatamente

### Fallback Inteligente

Si el plugin no puede importarse:
- Intenta cargar desde path relativo (desarrollo)
- Si falla, continúa sin MCP
- No rompe el CLI

## 📊 Resumen

| Aspecto | Detalle |
|---------|---------|
| **Ubicación default** | `~/.phoson/mcps.json` |
| **Cuándo se carga** | Al iniciar CLI si `enable_mcp = true` |
| **Cómo se carga** | Plugin MCP lee el JSON y crea tools |
| **Cambio de config** | `/mcp config <path>` + recarga automática |
| **Habilitación** | `/mcp enable` (persiste en config.toml) |
| **Verificación** | `/mcp status` |

---

**TL;DR**: Los MCPs se configuran en `~/.phoson/mcps.json` y se cargan automáticamente cuando inicias el CLI con `enable_mcp = true`. Usa `/mcp init` para empezar.
