#!/usr/bin/env python3
"""
Ejemplo de uso exactamente como lo solicitaste.
"""

import asyncio
from phoson_agent import AgentEngine
from phoson_llm import OpenAIChat, ModelConfig, Message


async def main():
    """
    Ejemplo del uso exacto que solicitaste:
    
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
    print("🔌 Phoson Agent - Uso del Sistema de Plugins")
    print("=" * 70)
    
    # Nota: Como los plugins reales aún no existen, usaremos el ejemplo local
    # En producción, esto funcionaría con plugins instalados vía pip
    
    print("\n📦 Ejemplo 1: Plugins como strings (cuando estén publicados)")
    print("-" * 70)
    print("""
engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        "phoson-plugin-mcp",        # Se instalaría con: pip install phoson-plugin-mcp
        "phoson-plugin-memory",     # Se instalaría con: pip install phoson-plugin-memory
        "phoson-plugin-checkpoint", # Se instalaría con: pip install phoson-plugin-checkpoint
    ],
)
""")
    
    print("\n📦 Ejemplo 2: Con configuración personalizada")
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
    
    print("\n📦 Ejemplo 3: Mezclando diferentes formatos")
    print("-" * 70)
    print("""
from my_custom_plugin import MyPlugin

engine = AgentEngine(
    chat=OpenAIChat(),
    plugins=[
        "phoson-plugin-mcp",                    # Package instalado
        "path:./local_plugin.py",              # Plugin local
        MyPlugin(),                             # Instancia directa
        {
            "name": "phoson-plugin-memory",
            "config": {"max_memories": 50}
        },
    ],
)
""")
    
    print("\n🚀 Demo funcional con plugin local")
    print("-" * 70)
    
    # Demo real con el plugin de ejemplo
    engine = AgentEngine(
        chat=OpenAIChat(),
        plugins=[
            "path:./examples/plugin_example_memory.py",
        ],
    )
    
    print(f"✅ Engine creado con {len(engine._loaded_plugins)} plugin(s)")
    print(f"🔧 Tools disponibles: {[t.name for t in engine.tools]}")
    print(f"🔀 Middlewares activos: {len(engine.middlewares)}")
    
    # Probar las tools del plugin
    print("\n🧪 Probando tools del plugin de memoria:")
    
    store_tool = engine._tools_by_name["store_memory"]
    result = store_tool.handler(
        {"key": "user_name", "value": "Alice"},
        engine.context
    )
    print(f"  → store_memory('user_name', 'Alice'): {result}")
    
    retrieve_tool = engine._tools_by_name["retrieve_memory"]
    result = retrieve_tool.handler(
        {"key": "user_name"},
        engine.context
    )
    print(f"  → retrieve_memory('user_name'): {result}")
    
    list_tool = engine._tools_by_name["list_memories"]
    result = list_tool.handler({}, engine.context)
    print(f"  → list_memories(): {result}")
    
    # Cleanup
    print("\n🧹 Limpiando recursos...")
    engine.cleanup()
    
    print("\n✨ Demo completado!")
    print("\n" + "=" * 70)
    print("💡 Próximos pasos:")
    print("   1. Implementar plugins reales (phoson-plugin-mcp, etc)")
    print("   2. Publicarlos en PyPI")
    print("   3. Instalar con: pip install phoson-plugin-<name>")
    print("   4. Usar como en el Ejemplo 1")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
