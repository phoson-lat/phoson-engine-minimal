from collections.abc import AsyncIterator

import httpx
import pytest

from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
)
from phoson_agent.models import (
    AgentTool,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.ollama import OllamaChat
from phoson_llm.chats.openai import OpenAIChat
from phoson_llm.chats.anthropic import AnthropicChat
from phoson_llm.chats.openrouter import OpenRouterChat


class _Delta:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _ToolFunction:
    def __init__(self, name=None, arguments=None) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, call_id, name, arguments) -> None:
        self.index = index
        self.id = call_id
        self.function = _ToolFunction(name=name, arguments=arguments)


class _Choice:
    def __init__(self, delta=None, finish_reason=None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, delta=None, finish_reason=None, usage=None) -> None:
        self.choices = [
            _Choice(
                delta=delta if delta is not None else _Delta(),
                finish_reason=finish_reason,
            )
        ]
        self.usage = usage


class _FakeStream:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def _iterator():
            for chunk in self._chunks:
                yield chunk

        return _iterator()


def _extract_agent_event_types(events):
    return [type(event) for event in events]


class FakeToolChat(BaseLLMChat):
    def __init__(self) -> None:
        self._iteration = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self._iteration += 1

        yield LLMStartEvent(model=config.model, message_count=len(messages))

        if self._iteration == 1:
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_fake_weather_1",
                tool_name="get_weather",
                args={"city": "Queretaro", "country": "MX"},
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=120, output=28),
                cost_usd=0.00042,
                cost_known=True,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return

        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=180, output=46),
            cost_usd=0.00067,
            cost_known=True,
        )
        yield LLMDoneEvent(
            content="En Queretaro esta soleado, 27C, humedad moderada.",
            has_tool_calls=False,
        )


class FakeErrorChat(BaseLLMChat):
    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        yield ErrorEvent(message="provider timeout", code="timeout", retryable=True)


def get_weather(args: dict, context: object = None) -> dict:
    city = args.get("city", "unknown")
    country = args.get("country", "unknown")
    return {
        "city": city,
        "country": country,
        "condition": "sunny",
        "temperature_c": 27,
        "humidity": "moderate",
    }


def build_tools() -> list[AgentTool]:
    return [
        AgentTool(
            name="get_weather",
            description="Returns current weather by city and country.",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "country": {"type": "string"},
                },
                "required": ["city"],
            },
            handler=get_weather,
        )
    ]


@tool(inject=["safe_mode"])
def run_shell(command: str, safe_mode: bool = False) -> str:
    return f"cmd={command} safe={safe_mode}"


@pytest.mark.asyncio
async def test_run_integration_fake_tool_chat() -> None:
    engine = AgentEngine(chat=FakeToolChat(), tools=build_tools(), phoson_weight=1.2)
    result = await engine.run(
        messages=[Message(role="user", content="Que clima hace en Queretaro?")],
        config=ModelConfig(model="fake-demo-model", max_tokens=256),
    )

    assert "Queretaro" in result.final_content
    assert result.total_cost_usd == pytest.approx(0.00109, abs=1e-9)
    assert result.total_credits == pytest.approx(0.001308, abs=1e-9)
    assert [step.kind for step in result.steps] == ["llm", "tool", "llm"]
    assert result.steps[1].tool_name == "get_weather"
    assert result.steps[1].error is None
    assert len(result.history) == 4


@pytest.mark.asyncio
async def test_stream_integration_emits_tool_and_done_events() -> None:
    engine = AgentEngine(chat=FakeToolChat(), tools=build_tools(), phoson_weight=1.2)
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="Que clima hace en Queretaro?")],
            config=ModelConfig(model="fake-demo-model", max_tokens=256),
        )
    ]

    assert any(isinstance(event, AgentToolStartEvent) for event in events)
    assert any(isinstance(event, AgentToolDoneEvent) for event in events)
    assert any(isinstance(event, AgentStepDoneEvent) for event in events)

    done_events = [event for event in events if isinstance(event, AgentDoneEvent)]
    assert len(done_events) == 1
    assert "Queretaro" in done_events[0].result.final_content


@pytest.mark.asyncio
async def test_run_raises_when_llm_emits_error() -> None:
    from phoson_agent.exceptions import PhosonAgentError

    engine = AgentEngine(chat=FakeErrorChat(), tools=build_tools(), phoson_weight=1.2)

    with pytest.raises(
        PhosonAgentError, match=r"Agent error \(timeout\): provider timeout"
    ):
        await engine.run(
            messages=[Message(role="user", content="test")],
            config=ModelConfig(model="fake-demo-model", max_tokens=128),
        )


@pytest.mark.asyncio
async def test_stream_emits_agent_error_when_llm_fails() -> None:
    engine = AgentEngine(chat=FakeErrorChat(), tools=build_tools(), phoson_weight=1.2)
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="test")],
            config=ModelConfig(model="fake-demo-model", max_tokens=128),
        )
    ]

    error_events = [event for event in events if isinstance(event, AgentErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].code == "timeout"
    assert error_events[0].retryable is True


@pytest.mark.asyncio
async def test_run_executes_decorated_tool_with_context_injection() -> None:
    class FakeInjectedToolChat(BaseLLMChat):
        async def stream(
            self,
            messages: list[Message],
            config: ModelConfig,
            tools: list[ToolDefinition] | None = None,
        ) -> AsyncIterator[LLMEvent]:
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_shell_1",
                tool_name="run_shell",
                args={"command": "git log -1 --oneline"},
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)

    engine = AgentEngine(chat=FakeInjectedToolChat(), tools=[run_shell])
    engine.context.extra["safe_mode"] = True

    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="revisa cambios")],
            config=ModelConfig(model="fake-demo-model", max_tokens=128),
        )
    ]

    tool_done = next(event for event in events if isinstance(event, AgentToolDoneEvent))
    assert tool_done.result == "cmd=git log -1 --oneline safe=True"


@pytest.mark.asyncio
async def test_openai_adapter_integration_tool_loop(monkeypatch) -> None:
    call_count = 0

    async def _fake_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _FakeStream(
                [
                    _Chunk(
                        delta=_Delta(
                            tool_calls=[
                                _ToolCall(
                                    index=0,
                                    call_id="call_openai_1",
                                    name="get_weather",
                                    arguments='{"city":"Qro"}',
                                )
                            ]
                        )
                    ),
                    _Chunk(finish_reason="tool_calls"),
                    _Chunk(delta=_Delta(content="")),
                ]
            )
        return _FakeStream(
            [
                _Chunk(delta=_Delta(content="Listo")),
            ]
        )

    chat = OpenAIChat(api_key="test")
    monkeypatch.setattr(chat._client.chat.completions, "create", _fake_create)

    engine = AgentEngine(chat=chat, tools=build_tools())
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="clima")],
            config=ModelConfig(model="gpt-4o-mini", max_tokens=128),
        )
    ]

    event_types = _extract_agent_event_types(events)
    assert event_types[0] is AgentStartEvent
    assert AgentToolStartEvent in event_types
    assert AgentToolDoneEvent in event_types
    assert AgentStepDoneEvent in event_types
    assert event_types[-1] is AgentDoneEvent
    done = next(event for event in events if isinstance(event, AgentDoneEvent))
    tool_done = next(event for event in events if isinstance(event, AgentToolDoneEvent))
    assert tool_done.tool_name == "get_weather"
    assert tool_done.tool_call_id == "call_openai_1"
    assert done.result.steps[0].kind == "llm"
    assert done.result.steps[1].kind == "tool"
    assert done.result.steps[2].kind == "llm"
    assert done.result.final_content == "Listo"


@pytest.mark.asyncio
async def test_openrouter_adapter_integration_tool_loop(monkeypatch) -> None:
    call_count = 0

    async def _fake_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _FakeStream(
                [
                    _Chunk(
                        delta=_Delta(
                            tool_calls=[
                                _ToolCall(
                                    index=0,
                                    call_id="call_openrouter_1",
                                    name="get_weather",
                                    arguments='{"city":"Qro"}',
                                )
                            ]
                        )
                    ),
                    _Chunk(finish_reason="tool_calls"),
                    _Chunk(delta=_Delta(content="")),
                ]
            )
        return _FakeStream(
            [
                _Chunk(delta=_Delta(content="Ok")),
            ]
        )

    chat = OpenRouterChat(api_key="test")
    monkeypatch.setattr(chat._client.chat.completions, "create", _fake_create)

    engine = AgentEngine(chat=chat, tools=build_tools())
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="clima")],
            config=ModelConfig(model="openrouter/test", max_tokens=128),
        )
    ]

    event_types = _extract_agent_event_types(events)
    assert event_types[0] is AgentStartEvent
    assert AgentToolStartEvent in event_types
    assert AgentToolDoneEvent in event_types
    assert AgentStepDoneEvent in event_types
    assert event_types[-1] is AgentDoneEvent
    done = next(event for event in events if isinstance(event, AgentDoneEvent))
    tool_done = next(event for event in events if isinstance(event, AgentToolDoneEvent))
    assert tool_done.tool_name == "get_weather"
    assert tool_done.tool_call_id == "call_openrouter_1"
    assert done.result.final_content == "Ok"
    assert [step.kind for step in done.result.steps] == ["llm", "tool", "llm"]


@pytest.mark.asyncio
async def test_anthropic_adapter_integration_tool_loop(monkeypatch) -> None:
    class _Delta:
        def __init__(self, delta_type, text=None, thinking=None, partial_json=None):
            self.type = delta_type
            self.text = text
            self.thinking = thinking
            self.partial_json = partial_json

    class _Event:
        def __init__(self, etype, index=0, delta=None, content_block=None):
            self.type = etype
            self.index = index
            self.delta = delta
            self.content_block = content_block

    class _ToolBlock:
        def __init__(self, name, tool_id):
            self.type = "tool_use"
            self.name = name
            self.id = tool_id

    class _Usage:
        def __init__(self):
            self.input_tokens = 12
            self.output_tokens = 4
            self.cache_creation_input_tokens = 0
            self.cache_read_input_tokens = 0

    class _FinalMessage:
        def __init__(self):
            self.usage = _Usage()

    class _Stream:
        def __init__(self, events):
            self._events = events

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def __aiter__(self):
            async def _iter():
                for item in self._events:
                    yield item

            return _iter()

        async def get_final_message(self):
            return _FinalMessage()

    events = [
        _Event(
            "content_block_start",
            index=0,
            content_block=_ToolBlock("get_weather", "call_anthropic_1"),
        ),
        _Event(
            "content_block_delta",
            index=0,
            delta=_Delta("input_json_delta", partial_json='{"city":"Qro"}'),
        ),
        _Event("content_block_stop", index=0),
        _Event("content_block_delta", index=1, delta=_Delta("text_delta", text="")),
    ]
    final_events = [
        _Event(
            "content_block_delta", index=0, delta=_Delta("text_delta", text="Listo")
        ),
    ]

    call_count = 0

    def _make_stream(**kwargs):
        nonlocal call_count
        call_count += 1
        return _Stream(events if call_count == 1 else final_events)

    chat = AnthropicChat(api_key="test")
    monkeypatch.setattr(chat._client.messages, "stream", _make_stream)

    engine = AgentEngine(chat=chat, tools=build_tools())
    events_out = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="clima")],
            config=ModelConfig(model="claude-3-haiku", max_tokens=128),
        )
    ]

    event_types = _extract_agent_event_types(events_out)
    assert event_types[0] is AgentStartEvent
    assert AgentToolStartEvent in event_types
    assert AgentToolDoneEvent in event_types
    assert AgentStepDoneEvent in event_types
    assert event_types[-1] is AgentDoneEvent
    done = next(event for event in events_out if isinstance(event, AgentDoneEvent))
    tool_done = next(
        event for event in events_out if isinstance(event, AgentToolDoneEvent)
    )
    assert tool_done.tool_name == "get_weather"
    assert tool_done.tool_call_id == "call_anthropic_1"
    assert done.result.final_content == "Listo"
    assert [step.kind for step in done.result.steps] == ["llm", "tool", "llm"]


@pytest.mark.asyncio
async def test_ollama_adapter_integration_tool_loop(monkeypatch) -> None:
    call_count = 0

    class _Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_lines(self):
            nonlocal call_count
            if call_count == 1:
                yield (
                    '{"message":{"type":"message","content":"","tool_calls":'
                    '[{"index":0,"id":"call_ollama_1","function":'
                    '{"name":"get_weather",'
                    '"arguments":"{\\"city\\":\\"Qro\\"}"}}]}}'
                )
                yield (
                    '{"message":{"type":"done"},"eval_count":5,"prompt_eval_count":10}'
                )
            else:
                yield '{"message":{"type":"message","content":"Listo"},"done":true}'

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, json):
            return _Response()

    def make_client(timeout=None):
        nonlocal call_count
        call_count += 1
        return _Client()

    monkeypatch.setattr(httpx, "AsyncClient", make_client)

    chat = OllamaChat(base_url="http://ollama")
    engine = AgentEngine(chat=chat, tools=build_tools())
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="clima")],
            config=ModelConfig(model="llama3", max_tokens=128),
        )
    ]

    event_types = _extract_agent_event_types(events)
    assert event_types[0] is AgentStartEvent
    assert AgentToolStartEvent in event_types
    assert AgentToolDoneEvent in event_types
    assert AgentStepDoneEvent in event_types
    assert event_types[-1] is AgentDoneEvent
    done = next(event for event in events if isinstance(event, AgentDoneEvent))
    tool_done = next(event for event in events if isinstance(event, AgentToolDoneEvent))
    assert tool_done.tool_name == "get_weather"
    assert tool_done.tool_call_id == "call_ollama_1"
    assert [step.kind for step in done.result.steps] == ["llm", "tool", "llm"]


@pytest.mark.asyncio
async def test_tool_handler_error_sets_tool_done_error(monkeypatch) -> None:
    class FakeToolErrorChat(BaseLLMChat):
        async def stream(self, messages, config, tools=None):
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_tool_error_1",
                tool_name="get_weather",
                args={"city": "Qro"},
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)

    def boom(args, context=None):
        raise RuntimeError("boom")

    tools = [
        AgentTool(
            name="get_weather",
            description="",
            parameters={"type": "object", "properties": {}},
            handler=boom,
        )
    ]

    engine = AgentEngine(chat=FakeToolErrorChat(), tools=tools)
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="clima")],
            config=ModelConfig(model="fake", max_tokens=64),
        )
    ]

    tool_done = next(event for event in events if isinstance(event, AgentToolDoneEvent))
    step_done = next(
        event
        for event in events
        if isinstance(event, AgentStepDoneEvent) and event.step.kind == "tool"
    )
    assert tool_done.error == "boom"
    assert step_done.step.error == "boom"


@pytest.mark.asyncio
async def test_empty_done_content_returns_empty_final_content() -> None:
    class FakeEmptyDoneChat(BaseLLMChat):
        async def stream(self, messages, config, tools=None):
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield LLMDoneEvent(content="", has_tool_calls=False)

    engine = AgentEngine(chat=FakeEmptyDoneChat(), tools=[])
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="hola")],
            config=ModelConfig(model="fake", max_tokens=16),
        )
    ]

    done = next(event for event in events if isinstance(event, AgentDoneEvent))
    assert done.result.final_content == ""


@pytest.mark.asyncio
async def test_max_iterations_emits_error_event() -> None:
    class FakeLoopingChat(BaseLLMChat):
        async def stream(self, messages, config, tools=None):
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_loop_1",
                tool_name="get_weather",
                args={"city": "Qro"},
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)

    engine = AgentEngine(chat=FakeLoopingChat(), tools=build_tools(), max_iterations=2)
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="loop")],
            config=ModelConfig(model="fake", max_tokens=32),
        )
    ]

    error_event = next(event for event in events if isinstance(event, AgentErrorEvent))
    assert error_event.code == "max_iterations"


@pytest.mark.asyncio
async def test_run_raises_phoson_max_iterations_error() -> None:
    """``engine.run()`` must surface max_iterations as a typed exception."""
    from phoson_agent.exceptions import PhosonMaxIterationsError

    class FakeLoopingChat(BaseLLMChat):
        async def stream(self, messages, config, tools=None):
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_loop_1",
                tool_name="get_weather",
                args={"city": "Qro"},
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)

    engine = AgentEngine(chat=FakeLoopingChat(), tools=build_tools(), max_iterations=3)

    with pytest.raises(PhosonMaxIterationsError) as exc_info:
        await engine.run(
            messages=[Message(role="user", content="loop")],
            config=ModelConfig(model="fake", max_tokens=32),
        )

    assert exc_info.value.max_iterations == 3


@pytest.mark.asyncio
async def test_llm_protocol_error_missing_tool_calls() -> None:
    class FakeMissingToolCallsChat(BaseLLMChat):
        async def stream(self, messages, config, tools=None):
            yield LLMStartEvent(model=config.model, message_count=len(messages))
            yield LLMDoneEvent(content="", has_tool_calls=True)

    engine = AgentEngine(chat=FakeMissingToolCallsChat(), tools=build_tools())
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="hola")],
            config=ModelConfig(model="fake", max_tokens=32),
        )
    ]

    error_event = next(event for event in events if isinstance(event, AgentErrorEvent))
    assert error_event.code == "llm_protocol"


@pytest.mark.asyncio
async def test_llm_protocol_error_missing_done_event() -> None:
    class FakeMissingDoneChat(BaseLLMChat):
        async def stream(self, messages, config, tools=None):
            yield LLMStartEvent(model=config.model, message_count=len(messages))

    engine = AgentEngine(chat=FakeMissingDoneChat(), tools=[])
    events = [
        event
        async for event in engine.stream(
            messages=[Message(role="user", content="hola")],
            config=ModelConfig(model="fake", max_tokens=32),
        )
    ]

    error_event = next(event for event in events if isinstance(event, AgentErrorEvent))
    assert error_event.code == "llm_protocol"
