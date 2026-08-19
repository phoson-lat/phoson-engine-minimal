#!/usr/bin/env python3
"""
Usage example exactly as requested.
"""

import asyncio

from phoson_llm import OpenAIChat
from phoson_agent import AgentEngine


async def main():
    """
    The exact usage as requested:

    engine = AgentEngine(
        chat=OpenAIChat(),
        plugins=[
            "phoson-plugin-mcp",
            "phoson-plugin-memory",
            "phoson-plugin-checkpoint",
        ],
    )
    """

    print("=" * 70)
    print("🔌 Phoson Agent - Plugin System Usage")
    print("=" * 70)

    # Note: since the real plugins are not published yet, we use the local example
    # In production, this would work with plugins installed via pip

    print("\n📦 Example 1: Plugins as strings (once published)")
    print("-" * 70)
    print("""
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        "phoson-plugin-mcp",  # pip install phoson-plugin-mcp
        "phoson-plugin-memory",  # pip install phoson-plugin-memory
        "phoson-plugin-checkpoint",  # pip install phoson-plugin-checkpoint
    ],
)
""")

    print("\n📦 Example 2: With custom configuration")
    print("-" * 70)
    print("""
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        "phoson-plugin-mcp",
        {
            "name": "phoson-plugin-memory",
            "config": {
                "max_memories": 100,
                "persist": True,
                "storage_path": "./memories"
            }
        },
        {
            "name": "phoson-plugin-checkpoint",
            "config": {
                "save_interval": 100,
                "checkpoint_dir": "./checkpoints"
            }
        },
    ],
)
""")

    print("\n📦 Example 3: Mixing different formats")
    print("-" * 70)
    print("""
from my_custom_plugin import MyPlugin

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        "phoson-plugin-mcp",                    # Installed package
        "path:./local_plugin.py",              # Local plugin
        MyPlugin(),                             # Direct instance
        {
            "name": "phoson-plugin-memory",
            "config": {"max_memories": 50}
        },
    ],
)
""")

    print("\n🚀 Working demo with a local plugin")
    print("-" * 70)

    # Real demo with the example plugin
    engine = AgentEngine(
        chat=OpenAIChat(),
        plugins=[
            "path:./examples/plugin_example_memory.py",
        ],
    )

    print(f"✅ Engine creado con {len(engine._loaded_plugins)} plugin(s)")
    print(f"🔧 Available tools: {[t.name for t in engine.tools]}")
    print(f"🔀 Active middlewares: {len(engine.middlewares)}")

    # Try the plugin tools
    print("\n🧪 Testing the memory plugin tools:")

    store_tool = engine._tools_by_name["store_memory"]
    result = store_tool.handler({"key": "user_name", "value": "Alice"}, engine.context)
    print(f"  → store_memory('user_name', 'Alice'): {result}")

    retrieve_tool = engine._tools_by_name["retrieve_memory"]
    result = retrieve_tool.handler({"key": "user_name"}, engine.context)
    print(f"  → retrieve_memory('user_name'): {result}")

    list_tool = engine._tools_by_name["list_memories"]
    result = list_tool.handler({}, engine.context)
    print(f"  → list_memories(): {result}")

    # Cleanup
    print("\n🧹 Cleaning up resources...")
    engine.cleanup()

    print("\n✨ Demo complete!")
    print("\n" + "=" * 70)
    print("💡 Next steps:")
    print("   1. Implement the real plugins (phoson-plugin-mcp, etc.)")
    print("   2. Publish them to PyPI")
    print("   3. Instalar con: pip install phoson-plugin-<name>")
    print("   4. Use them as in Example 1")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
