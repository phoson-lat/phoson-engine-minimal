#!/usr/bin/env python3
"""
Demo simple del sistema de plugins.
Muestra cómo crear y usar un plugin inline.
"""

import asyncio
from phoson_agent import AgentEngine, Plugin, AgentTool, tool
from phoson_llm import ModelConfig, Message


# Definir un plugin simple inline
class CalculatorPlugin(Plugin):
    """Plugin que proporciona operaciones matemáticas."""
    
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
        """Proporciona herramientas matemáticas."""
        
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
            return base ** exponent
        
        return [add, multiply, power]


async def main():
    """Demo principal."""
    print("=" * 60)
    print("🔌 Phoson Agent - Plugin System Demo")
    print("=" * 60)
    
    # Crear engine con el plugin
    print("\n📦 Cargando plugin de calculadora...")
    engine = AgentEngine(
        chat=None,  # No necesitamos LLM para este demo
        plugins=[
            CalculatorPlugin(),  # Plugin inline
        ],
    )
    
    # Verificar que las tools se cargaron
    print(f"✅ Plugin cargado: {engine._loaded_plugins[0].name}")
    print(f"🔧 Tools disponibles: {[t.name for t in engine.tools]}")
    
    # Mostrar info de las tools
    print("\n📋 Información de las tools:")
    for tool_obj in engine.tools:
        print(f"  • {tool_obj.name}: {tool_obj.description}")
    
    # Probar las tools directamente
    print("\n🧪 Probando las tools:")
    
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
    print("\n🧹 Limpiando recursos...")
    engine.cleanup()
    
    print("\n✨ Demo completado!")


if __name__ == "__main__":
    asyncio.run(main())
