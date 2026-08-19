#!/usr/bin/env python3
"""
Simple demo of the plugin system.
Shows how to create and use an inline plugin.
"""

import asyncio

from phoson_agent import Plugin, AgentTool, AgentEngine, tool


# Define a simple inline plugin
class CalculatorPlugin(Plugin):
    """Plugin that provides math operations."""

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Provides basic math operations"

    def get_tools(self) -> list[AgentTool]:
        """Provides math tools."""

        @tool
        def add(a: float, b: float) -> float:
            """Add two numbers together."""
            return a + b

        @tool
        def multiply(a: float, b: float) -> float:
            """Multiply two numbers."""
            return a * b

        @tool
        def power(base: float, exponent: float) -> float:
            """Raise base to the power of exponent."""
            return base**exponent

        return [add, multiply, power]


async def main():
    """Main demo."""
    print("=" * 60)
    print("🔌 Phoson Agent - Plugin System Demo")
    print("=" * 60)

    # Create the engine with the plugin
    print("\n📦 Loading calculator plugin...")
    engine = AgentEngine(
        chat=None,  # No LLM needed for this demo
        plugins=[
            CalculatorPlugin(),  # Inline plugin
        ],
    )

    # Verify the tools were loaded
    print(f"✅ Plugin loaded: {engine._loaded_plugins[0].name}")
    print(f"🔧 Available tools: {[t.name for t in engine.tools]}")

    # Show tool info
    print("\n📋 Tool info:")
    for tool_obj in engine.tools:
        print(f"  • {tool_obj.name}: {tool_obj.description}")

    # Try the tools directly
    print("\n🧪 Testing the tools:")

    add_tool = engine._tools_by_name["add"]
    result = add_tool.handler({"a": 5, "b": 3}, engine.context)
    print(f"  add(5, 3) = {result}")

    multiply_tool = engine._tools_by_name["multiply"]
    result = multiply_tool.handler({"a": 4, "b": 7}, engine.context)
    print(f"  multiply(4, 7) = {result}")

    power_tool = engine._tools_by_name["power"]
    result = power_tool.handler({"base": 2, "exponent": 10}, engine.context)
    print(f"  power(2, 10) = {result}")

    # Cleanup
    print("\n🧹 Cleaning up resources...")
    engine.cleanup()

    print("\n✨ Demo complete!")


if __name__ == "__main__":
    asyncio.run(main())
