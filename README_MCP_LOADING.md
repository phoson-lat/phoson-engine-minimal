# Cómo se Cargan los MCPs en el CLI - Respuesta Rápida

## 📍 Respuesta Directa

Los MCPs se cargan desde: **`~/.phoson/mcps.json`**

## 🚀 Setup en 3 Comandos

```bash
phoson-cli

> /mcp init
✅ Created MCP config: ~/.phoson/mcps.json

> /mcp enable
MCP enabled  ·  saved

> /mcp status
MCP: enabled
Loaded 2 MCP tool(s):
  • mcp_filesystem_call
  • mcp_memory_call
```

## 🔄 Proceso de Carga Simplificado

```
phoson-cli ejecuta
    ↓
Lee ~/.phoson/config.toml (enable_mcp = true?)
    ↓
Importa MCPPlugin
    ↓
Plugin lee ~/.phoson/mcps.json
    ↓
Crea tools para cada servidor
    ↓
Tools disponibles para el agente
```

## 📂 Estructura de Archivos

```
~/.phoson/
├── config.toml          # enable_mcp = true
├── mcps.json           # ← Servidores MCP aquí
└── sessions/
```

## 🔧 Comandos

- `/mcp init` - Crear config de ejemplo
- `/mcp enable` - Habilitar MCPs
- `/mcp status` - Ver qué está cargado
- `/mcp config <path>` - Cambiar ubicación

## 🔌 Transportes Soportados

- **STDIO** - Procesos locales (default)
- **SSE** - Streaming remoto
- **HTTP** - APIs remotas

## 📋 Ejemplo de Config

```json
{
  "mcpServers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "remote-api": {
      "transport": "http",
      "url": "http://localhost:3000/mcp",
      "headers": {"Authorization": "Bearer token"}
    }
  }
}
```

## 📚 Documentación Detallada

- **docs/mcp-loading.md** - Proceso completo de carga
- **docs/mcp-flow.md** - Diagrama de flujo visual
- **docs/mcp-cli.md** - Guía completa del CLI

---

**TL;DR**: Archivo `~/.phoson/mcps.json` → `/mcp init` + `/mcp enable` → ¡listo!
