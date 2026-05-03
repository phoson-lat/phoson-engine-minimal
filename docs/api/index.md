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
from phoson_agent import AgentEngine, tool
from phoson_llm import build_chat, ModelConfig

@tool
def get_weather(location: str) -> str:
    """Get weather for a location."""
    return f"Weather in {location}: Sunny"

agent = AgentEngine(chat=build_chat("openai", api_key="sk-..."))
result = await agent.run(
    messages=[{"role": "user", "content": "What's the weather in NYC?"}],
    tools=[get_weather],
)
print(result.final_content)
```