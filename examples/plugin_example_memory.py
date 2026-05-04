"""
Example plugin: Memory Plugin
Provides persistent memory across agent runs.
"""

from typing import Any
from phoson_agent import Plugin, AgentTool, AgentMiddleware, tool
from phoson_llm.schemas import Message, ModelConfig


class MemoryPlugin(Plugin):
    """
    Plugin that provides memory capabilities to the agent.
    Stores and retrieves information across conversations.
    """

    def __init__(self) -> None:
        self._memory_store: dict[str, Any] = {}
        self._max_memories: int = 100

    @property
    def name(self) -> str:
        return "phoson-plugin-memory"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Provides persistent memory storage for the agent"

    def configure(self, config: dict[str, Any]) -> None:
        """Configure the memory plugin."""
        self._max_memories = config.get("max_memories", 100)

    def get_tools(self) -> list[AgentTool]:
        """Provide memory tools to the agent."""
        
        @tool
        def store_memory(key: str, value: str) -> str:
            """
            Store information in memory for later retrieval.
            
            Args:
                key: Unique identifier for the memory
                value: Information to store
            """
            if len(self._memory_store) >= self._max_memories:
                # Remove oldest entry
                oldest_key = next(iter(self._memory_store))
                del self._memory_store[oldest_key]
            
            self._memory_store[key] = value
            return f"Stored memory '{key}'"

        @tool
        def retrieve_memory(key: str) -> str:
            """
            Retrieve information from memory.
            
            Args:
                key: Unique identifier for the memory
            """
            value = self._memory_store.get(key)
            if value is None:
                return f"No memory found for key '{key}'"
            return str(value)

        @tool
        def list_memories() -> dict[str, list[str]]:
            """List all stored memory keys."""
            return {"keys": list(self._memory_store.keys())}

        return [store_memory, retrieve_memory, list_memories]

    def get_middlewares(self) -> list[AgentMiddleware]:
        """Provide memory middleware."""
        
        class MemoryMiddleware(AgentMiddleware):
            """Middleware to inject memory context into messages."""
            
            def __init__(self, memory_store: dict[str, Any]):
                self.memory_store = memory_store
            
            async def on_before_llm(
                self,
                messages: list[Message],
                config: ModelConfig,
            ) -> list[Message]:
                """Inject memory summary if available."""
                if not self.memory_store:
                    return messages
                
                # Add a system message with memory context
                memory_summary = "\n".join(
                    f"- {k}: {v}" for k, v in list(self.memory_store.items())[:5]
                )
                
                memory_msg = Message(
                    role="system",
                    content=f"Available memories:\n{memory_summary}",
                )
                
                # Insert after the first message (usually system prompt)
                if len(messages) > 0:
                    return [messages[0], memory_msg] + messages[1:]
                return [memory_msg] + messages
        
        return [MemoryMiddleware(self._memory_store)]

    def cleanup(self) -> None:
        """Cleanup memory resources."""
        # Could save to disk here
        self._memory_store.clear()


# Export plugin instance
plugin = MemoryPlugin()
