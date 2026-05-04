#!/usr/bin/env python3
"""
Example demonstrating all three MCP transport types:
- STDIO (local processes)
- SSE (Server-Sent Events)
- HTTP (standard HTTP)
"""

import json
import asyncio
from pathlib import Path
from phoson_agent import AgentEngine


def create_stdio_config():
    """Create configuration with STDIO transport."""
    config = {
        "mcpServers": {
            "filesystem": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "env": {}
            },
            "memory": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-memory"],
                "env": {}
            }
        }
    }
    
    config_file = Path("mcp-stdio.json")
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)
    
    print(f"✅ Created STDIO config: {config_file}")
    return config_file


def create_sse_config():
    """Create configuration with SSE transport."""
    config = {
        "mcpServers": {
            "remote-sse": {
                "transport": "sse",
                "url": "http://localhost:3000/sse",
                "headers": {
                    "Authorization": "Bearer demo-token",
                    "X-Client": "phoson-agent"
                }
            }
        }
    }
    
    config_file = Path("mcp-sse.json")
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)
    
    print(f"✅ Created SSE config: {config_file}")
    return config_file


def create_http_config():
    """Create configuration with HTTP transport."""
    config = {
        "mcpServers": {
            "remote-http": {
                "transport": "http",
                "url": "http://localhost:3000/mcp",
                "headers": {
                    "Authorization": "Bearer demo-token",
                    "Content-Type": "application/json"
                }
            }
        }
    }
    
    config_file = Path("mcp-http.json")
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)
    
    print(f"✅ Created HTTP config: {config_file}")
    return config_file


def create_mixed_config():
    """Create configuration mixing all three transports."""
    config = {
        "mcpServers": {
            "local-filesystem": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                "env": {}
            },
            "remote-api": {
                "transport": "http",
                "url": "https://api.example.com/mcp",
                "headers": {
                    "Authorization": "Bearer api-key-here"
                }
            },
            "streaming-service": {
                "transport": "sse",
                "url": "https://stream.example.com/sse",
                "headers": {
                    "X-API-Key": "key-here"
                }
            }
        }
    }
    
    config_file = Path("mcp-mixed.json")
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)
    
    print(f"✅ Created mixed config: {config_file}")
    return config_file


async def example_stdio():
    """Example: Load plugin with STDIO transport."""
    print("\n" + "=" * 70)
    print("📦 Example 1: STDIO Transport (Local Processes)")
    print("=" * 70)
    
    config_file = create_stdio_config()
    
    try:
        engine = AgentEngine(
            chat=None,
            plugins=[
                {
                    "name": "path:./phoson_plugin_mcp/plugin.py",
                    "config": {"config_file": str(config_file)}
                }
            ],
        )
        
        print(f"\n✅ Plugin loaded with STDIO transport")
        print(f"🔧 Tools available: {len(engine.tools)}")
        for tool in engine.tools:
            print(f"   • {tool.name}")
        
        print("\n💡 STDIO transport:")
        print("   - Runs servers as local processes")
        print("   - Communication via stdin/stdout")
        print("   - Best for: Node.js, Python local servers")
        
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if config_file.exists():
            config_file.unlink()


async def example_sse():
    """Example: Load plugin with SSE transport."""
    print("\n" + "=" * 70)
    print("📦 Example 2: SSE Transport (Server-Sent Events)")
    print("=" * 70)
    
    config_file = create_sse_config()
    
    try:
        engine = AgentEngine(
            chat=None,
            plugins=[
                {
                    "name": "path:./phoson_plugin_mcp/plugin.py",
                    "config": {"config_file": str(config_file)}
                }
            ],
        )
        
        print(f"\n✅ Plugin loaded with SSE transport")
        print(f"🔧 Tools available: {len(engine.tools)}")
        for tool in engine.tools:
            print(f"   • {tool.name}")
        
        print("\n💡 SSE transport:")
        print("   - Connects to remote servers")
        print("   - Bidirectional streaming")
        print("   - Best for: Cloud services, real-time updates")
        print("   - Requires: Server running at http://localhost:3000/sse")
        
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if config_file.exists():
            config_file.unlink()


async def example_http():
    """Example: Load plugin with HTTP transport."""
    print("\n" + "=" * 70)
    print("📦 Example 3: HTTP Transport (Standard HTTP)")
    print("=" * 70)
    
    config_file = create_http_config()
    
    try:
        engine = AgentEngine(
            chat=None,
            plugins=[
                {
                    "name": "path:./phoson_plugin_mcp/plugin.py",
                    "config": {"config_file": str(config_file)}
                }
            ],
        )
        
        print(f"\n✅ Plugin loaded with HTTP transport")
        print(f"🔧 Tools available: {len(engine.tools)}")
        for tool in engine.tools:
            print(f"   • {tool.name}")
        
        print("\n💡 HTTP transport:")
        print("   - Standard HTTP protocol")
        print("   - Request/response model")
        print("   - Best for: REST APIs, existing HTTP services")
        print("   - Requires: Server running at http://localhost:3000/mcp")
        
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if config_file.exists():
            config_file.unlink()


async def example_mixed():
    """Example: Mix all three transports."""
    print("\n" + "=" * 70)
    print("📦 Example 4: Mixed Transports (All Three)")
    print("=" * 70)
    
    config_file = create_mixed_config()
    
    try:
        engine = AgentEngine(
            chat=None,
            plugins=[
                {
                    "name": "path:./phoson_plugin_mcp/plugin.py",
                    "config": {"config_file": str(config_file)}
                }
            ],
        )
        
        print(f"\n✅ Plugin loaded with mixed transports")
        print(f"🔧 Tools available: {len(engine.tools)}")
        for tool in engine.tools:
            print(f"   • {tool.name}")
        
        print("\n💡 Mixed transports:")
        print("   - Combine local and remote servers")
        print("   - Use best transport for each service")
        print("   - STDIO for local tools")
        print("   - HTTP/SSE for remote APIs")
        
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if config_file.exists():
            config_file.unlink()


async def main():
    """Run all examples."""
    print("\n" + "=" * 70)
    print("🔌 MCP Transports - Complete Examples")
    print("=" * 70)
    
    print("\nPhoson MCP plugin supports three transport types:")
    print("  1. STDIO - Local processes (default)")
    print("  2. SSE - Server-Sent Events (remote streaming)")
    print("  3. HTTP - Standard HTTP (remote request/response)")
    
    await example_stdio()
    await example_sse()
    await example_http()
    await example_mixed()
    
    print("\n" + "=" * 70)
    print("✨ Examples completed!")
    print("=" * 70)
    
    print("\n📚 Transport Selection Guide:")
    print("")
    print("Use STDIO when:")
    print("  • Running servers locally")
    print("  • Using Node.js/Python MCP servers")
    print("  • No network required")
    print("")
    print("Use SSE when:")
    print("  • Connecting to remote services")
    print("  • Need real-time streaming")
    print("  • Want bidirectional communication")
    print("")
    print("Use HTTP when:")
    print("  • Integrating with existing REST APIs")
    print("  • Simple request/response pattern")
    print("  • Standard HTTP infrastructure")
    print("")
    print("💡 You can mix all three in the same configuration!")


if __name__ == "__main__":
    asyncio.run(main())
