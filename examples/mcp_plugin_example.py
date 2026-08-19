#!/usr/bin/env python3
"""
Example of using the MCP plugin with Phoson Agent.

This example shows how to:
1. Configure MCP servers via phoson-mcp.json
2. Load the MCP plugin
3. Use MCP tools with the agent
"""

import json
import asyncio
from pathlib import Path

from phoson_agent import AgentEngine


def create_example_config():
    """Create an example MCP configuration file."""
    config = {
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "env": {},
            },
            "memory": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-memory"],
                "env": {},
            },
        }
    }

    config_file = Path("phoson-mcp.json")
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    print(f"✅ Created example config: {config_file}")
    return config_file


async def example_basic():
    """Basic example: Load MCP plugin from config file."""
    print("\n" + "=" * 70)
    print("📦 Example 1: Load MCP Plugin from phoson-mcp.json")
    print("=" * 70)

    # Create example config
    config_file = create_example_config()

    try:
        # Create engine with MCP plugin
        # The plugin will automatically load phoson-mcp.json
        engine = AgentEngine(
            chat=None,  # No LLM needed for this demo
            plugins=["path:./phoson_plugin_mcp/_plugin.py"],
        )

        print(f"\n✅ Plugin loaded: {engine._loaded_plugins[0].name}")
        print(f"🔧 Tools available: {len(engine.tools)}")

        for tool in engine.tools:
            print(f"   • {tool.name}: {tool.description}")

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # Cleanup
        if config_file.exists():
            config_file.unlink()
            print(f"\n🧹 Cleaned up: {config_file}")


async def example_custom_config():
    """Example: Load MCP plugin with custom config file."""
    print("\n" + "=" * 70)
    print("📦 Example 2: Load MCP Plugin with Custom Config")
    print("=" * 70)

    # Create custom config file
    custom_config = Path("custom-mcp.json")
    config = {"servers": {"test": {"command": "echo", "args": ["test"]}}}

    with open(custom_config, "w") as f:
        json.dump(config, f, indent=2)

    print(f"✅ Created custom config: {custom_config}")

    try:
        # Load plugin with custom config file
        engine = AgentEngine(
            chat=None,
            plugins=[
                {
                    "name": "path:./phoson_plugin_mcp/_plugin.py",
                    "config": {"config_file": str(custom_config)},
                }
            ],
        )

        print("\n✅ Plugin loaded with custom config")
        print(f"🔧 Tools available: {len(engine.tools)}")

        for tool in engine.tools:
            print(f"   • {tool.name}")

    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if custom_config.exists():
            custom_config.unlink()
            print(f"\n🧹 Cleaned up: {custom_config}")


async def example_inline_config():
    """Example: Configure MCP servers inline (no file)."""
    print("\n" + "=" * 70)
    print("📦 Example 3: Configure MCP Servers Inline")
    print("=" * 70)

    try:
        # Configure servers directly in code
        engine = AgentEngine(
            chat=None,
            plugins=[
                {
                    "name": "path:./phoson_plugin_mcp/_plugin.py",
                    "config": {
                        "servers": {
                            "echo": {"command": "echo", "args": ["Hello from MCP!"]}
                        }
                    },
                }
            ],
        )

        print("✅ Plugin loaded with inline config")
        print(f"🔧 Tools available: {len(engine.tools)}")

        for tool in engine.tools:
            print(f"   • {tool.name}")

    except Exception as e:
        print(f"❌ Error: {e}")


async def example_no_config():
    """Example: Load plugin without any servers configured."""
    print("\n" + "=" * 70)
    print("📦 Example 4: Load Plugin Without Configuration")
    print("=" * 70)

    try:
        # Load plugin without config file (should work fine, just no tools)
        engine = AgentEngine(
            chat=None,
            plugins=[
                {
                    "name": "path:./phoson_plugin_mcp/_plugin.py",
                    "config": {"config_file": "./nonexistent.json"},
                }
            ],
        )

        print("✅ Plugin loaded (no servers configured)")
        print(f"🔧 Tools available: {len(engine.tools)}")

        if len(engine.tools) == 0:
            print("   (No tools - no MCP servers configured)")

    except Exception as e:
        print(f"❌ Error: {e}")


async def main():
    """Run all examples."""
    print("\n" + "=" * 70)
    print("🔌 Phoson MCP Plugin - Examples")
    print("=" * 70)

    print("\nNote: These examples demonstrate plugin loading.")
    print("To actually use MCP servers, you need:")
    print("  1. Node.js installed (for npx commands)")
    print("  2. MCP servers installed (e.g., @modelcontextprotocol/server-*)")
    print("  3. A real LLM chat instance")

    await example_basic()
    await example_custom_config()
    await example_inline_config()
    await example_no_config()

    print("\n" + "=" * 70)
    print("✨ Examples completed!")
    print("=" * 70)

    print("\n💡 Next steps:")
    print("   1. Install MCP servers: npm install -g @modelcontextprotocol/server-*")
    print("   2. Configure phoson-mcp.json with your servers")
    print("   3. Use with a real LLM:")
    print("      engine = AgentEngine(")
    print("          chat=OpenAIChat(),")
    print('          plugins=["path:./phoson_plugin_mcp/_plugin.py"],')
    print("      )")


if __name__ == "__main__":
    asyncio.run(main())
