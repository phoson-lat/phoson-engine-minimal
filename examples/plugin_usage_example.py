"""
Example of using the plugin system with AgentEngine.
"""

import asyncio

from phoson_llm import Message, OpenAIChat, ModelConfig
from phoson_agent import Plugin, AgentTool, AgentEngine, tool


# Example 1: Using a plugin from a local file
async def example_local_plugin():
    """Load a plugin from a local Python file."""

    engine = AgentEngine(
        chat=OpenAIChat(),
        plugins=[
            "path:./examples/plugin_example_memory.py",
        ],
    )

    result = await engine.run(
        messages=[
            Message(role="user", content="Store a memory: my_name is Alice"),
            Message(role="user", content="What is my name?"),
        ],
        config=ModelConfig(model="gpt-4o-mini"),
    )

    print("Result:", result.final_content)
    engine.cleanup()


# Example 2: Using a plugin with configuration
async def example_plugin_with_config():
    """Load a plugin with custom configuration."""

    engine = AgentEngine(
        chat=OpenAIChat(),
        plugins=[
            {
                "name": "path:./examples/plugin_example_memory.py",
                "config": {
                    "max_memories": 50,
                },
            },
        ],
    )

    result = await engine.run(
        messages=[Message(role="user", content="Hello!")],
        config=ModelConfig(model="gpt-4o-mini"),
    )

    print("Result:", result.final_content)
    engine.cleanup()


# Example 3: Creating an inline plugin
class LoggingPlugin(Plugin):
    """Simple plugin that logs all agent events."""

    @property
    def name(self) -> str:
        return "logging-plugin"

    def get_tools(self) -> list[AgentTool]:
        @tool
        def log_message(message: str) -> str:
            """Log a message to the console."""
            print(f"[LOG] {message}")
            return "Message logged"

        return [log_message]


async def example_inline_plugin():
    """Use a plugin defined inline."""

    engine = AgentEngine(
        chat=OpenAIChat(),
        plugins=[
            LoggingPlugin(),  # Pass plugin instance directly
        ],
    )

    result = await engine.run(
        messages=[Message(role="user", content="Log this: Hello World!")],
        config=ModelConfig(model="gpt-4o-mini"),
    )

    print("Result:", result.final_content)
    engine.cleanup()


# Example 4: Mixing plugins with regular tools
async def example_mixed():
    """Mix plugins with regular tools."""

    @tool
    def custom_tool(x: int, y: int) -> int:
        """Add two numbers."""
        return x + y

    engine = AgentEngine(
        chat=OpenAIChat(),
        tools=[custom_tool],  # Regular tools
        plugins=[
            LoggingPlugin(),  # Plugin
        ],
    )

    result = await engine.run(
        messages=[Message(role="user", content="Add 5 and 3")],
        config=ModelConfig(model="gpt-4o-mini"),
    )

    print("Result:", result.final_content)
    engine.cleanup()


# Example 5: Using context manager for automatic cleanup
async def example_context_manager():
    """Use context manager for automatic plugin cleanup."""

    with AgentEngine(
        chat=OpenAIChat(),
        plugins=[LoggingPlugin()],
    ) as engine:
        result = await engine.run(
            messages=[Message(role="user", content="Hello!")],
            config=ModelConfig(model="gpt-4o-mini"),
        )
        print("Result:", result.final_content)
    # Cleanup is called automatically


# Example 6: Multiple plugins
async def example_multiple_plugins():
    """Use multiple plugins together."""

    engine = AgentEngine(
        chat=OpenAIChat(),
        plugins=[
            "path:./examples/plugin_example_memory.py",
            LoggingPlugin(),
            # Could add more:
            # "phoson-plugin-mcp",
            # "phoson-plugin-checkpoint",
        ],
    )

    result = await engine.run(
        messages=[Message(role="user", content="Store and log a message")],
        config=ModelConfig(model="gpt-4o-mini"),
    )

    print("Result:", result.final_content)
    engine.cleanup()


if __name__ == "__main__":
    # Run examples
    print("=== Example 1: Local Plugin ===")
    asyncio.run(example_local_plugin())

    print("\n=== Example 2: Plugin with Config ===")
    asyncio.run(example_plugin_with_config())

    print("\n=== Example 3: Inline Plugin ===")
    asyncio.run(example_inline_plugin())

    print("\n=== Example 4: Mixed Tools and Plugins ===")
    asyncio.run(example_mixed())

    print("\n=== Example 5: Context Manager ===")
    asyncio.run(example_context_manager())

    print("\n=== Example 6: Multiple Plugins ===")
    asyncio.run(example_multiple_plugins())
