# MCP Support in Phoson CLI

El CLI de Phoson ahora soporta Model Context Protocol (MCP) para integrar servidores MCP directamente en tus conversaciones.

## 🚀 Inicio Rápido

### 1. Crear configuración MCP

Crea un archivo `phoson-mcp.json` en tu directorio de trabajo:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {}
    },
    "memory": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory"],
      "env": {}
    }
  }
}
```

### 2. Habilitar MCP en el CLI

```bash
phoson-cli
```

Dentro del CLI:
```
> /mcp enable
MCP enabled  ·  saved
Config file: phoson-mcp.json
```

### 3. Verificar estado

```
> /mcp status
MCP: enabled
Config file: phoson-mcp.json
Loaded 2 MCP tool(s):
  • mcp_filesystem_call
  • mcp_memory_call
```

### 4. ¡Usar!

Ahora el agente puede usar automáticamente las herramientas MCP:

```
> List the files in /tmp

[El agente automáticamente llamará a mcp_filesystem_call]
```

## 📋 Comandos Disponibles

### `/mcp status`
Muestra el estado actual de MCP y las herramientas cargadas.

```
> /mcp status
MCP: enabled
Config file: phoson-mcp.json
Loaded 2 MCP tool(s):
  • mcp_filesystem_call
  • mcp_memory_call
```

### `/mcp enable`
Habilita el soporte MCP y recarga el engine.

```
> /mcp enable
MCP enabled  ·  saved
```

### `/mcp disable`
Deshabilita el soporte MCP.

```
> /mcp disable
MCP disabled  ·  saved
```

### `/mcp config <path>`
Cambia la ruta del archivo de configuración MCP.

```
> /mcp config ./my-custom-mcp.json
MCP config file → ./my-custom-mcp.json  ·  saved
```

### `/mcp help`
Muestra ayuda sobre los comandos MCP.

```
> /mcp help
MCP (Model Context Protocol) commands:
  /mcp status          Show MCP status and loaded tools
  /mcp enable          Enable MCP support
  /mcp disable         Disable MCP support
  /mcp config <path>   Set MCP config file path
  /mcp help            Show this help
```

## ⚙️ Configuración

### Variables de Entorno

```bash
# Habilitar MCP al iniciar
export PHOSON_ENABLE_MCP=true

# Especificar archivo de configuración
export PHOSON_MCP_CONFIG=./my-mcp-config.json

# Iniciar CLI
phoson-cli
```

### Archivo de Configuración

Edita `~/.phoson/config.toml`:

```toml
[defaults]
enable_mcp = true
mcp_config_file = "phoson-mcp.json"
```

## 📦 Servidores MCP Disponibles

### Oficiales de Anthropic

#### 1. Filesystem
Acceso al sistema de archivos.

```json
{
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/directory"]
  }
}
```

**Uso:**
```
> List all Python files in the current directory
> Read the contents of main.py
> Create a new file called test.txt with "Hello World"
```

#### 2. GitHub
Interacción con GitHub.

```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {
      "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."
    }
  }
}
```

**Uso:**
```
> Show me the open issues in my repository
> Create a new issue titled "Bug fix needed"
> List recent commits
```

#### 3. Brave Search
Búsqueda web.

```json
{
  "brave-search": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-brave-search"],
    "env": {
      "BRAVE_API_KEY": "..."
    }
  }
}
```

**Uso:**
```
> Search the web for "latest Python features"
> Find news about AI developments
```

#### 4. Memory
Almacenamiento de conocimiento.

```json
{
  "memory": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-memory"]
  }
}
```

**Uso:**
```
> Remember that my favorite color is blue
> What's my favorite color?
> Store this: The project deadline is next Friday
```

#### 5. PostgreSQL
Base de datos.

```json
{
  "postgres": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://user:pass@localhost/db"]
  }
}
```

**Uso:**
```
> Query the users table
> Show me all orders from last week
> Create a new table for products
```

#### 6. Puppeteer
Automatización de navegador.

```json
{
  "puppeteer": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-puppeteer"]
  }
}
```

**Uso:**
```
> Take a screenshot of example.com
> Navigate to google.com and search for "AI"
> Fill out the form on this website
```

#### 7. Slack
Integración con Slack.

```json
{
  "slack": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-slack"],
    "env": {
      "SLACK_BOT_TOKEN": "xoxb-...",
      "SLACK_TEAM_ID": "T..."
    }
  }
}
```

**Uso:**
```
> Send a message to #general channel
> List recent messages in #dev
> Create a new channel called #project-x
```

## 🔧 Ejemplo Completo

### 1. Instalar servidores MCP

```bash
# Instalar Node.js si no lo tienes
# Ubuntu/Debian:
sudo apt install nodejs npm

# macOS:
brew install node

# Instalar servidor filesystem
npm install -g @modelcontextprotocol/server-filesystem

# Instalar servidor memory
npm install -g @modelcontextprotocol/server-memory
```

### 2. Crear configuración

```bash
cat > phoson-mcp.json << 'EOF'
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": {}
    },
    "memory": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory"],
      "env": {}
    }
  }
}
EOF
```

### 3. Iniciar CLI y habilitar MCP

```bash
phoson-cli
```

```
> /mcp enable
MCP enabled  ·  saved

> /mcp status
MCP: enabled
Loaded 2 MCP tool(s):
  • mcp_filesystem_call
  • mcp_memory_call

> List all files in the current directory
[El agente usa mcp_filesystem_call]

> Remember that this is a test project
[El agente usa mcp_memory_call]

> What did I just tell you to remember?
[El agente recupera de la memoria]
```

## 🐛 Troubleshooting

### MCP no se habilita

**Problema:** `/mcp enable` no carga herramientas.

**Solución:**
1. Verifica que `phoson-mcp.json` existe
2. Verifica que el JSON es válido
3. Verifica que Node.js está instalado: `node --version`
4. Verifica que los servidores MCP están instalados

### Herramientas no aparecen

**Problema:** `/mcp status` muestra 0 herramientas.

**Solución:**
1. Verifica la configuración en `phoson-mcp.json`
2. Prueba manualmente: `npx -y @modelcontextprotocol/server-filesystem /tmp`
3. Revisa los logs de errores en la consola

### Comandos MCP fallan

**Problema:** El agente intenta usar herramientas MCP pero fallan.

**Solución:**
1. Verifica que los servidores tienen los permisos necesarios
2. Verifica variables de entorno (API keys, tokens, etc)
3. Prueba con un servidor simple primero (memory o filesystem)

## 💡 Tips

### 1. Empezar simple
Comienza con servidores simples como `memory` o `filesystem` antes de configurar servicios externos.

### 2. Variables de entorno
Usa variables de entorno para API keys en lugar de hardcodearlas:

```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {
      "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
    }
  }
}
```

Luego:
```bash
export GITHUB_TOKEN=ghp_...
phoson-cli
```

### 3. Múltiples configuraciones
Crea diferentes archivos de configuración para diferentes contextos:

```bash
# Desarrollo
phoson-cli
> /mcp config ./mcp-dev.json

# Producción
phoson-cli
> /mcp config ./mcp-prod.json
```

### 4. Deshabilitar temporalmente
Si necesitas deshabilitar MCP temporalmente sin perder la configuración:

```
> /mcp disable
[Trabaja sin MCP]
> /mcp enable
[MCP vuelve con la misma configuración]
```

## 🔗 Recursos

- [MCP Documentation](https://modelcontextprotocol.io/)
- [MCP Specification](https://spec.modelcontextprotocol.io/)
- [Official MCP Servers](https://github.com/modelcontextprotocol/servers)
- [Plugin System Documentation](./plugins.md)
- [MCP Plugin README](../phoson_plugin_mcp/README.md)

## 📝 Notas

- MCP requiere Node.js instalado
- Los servidores MCP se ejecutan como procesos separados
- Cada herramienta MCP se expone como `mcp_{server_name}_call`
- La configuración se guarda en `~/.phoson/config.toml`
- Los servidores MCP se reinician en cada uso (stateless)
