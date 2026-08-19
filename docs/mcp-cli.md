# MCP Support in Phoson CLI

The Phoson CLI now supports the Model Context Protocol (MCP) to integrate MCP servers directly into your conversations.

## 🔌 Supported Transports

Phoson supports **three MCP transport types**:

- **STDIO** (default): local servers run as processes
- **SSE** (Server-Sent Events): remote servers with streaming
- **HTTP**: standard HTTP servers

This lets you connect both local and remote servers.

## 🚀 Quick Start

### Method 1: Using `/mcp init` (Recommended)

```bash
phoson-cli
```

Inside the CLI:
```
> /mcp init
✅ Created MCP config: ~/.phoson/mcps.json
Configured servers:
  • filesystem (STDIO) - Access to home directory
  • memory (STDIO) - Knowledge storage

> /mcp enable
MCP enabled  ·  saved

> /mcp status
MCP: enabled
Loaded 2 MCP tool(s):
  • mcp_filesystem_read_file
  • mcp_memory_store_memory  # example names; depends on server discovery
```

### Method 2: Create the configuration manually

Create a `~/.phoson/mcps.json` file:

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

Then in the CLI:
```
> /mcp enable
MCP enabled  ·  saved
```

### 3. Check the status

```
> /mcp status
MCP: enabled
Config file: ~/.phoson/mcps.json
Loaded 2 MCP tool(s):
  • mcp_filesystem_read_file
  • mcp_memory_store_memory  # example names; depends on server discovery
```

### 4. Use it!

The agent can now use MCP tools automatically:

```
> List the files in /tmp

[The agent will automatically call mcp_filesystem_read_file]
```

## 📋 Available Commands

### `/mcp init`
Creates a sample configuration file at `~/.phoson/mcps.json`.

```
> /mcp init
✅ Created MCP config: ~/.phoson/mcps.json
Configured servers:
  • filesystem (STDIO) - Access to home directory
  • memory (STDIO) - Knowledge storage

Next steps:
  1. Edit the file to add your servers
  2. Run: /mcp enable
  3. Run: /mcp status
```

### `/mcp status`
Shows the current MCP status, the configured servers, their transport/target and the loaded tools.

```
> /mcp status
MCP: enabled
Config file: ~/.phoson/mcps.json
Configured 2 MCP server(s):
  • github [http] → https://api.example.com/mcp
  • filesystem [stdio] → npx -y @modelcontextprotocol/server-filesystem .
Loaded 2 MCP tool(s):
  • mcp_filesystem_read_file
  • mcp_memory_store_memory  # example names; depends on server discovery
```

### `/mcp enable`
Enables MCP support and reloads the engine.

```
> /mcp enable
MCP enabled  ·  saved
```

### `/mcp disable`
Disables MCP support.

```
> /mcp disable
MCP disabled  ·  saved
```

### `/mcp config <path>`
Changes the MCP config file path.

```
> /mcp config ./my-custom-mcp.json
MCP config file → ./my-custom-mcp.json  ·  saved
```

### `/mcp help`
Shows help for the MCP commands.

```
> /mcp help
MCP (Model Context Protocol) commands:
  /mcp status          Show MCP status and loaded tools
  /mcp enable          Enable MCP support
  /mcp disable         Disable MCP support
  /mcp config <path>   Set MCP config file path
  /mcp help            Show this help
```

## ⚙️ Configuration

### MCP File Location

By default, the CLI looks for the MCP configuration at:

**`~/.phoson/mcps.json`**

You can change this location in three ways:

1. **Environment variable** (temporary):
   ```bash
   export PHOSON_MCP_CONFIG=./my-mcps.json
   phoson-cli
   ```

2. **Config file** (persistent):
   ```toml
   # ~/.phoson/config.toml
   [defaults]
   enable_mcp = true
   mcp_config_file = "~/.phoson/mcps.json"
   ```

3. **Runtime command** (persistent):
   ```
   > /mcp config ./project-mcps.json
   ```

### Environment Variables

```bash
# Enable MCP at startup
export PHOSON_ENABLE_MCP=true

# Specify a custom config file
export PHOSON_MCP_CONFIG=~/.phoson/mcps.json

# Start the CLI
phoson-cli
```

### Persistent Config File

Edit `~/.phoson/config.toml`:

```toml
[defaults]
enable_mcp = true
mcp_config_file = "~/.phoson/mcps.json"
```

## 🔌 Transport Types

### STDIO Transport (Default)

For servers that run as local processes:

```json
{
  "mcpServers": {
    "local-server": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {
        "API_KEY": "value"
      }
    }
  }
}
```

**Characteristics:**
- Runs the server as a child process
- Communication via stdin/stdout
- Ideal for local Node.js/Python servers
- No network required

### SSE Transport

For remote servers with Server-Sent Events:

```json
{
  "mcpServers": {
    "remote-sse": {
      "transport": "sse",
      "url": "http://localhost:3000/sse",
      "headers": {
        "Authorization": "Bearer token",
        "X-API-Key": "key"
      }
    }
  }
}
```

**Characteristics:**
- Connection to remote servers
- Bidirectional streaming
- Ideal for cloud services
- Supports authentication via headers

### HTTP Transport

For standard HTTP servers:

```json
{
  "mcpServers": {
    "remote-http": {
      "transport": "http",
      "url": "http://api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer token"
      }
    }
  }
}
```

**Characteristics:**
- Standard HTTP protocol
- Request/response
- Compatible with REST APIs
- Supports authentication

### Mixing Transports

You can use multiple transports in the same configuration:

```json
{
  "mcpServers": {
    "local-fs": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
    },
    "remote-api": {
      "transport": "http",
      "url": "https://api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer token"
      }
    },
    "streaming-service": {
      "transport": "sse",
      "url": "https://stream.example.com/sse"
    }
  }
}
```

## 📦 Available MCP Servers

### Official from Anthropic

#### 1. Filesystem
Filesystem access.

```json
{
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/directory"]
  }
}
```

**Usage:**
```
> List all Python files in the current directory
> Read the contents of main.py
> Create a new file called test.txt with "Hello World"
```

#### 2. GitHub
GitHub interaction.

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

**Usage:**
```
> Show me the open issues in my repository
> Create a new issue titled "Bug fix needed"
> List recent commits
```

#### 3. Brave Search
Web search.

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

**Usage:**
```
> Search the web for "latest Python features"
> Find news about AI developments
```

#### 4. Memory
Knowledge storage.

```json
{
  "memory": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-memory"]
  }
}
```

**Usage:**
```
> Remember that my favorite color is blue
> What's my favorite color?
> Store this: The project deadline is next Friday
```

#### 5. PostgreSQL
Database.

```json
{
  "postgres": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://user:pass@localhost/db"]
  }
}
```

**Usage:**
```
> Query the users table
> Show me all orders from last week
> Create a new table for products
```

#### 6. Puppeteer
Browser automation.

```json
{
  "puppeteer": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-puppeteer"]
  }
}
```

**Usage:**
```
> Take a screenshot of example.com
> Navigate to google.com and search for "AI"
> Fill out the form on this website
```

#### 7. Slack
Slack integration.

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

**Usage:**
```
> Send a message to #general channel
> List recent messages in #dev
> Create a new channel called #project-x
```

## 🔧 Complete Example

### 1. Install the MCP servers

```bash
# Install Node.js if you do not have it
# Ubuntu/Debian:
sudo apt install nodejs npm

# macOS:
brew install node

# Install the filesystem server
npm install -g @modelcontextprotocol/server-filesystem

# Install the memory server
npm install -g @modelcontextprotocol/server-memory
```

### 2. Create the configuration

**Option A: Use the init command (easiest)**

```bash
phoson-cli

> /mcp init
✅ Created MCP config: ~/.phoson/mcps.json
```

**Option B: Create it manually**

```bash
cat > ~/.phoson/mcps.json << 'EOF'
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

### 3. Start the CLI and enable MCP

```bash
phoson-cli
```

```
> /mcp enable
MCP enabled  ·  saved

> /mcp status
MCP: enabled
Loaded 2 MCP tool(s):
  • mcp_filesystem_read_file
  • mcp_memory_store_memory  # example names; depends on server discovery

> List all files in the current directory
[The agent uses mcp_filesystem_read_file]

> Remember that this is a test project
[The agent uses mcp_memory_store_memory]

> What did I just tell you to remember?
[The agent retrieves it from memory]
```

## 🐛 Troubleshooting

### MCP does not enable

**Problem:** `/mcp enable` loads no tools.

**Solution:**
1. Verify that `~/.phoson/mcps.json` exists
2. Verify that the JSON is valid
3. Verify that Node.js is installed: `node --version`
4. Verify that the MCP servers are installed

### Tools do not appear

**Problem:** `/mcp status` shows 0 tools.

**Solution:**
1. Check the configuration in `~/.phoson/mcps.json`
2. Test manually: `npx -y @modelcontextprotocol/server-filesystem /tmp`
3. Check the error logs in the console

### MCP commands fail

**Problem:** the agent tries to use MCP tools but they fail.

**Solution:**
1. Verify that the servers have the required permissions
2. Check environment variables (API keys, tokens, etc.)
3. Try a simple server first (memory or filesystem)

## 💡 Tips

### 1. Start simple
Begin with simple servers like `memory` or `filesystem` before configuring external services.

### 2. Environment variables
Use environment variables for API keys instead of hard-coding them:

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

Then:
```bash
export GITHUB_TOKEN=ghp_...
phoson-cli
```

### 3. Multiple configurations
Create different configuration files for different contexts:

```bash
# Development
phoson-cli
> /mcp config ./mcp-dev.json

# Production
phoson-cli
> /mcp config ./mcp-prod.json
```

### 4. Temporarily disable
If you need to disable MCP temporarily without losing the configuration:

```
> /mcp disable
[Work without MCP]
> /mcp enable
[MCP comes back with the same configuration]
```

## 🔗 Resources

- [MCP Documentation](https://modelcontextprotocol.io/)
- [MCP Specification](https://spec.modelcontextprotocol.io/)
- [Official MCP Servers](https://github.com/modelcontextprotocol/servers)
- [Plugin System Documentation](./plugins.md)
- [MCP Plugin README](../phoson_plugin_mcp/README.md)

## 📝 Notes

- MCP requires Node.js to be installed
- MCP servers run as separate processes
- Each discovered MCP tool is exposed as `mcp_{server_name}_{tool_name}`.
- If discovery fails, a `mcp_{server_name}_call` fallback proxy is kept.
- The configuration is saved in `~/.phoson/config.toml`
- MCP servers restart on every use (stateless)
