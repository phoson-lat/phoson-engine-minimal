# Phoson MCP Plugin

Plugin para integrar servidores Model Context Protocol (MCP) con Phoson Agent.

## Instalación

```bash
# Instalar el paquete MCP
pip install mcp

# El plugin viene incluido con phoson-engine
```

## Configuración

Crea un archivo `phoson-mcp.json` en el directorio raíz de tu proyecto:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {}
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "your-token-here"
      }
    }
  }
}
```

## Uso

### Uso Básico

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

# El plugin cargará automáticamente phoson-mcp.json
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["phoson-plugin-mcp"],
)
```

### Configuración Personalizada

```python
# Especificar un archivo de configuración diferente
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        {
            "name": "phoson-plugin-mcp",
            "config": {
                "config_file": "./config/my-mcp-servers.json"
            }
        }
    ],
)
```

### Configuración Inline

```python
# Configurar servidores directamente en código
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        {
            "name": "phoson-plugin-mcp",
            "config": {
                "servers": {
                    "filesystem": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
                    }
                }
            }
        }
    ],
)
```

### Usando desde Path Local

```python
# Cargar el plugin desde el directorio local
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["path:./phoson_plugin_mcp/plugin.py"],
)
```

## Formato del Archivo de Configuración

El archivo `phoson-mcp.json` soporta tres tipos de transporte: **STDIO**, **SSE** y **HTTP**.

### STDIO Transport (Default)

Para servidores que se ejecutan como procesos locales:

```json
{
  "mcpServers": {
    "server-name": {
      "transport": "stdio",
      "command": "comando-ejecutable",
      "args": ["arg1", "arg2"],
      "env": {
        "VARIABLE": "valor"
      }
    }
  }
}
```

**Campos:**
- `transport`: "stdio" (opcional, es el default)
- `command`: Comando para ejecutar el servidor
- `args`: Lista de argumentos
- `env`: Variables de entorno (opcional)

### SSE Transport

Para servidores que exponen Server-Sent Events:

```json
{
  "mcpServers": {
    "server-name": {
      "transport": "sse",
      "url": "http://localhost:3000/sse",
      "headers": {
        "Authorization": "Bearer token"
      }
    }
  }
}
```

**Campos:**
- `transport`: "sse"
- `url`: URL del endpoint SSE (requerido)
- `headers`: Headers HTTP (opcional)

### HTTP Transport

Para servidores HTTP estándar:

```json
{
  "mcpServers": {
    "server-name": {
      "transport": "http",
      "url": "http://localhost:3000/mcp",
      "headers": {
        "Authorization": "Bearer token",
        "X-API-Key": "key"
      }
    }
  }
}
```

**Campos:**
- `transport`: "http" o "streamable_http"
- `url`: URL del servidor MCP (requerido)
- `headers`: Headers HTTP (opcional)

## Servidores MCP Disponibles

### Oficiales de Anthropic

1. **Filesystem** - Acceso al sistema de archivos
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/directory"]
   }
   ```

2. **GitHub** - Interacción con GitHub
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-github"],
     "env": {
       "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."
     }
   }
   ```

3. **Brave Search** - Búsqueda web
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-brave-search"],
     "env": {
       "BRAVE_API_KEY": "..."
     }
   }
   ```

4. **Memory** - Almacenamiento de conocimiento
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-memory"]
   }
   ```

5. **PostgreSQL** - Base de datos
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://..."]
   }
   ```

6. **Puppeteer** - Automatización de navegador
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-puppeteer"]
   }
   ```

7. **Slack** - Integración con Slack
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-slack"],
     "env": {
       "SLACK_BOT_TOKEN": "xoxb-...",
       "SLACK_TEAM_ID": "T..."
     }
   }
   ```

## Ejemplo Completo

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat, ModelConfig, Message

# Crear engine con plugin MCP
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["phoson-plugin-mcp"],
)

# Usar el agente
result = await engine.run(
    messages=[
        Message(
            role="user",
            content="List the files in the /tmp directory"
        )
    ],
    config=ModelConfig(model="gpt-4o"),
)

print(result.final_content)
```

## Cómo Funcionan las Tools MCP

El plugin expone las tools MCP con el siguiente formato:

- **Nombre**: `mcp_{server_name}_call`
- **Parámetros**:
  - `tool_name`: Nombre de la tool del servidor MCP
  - `arguments`: Argumentos para la tool (dict)

El agente puede llamar estas tools automáticamente según sea necesario.

## Troubleshooting

### Error: "MCP package not installed"

Instala el paquete MCP:
```bash
pip install mcp
```

### Error: "Failed to load MCP config"

Verifica que:
1. El archivo `phoson-mcp.json` existe
2. El JSON es válido
3. Tienes permisos de lectura

### Error: "Tool not found"

El servidor MCP puede no tener la tool solicitada. Verifica:
1. El servidor está configurado correctamente
2. El comando del servidor es correcto
3. Las dependencias del servidor están instaladas

### Servidores Node.js

Los servidores MCP oficiales requieren Node.js. Instala con:
```bash
# Ubuntu/Debian
sudo apt install nodejs npm

# macOS
brew install node

# Windows
# Descargar desde nodejs.org
```

## Recursos

- [MCP Documentation](https://modelcontextprotocol.io/)
- [MCP Specification](https://spec.modelcontextprotocol.io/)
- [Official MCP Servers](https://github.com/modelcontextprotocol/servers)
- [Phoson Plugin System](../PLUGIN_SYSTEM.md)

## Licencia

MIT
