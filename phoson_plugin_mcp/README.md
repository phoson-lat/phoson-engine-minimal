# Phoson MCP Plugin

Plugin to integrate Model Context Protocol (MCP) servers with Phoson Agent.

## Installation

```bash
# Install the MCP package
pip install mcp

# The plugin ships with phoson-engine
```

## Configuration

### Config File Location

The plugin uses the path given in its configuration.

- In the CLI, Phoson passes `~/.phoson/mcps.json` by default.
- If you use the plugin directly without setting `config_file`, it uses `phoson-mcp.json` in the current directory.

### Creating the Config

Create the `~/.phoson/mcps.json` file (recommended for the CLI):

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

## Usage

### Basic Usage

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat

# Without config_file, the plugin loads phoson-mcp.json from the current directory
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["phoson-plugin-mcp"],
)
```

### Custom Configuration

```python
# Specify a different config file
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

### Inline Configuration

```python
# Configure servers directly in code
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

### Using a Local Path

```python
# Load the plugin from a local directory
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["path:./phoson_plugin_mcp/_plugin.py"],
)
```

## Config File Format

The MCP config file supports three transport types: **STDIO**, **SSE** and **HTTP**.

### STDIO Transport (Default)

For servers that run as local processes:

```json
{
  "mcpServers": {
    "server-name": {
      "transport": "stdio",
      "command": "executable-command",
      "args": ["arg1", "arg2"],
      "env": {
        "VARIABLE": "valor"
      }
    }
  }
}
```

**Fields:**
- `transport`: "stdio" (optional, it is the default)
- `command`: Command to run the server
- `args`: List of arguments
- `env`: Environment variables (optional)

### SSE Transport

For servers that expose Server-Sent Events:

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

**Fields:**
- `transport`: "sse"
- `url`: SSE endpoint URL (required)
- `headers`: HTTP headers (optional)

### HTTP Transport

For standard HTTP servers:

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

**Fields:**
- `transport`: "http" or "streamable_http"
- `url`: MCP server URL (required)
- `headers`: HTTP headers (optional)

## Available MCP Servers

### Official from Anthropic

1. **Filesystem** - Filesystem access
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/directory"]
   }
   ```

2. **GitHub** - GitHub interaction
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-github"],
     "env": {
       "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."
     }
   }
   ```

3. **Brave Search** - Web search
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-brave-search"],
     "env": {
       "BRAVE_API_KEY": "..."
     }
   }
   ```

4. **Memory** - Knowledge storage
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-memory"]
   }
   ```

5. **PostgreSQL** - Database
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://..."]
   }
   ```

6. **Puppeteer** - Browser automation
   ```json
   {
     "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-puppeteer"]
   }
   ```

7. **Slack** - Slack integration
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

## Complete Example

```python
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat, ModelConfig, Message

# Create the engine with the MCP plugin
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=["phoson-plugin-mcp"],
)

# Use the agent
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

## How MCP Tools Work

The plugin now **discovers the real tools** from each MCP server and exposes them as native `AgentTool`s.

### Before

A generic wrapper tool per server was exposed:

- `mcp_github_call(tool_name="get_user_public_profile", arguments={...})`

### Now

The real tools are exposed with a configurable prefix to avoid collisions:

- `mcp_github_get_user_public_profile(username="phoson-lat")`
- `mcp_filesystem_read_file(path="/tmp/test.txt")`
- `mcp_memory_store_memory(key="x", value="y")`

### Naming Convention

Tools are registered as:

```text
{tool_name_prefix}_{server_name}_{remote_tool_name}
```

The prefix defaults to `mcp`. This avoids collisions with local tools or other plugins.

### Configuring the Prefix

You can change the prefix like this:

```python
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        {
            "name": "phoson-plugin-mcp",
            "config": {
                "tool_name_prefix": "remote"
            }
        }
    ],
)
```

Example resulting names:
- `remote_github_get_user_public_profile`
- `remote_filesystem_read_file`

This avoids collisions between servers and lets the model see more natural tools.

### Benefits

- The model reasons better about specific tools
- Parameter schemas are the real ones from the MCP server
- No `tool_name + arguments` wrapper needed
- Better DX and better LLM tool selection

The agent can call these tools automatically as needed.

## Session pooling

Each MCP server keeps **a single active session/connection**, reused across tool calls (instead of reconnecting — and, for STDIO, relaunching the subprocess — on every call). The remote tool list is also cached per server after the first call.

If a cached session fails (broken pipe, dead process), it is discarded automatically and the next call reconnects on its own.

```bash
python scripts/benchmark_mcp_pooling.py --calls 10
```

In this repo, against a local STDIO test server, pooling gives ~11x lower latency for successive calls to the same tool (reconnecting means relaunching the full subprocess on every call).

For a clean shutdown of pooled connections, prefer `await plugin.aclose()` over `plugin.cleanup()` when you are already inside an event loop (`cleanup()` is sync and can only close connections safely if no loop is running).

## Troubleshooting

### Error: "MCP package not installed"

Install the MCP package:
```bash
pip install mcp
```

### Error: "Failed to load MCP config"

Make sure:
1. The `phoson-mcp.json` file exists
2. The JSON is valid
3. You have read permissions

### Error: "Tool not found"

The MCP server may not have the requested tool. Check:
1. The server is configured correctly
2. The server command is correct
3. The server dependencies are installed

### Node.js Servers

The official MCP servers require Node.js. Install it with:
```bash
# Ubuntu/Debian
sudo apt install nodejs npm

# macOS
brew install node

# Windows
# Download from nodejs.org
```

## Resources

- [MCP Documentation](https://modelcontextprotocol.io/)
- [MCP Specification](https://spec.modelcontextprotocol.io/)
- [Official MCP Servers](https://github.com/modelcontextprotocol/servers)
- [Phoson Plugin System](../docs/plugins.md)

## License

MIT
