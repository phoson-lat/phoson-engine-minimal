# API Reference

Complete reference documentation for the phoson-engine-minimal package.

## Modules

### Core

- [phoson_llm](./phoson_llm.md) — LLM Normalization Layer
- [phoson_agent](./phoson_agent.md) — Agent Orchestration
- [phoson_cli](./phoson_cli.md) — Interactive REPL

## Installation

```bash
pip install phoson-engine-minimal
```

## Quick Start

```python
import asyncio

from phoson_agent import AgentEngine, tool
from phoson_llm import Message, ModelConfig

@tool
def get_weather(location: str) -> str:
    """Get weather for a location."""
    return f"Weather in {location}: Sunny"

async def main() -> None:
    agent = AgentEngine(
        chat=ModelConfig(provider="openai", api_key="sk-..."),
        tools=[get_weather],
    )
    result = await agent.run(
        messages=[Message(role="user", content="What's the weather in NYC?")],
    )
    print(result.final_content)

asyncio.run(main())
```