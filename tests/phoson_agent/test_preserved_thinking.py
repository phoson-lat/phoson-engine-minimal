"""Tests for #134 — Preserved thinking.

The engine used to discard ``reasoning_content`` / Anthropic ``thinking``
blocks after streaming them to the UI. That breaks multi-turn reasoning
models:

* **Qwen3.8 / vLLM / LM Studio** (``preserve_thinking=True``) need
  ``reasoning_content`` re-sent on historical assistant messages.
* **Anthropic extended thinking + tool use** requires the signed
  ``thinking`` blocks to be returned on every subsequent turn.

These tests lock the acceptance criteria:

1. *OpenAI-compat round-trip* — an assistant message carrying ``reasoning``
   serializes to a dict with ``reasoning_content`` (capped when long) and
   deserializes back with the reasoning intact.
2. *Anthropic round-trip* — an assistant message with ``reasoning`` +
   ``signature`` serializes to a leading ``thinking`` block and back.
3. *Loop integration* — a faked stream that emits ``ReasoningDoneEvent``
   yields an assistant message in the history that carries the reasoning, and
   the *next* LLM call receives it.
4. *Tool use + reasoning* — the assistant message keeps both the tool_use
   block and the reasoning.
5. *Truncation* — a 20K-char reasoning is capped to 10K + marker on the wire.
6. *``preserve_thinking=False``* — no ``reasoning_content`` / thinking block.
7. *``preserve_thinking=None`` + no signature (Anthropic)* — graceful drop.
"""

from dataclasses import dataclass
from collections.abc import AsyncIterator

import pytest

from phoson_agent.tool import tool
from phoson_agent.agent import AgentEngine
from phoson_llm.schemas import (
    REASONING_MAX_CHARS,
    REASONING_TRUNCATION_MARKER,
    Message,
    LLMEvent,
    TextBlock,
    TokenEvent,
    TokenUsage,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ReasoningDoneEvent,
    cap_reasoning,
)
from phoson_agent.models import AgentTool
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.chats.anthropic import (
    _thinking_block,
    _extract_thinking_signature,
)
from phoson_llm.chats.anthropic import (
    _convert_messages as _anthropic_convert,
)
from phoson_agent.sessions.serialization import message_to_dict, message_from_dict
from phoson_llm.chats._openai_compatible import (
    _convert_messages as _openai_convert,
)
from phoson_llm.chats._openai_compatible import (
    _build_request_kwargs,
)

# ─── Fake Anthropic SDK objects (shared) ─────────────────────────────────────


@dataclass
class _FakeBlock:
    type: str
    signature: str | None = None
    thinking: str | None = None
    text: str | None = None


@dataclass
class _FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeFinalMessage:
    usage: _FakeUsage | None = None
    content: list = None  # type: ignore[assignment]
    stop_reason: str | None = "end_turn"

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = _FakeUsage()
        if self.content is None:
            self.content = []


# ─── OpenAI-compatible round-trip ────────────────────────────────────────────


def test_openai_reasoning_round_trip() -> None:
    """assistant + reasoning → dict has ``reasoning_content`` → back to Message."""
    msg = Message(
        role="assistant",
        content="I think it is 42",
        reasoning="let me work it out",
    )
    out = _openai_convert([msg])
    assert out[0]["reasoning_content"] == "let me work it out"

    # Deserialize back: the reasoning text is preserved verbatim.
    restored = message_from_dict(message_to_dict(msg))
    assert restored.reasoning == "let me work it out"


def test_openai_reasoning_on_tool_use_message() -> None:
    """An assistant turn that also issued a tool call still carries reasoning."""
    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="let me check"),
            ToolUseBlock(tool_call_id="call_1", tool_name="calc", args={"x": 1}),
        ],
        reasoning="need to compute",
    )
    out = _openai_convert([msg])
    assistant = out[0]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["function"]["name"] == "calc"
    assert assistant["reasoning_content"] == "need to compute"


def test_openai_no_reasoning_field_when_absent() -> None:
    """A message without reasoning must not gain a ``reasoning_content`` key."""
    out = _openai_convert([Message(role="assistant", content="plain")])
    assert "reasoning_content" not in out[0]


def test_openai_user_messages_never_carry_reasoning() -> None:
    """Only assistant turns are re-sent; a user turn with stray reasoning is
    ignored (the field is an assistant-only convention)."""
    out = _openai_convert(
        [Message(role="user", content="hi", reasoning="should not appear")]
    )
    assert "reasoning_content" not in out[0]


# ─── OpenAI-compatible truncation ────────────────────────────────────────────


def test_openai_reasoning_truncated_on_the_wire() -> None:
    """A 20K-char reasoning is capped to 10K + marker in the request body,
    while the in-memory Message keeps the full text."""
    long_reasoning = "x" * (REASONING_MAX_CHARS * 2)
    msg = Message(role="assistant", content="done", reasoning=long_reasoning)

    out = _openai_convert([msg])
    sent = out[0]["reasoning_content"]
    assert sent == long_reasoning[:REASONING_MAX_CHARS] + REASONING_TRUNCATION_MARKER
    assert len(sent) == REASONING_MAX_CHARS + len(REASONING_TRUNCATION_MARKER)

    # The Message itself is untouched — the cap is applied at serialization.
    assert msg.reasoning == long_reasoning
    # And the session round-trip preserves the full reasoning.
    restored = message_from_dict(message_to_dict(msg))
    assert restored.reasoning == long_reasoning


def test_cap_reasoning_boundary() -> None:
    """Exactly at the cap the text is returned unchanged; one over is capped."""
    at_cap = "y" * REASONING_MAX_CHARS
    assert cap_reasoning(at_cap) == at_cap
    over = "y" * (REASONING_MAX_CHARS + 1)
    assert cap_reasoning(over) == at_cap + REASONING_TRUNCATION_MARKER


# ─── preserve_thinking policy (OpenAI) ───────────────────────────────────────


def test_openai_preserve_thinking_false_omits_reasoning() -> None:
    """``preserve_thinking=False`` never emits ``reasoning_content``."""
    msg = Message(role="assistant", content="done", reasoning="secret thoughts")
    out = _openai_convert([msg], preserve_thinking=False)
    assert "reasoning_content" not in out[0]


def test_openai_preserve_thinking_true_emits_reasoning() -> None:
    """``preserve_thinking=True`` forces emission (adapter supports it)."""
    msg = Message(role="assistant", content="done", reasoning="thoughts")
    out = _openai_convert([msg], preserve_thinking=True)
    assert out[0]["reasoning_content"] == "thoughts"


def test_openai_preserve_thinking_none_default_emits() -> None:
    """``preserve_thinking=None`` (adapter decides) emits for OpenAI-compat."""
    msg = Message(role="assistant", content="done", reasoning="thoughts")
    out = _openai_convert([msg], preserve_thinking=None)
    assert out[0]["reasoning_content"] == "thoughts"


def test_build_request_kwargs_threads_preserve_thinking() -> None:
    """The config's tri-state is threaded through to the message conversion."""
    msg = Message(role="assistant", content="done", reasoning="thoughts")

    cfg_off = ModelConfig(model="m", max_tokens=16, preserve_thinking=False)
    kwargs_off = _build_request_kwargs(
        config=cfg_off, messages=[msg], tools=None, max_tokens_key="max_tokens"
    )
    assert "reasoning_content" not in kwargs_off["messages"][0]

    cfg_on = ModelConfig(model="m", max_tokens=16, preserve_thinking=True)
    kwargs_on = _build_request_kwargs(
        config=cfg_on, messages=[msg], tools=None, max_tokens_key="max_tokens"
    )
    assert kwargs_on["messages"][0]["reasoning_content"] == "thoughts"


# ─── Anthropic round-trip ────────────────────────────────────────────────────


def test_anthropic_thinking_round_trip() -> None:
    """assistant + reasoning + signature → leading thinking block → back."""
    msg = Message(
        role="assistant",
        content=[TextBlock(text="the answer is 42")],
        reasoning="working it out",
        reasoning_signature="sig-abc",
    )
    out = _anthropic_convert([msg])
    blocks = out[0]["content"]
    # The thinking block must lead the assistant turn.
    assert blocks[0] == {
        "type": "thinking",
        "thinking": "working it out",
        "signature": "sig-abc",
    }
    assert blocks[1] == {"type": "text", "text": "the answer is 42"}

    # Session round-trip preserves both fields.
    restored = message_from_dict(message_to_dict(msg))
    assert restored.reasoning == "working it out"
    assert restored.reasoning_signature == "sig-abc"


def test_anthropic_thinking_block_leads_tool_use() -> None:
    """With tool use, the thinking block still comes first (Anthropic order)."""
    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="let me check"),
            ToolUseBlock(tool_call_id="t1", tool_name="calc", args={"x": 1}),
        ],
        reasoning="need to compute",
        reasoning_signature="sig-1",
    )
    out = _anthropic_convert([msg])
    types = [b["type"] for b in out[0]["content"]]
    assert types == ["thinking", "text", "tool_use"]
    assert out[0]["content"][0]["signature"] == "sig-1"


def test_anthropic_missing_signature_drops_block() -> None:
    """Without a signature the thinking block is dropped (degradation, not an
    error) — the API would 400 on an unsigned block."""
    msg = Message(
        role="assistant",
        content=[TextBlock(text="hi")],
        reasoning="thinking but no signature",
    )
    out = _anthropic_convert([msg])
    types = [b["type"] for b in out[0]["content"]]
    assert "thinking" not in types
    assert types == ["text"]


def test_anthropic_preserve_thinking_false_omits_thinking() -> None:
    """``preserve_thinking=False`` suppresses the thinking block entirely."""
    msg = Message(
        role="assistant",
        content=[TextBlock(text="hi")],
        reasoning="thinking",
        reasoning_signature="sig",
    )
    out = _anthropic_convert([msg], preserve_thinking=False)
    types = [b["type"] for b in out[0]["content"]]
    assert "thinking" not in types


def test_anthropic_thinking_block_helper() -> None:
    """Direct unit of the block builder: role/preserve/signature gating."""
    assert (
        _thinking_block(
            Message(role="user", content="hi", reasoning="x", reasoning_signature="s"),
            None,
        )
        is None
    )
    assert (
        _thinking_block(Message(role="assistant", content="hi", reasoning="x"), None)
        is None
    )  # no signature
    assert (
        _thinking_block(
            Message(
                role="assistant", content="hi", reasoning="x", reasoning_signature="s"
            ),
            False,
        )
        is None
    )
    block = _thinking_block(
        Message(role="assistant", content="hi", reasoning="x", reasoning_signature="s"),
        None,
    )
    assert block == {"type": "thinking", "thinking": "x", "signature": "s"}


def test_extract_thinking_signature_from_final_message() -> None:
    """The signature is read from the final message's thinking block."""
    final = _FakeFinalMessage(
        content=[
            _FakeBlock("thinking", signature="sig-final", thinking="..."),
            _FakeBlock("text", text="hi"),
        ]
    )
    assert _extract_thinking_signature(final) == "sig-final"
    assert _extract_thinking_signature(_FakeFinalMessage(content=[])) is None
    assert (
        _extract_thinking_signature(
            _FakeFinalMessage(content=[_FakeBlock("text", text="hi")])
        )
        is None
    )


# ─── Loop integration (faked stream) ─────────────────────────────────────────


class _RecordingChat(BaseLLMChat):
    """A fake chat that records the messages it receives on every call and
    emits a scripted event stream per iteration.

    The first turn emits reasoning + a tool call; the second turn is the final
    answer. Recording the incoming messages lets us assert that the reasoning
    captured on turn 1 is re-sent to the model on turn 2.
    """

    def __init__(self, with_reasoning: bool = True) -> None:
        self._iteration = 0
        self.with_reasoning = with_reasoning
        self.received: list[list[Message]] = []

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,  # noqa: ARG002
    ) -> AsyncIterator[LLMEvent]:
        self._iteration += 1
        self.received.append(list(messages))
        yield LLMStartEvent(model=config.model, message_count=len(messages))
        if self._iteration == 1:
            if self.with_reasoning:
                yield ReasoningDoneEvent(content="turn one reasoning")
            yield ToolCallEvent(
                index=0,
                tool_call_id="call_pt_1",
                tool_name="get_weather",
                args={"city": "Qro"},
            )
            yield UsageEvent(
                model=config.model,
                usage=TokenUsage(input=10, output=5),
                cost_usd=0.0,
                cost_known=False,
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
            return
        if self.with_reasoning:
            yield ReasoningDoneEvent(content="turn two reasoning")
        yield TokenEvent(content="final answer")
        yield UsageEvent(
            model=config.model,
            usage=TokenUsage(input=20, output=10),
            cost_usd=0.0,
            cost_known=False,
        )
        yield LLMDoneEvent(content="final answer", has_tool_calls=False)


def _weather_tool(counter: list[str]) -> AgentTool:
    @tool
    def get_weather(city: str) -> dict:  # noqa: D103
        counter.append(city)
        return {"city": city, "condition": "sunny", "temperature_c": 27}

    return get_weather


@pytest.mark.asyncio
async def test_loop_captures_reasoning_and_resends() -> None:
    """A stream that emits ``ReasoningDoneEvent`` yields an assistant message
    in the history that carries the reasoning, and the next LLM call receives
    it."""
    counter: list[str] = []
    chat = _RecordingChat(with_reasoning=True)
    engine = AgentEngine(chat=chat, tools=[_weather_tool(counter)], max_iterations=3)

    result = await engine.run(
        messages=[Message(role="user", content="clima")],
        config=ModelConfig(model="fake", max_tokens=64),
    )

    assert result.final_content == "final answer"

    # The assistant message appended after turn 1 carries the reasoning.
    assistant_msgs = [m for m in result.history if m.role == "assistant"]
    assert len(assistant_msgs) == 2
    assert assistant_msgs[0].reasoning == "turn one reasoning"
    assert assistant_msgs[1].reasoning == "turn two reasoning"

    # The tool_use block survived alongside the reasoning (tool use + reasoning).
    assert any(isinstance(b, ToolUseBlock) for b in assistant_msgs[0].content), (
        "tool_use block must be preserved with the reasoning"
    )

    # The second LLM call received the turn-1 assistant message WITH reasoning.
    second_call_msgs = chat.received[1]
    second_assistant = [m for m in second_call_msgs if m.role == "assistant"]
    assert second_assistant, "turn-1 assistant message must be in the next call"
    assert second_assistant[0].reasoning == "turn one reasoning"


@pytest.mark.asyncio
async def test_loop_no_reasoning_when_stream_emits_none() -> None:
    """A stream without ``ReasoningDoneEvent`` leaves reasoning ``None``."""
    counter: list[str] = []
    chat = _RecordingChat(with_reasoning=False)
    engine = AgentEngine(chat=chat, tools=[_weather_tool(counter)], max_iterations=3)

    result = await engine.run(
        messages=[Message(role="user", content="clima")],
        config=ModelConfig(model="fake", max_tokens=64),
    )

    assistant_msgs = [m for m in result.history if m.role == "assistant"]
    assert all(m.reasoning is None for m in assistant_msgs)


# ─── Anthropic adapter: signature capture end-to-end (faked stream) ──────────


class _FakeAnthropicEvents:
    """Async-iterable of a stream whose deltas mirror the final message's
    blocks (a thinking block yields a ``thinking_delta``, a text block yields
    a ``text_delta``) + the final message itself."""

    def __init__(self, final: _FakeFinalMessage) -> None:
        self._final = final

    def __aiter__(self):
        return self._events().__aiter__()

    async def _events(self):
        for block in self._final.content:
            if block.type == "thinking":
                yield _DeltaEvent("thinking_delta", thinking=block.thinking or "")
            elif block.type == "text":
                yield _DeltaEvent("text_delta", text=block.text or "")

    async def get_final_message(self):
        return self._final


class _Delta:
    """Minimal stand-in for a stream ``delta`` payload."""

    def __init__(
        self, dtype: str, thinking: str | None = None, text: str | None = None
    ):
        self.type = dtype
        self.thinking = thinking
        self.text = text


class _DeltaEvent:
    """Minimal stand-in for a ``content_block_delta`` stream event."""

    def __init__(
        self, dtype: str, thinking: str | None = None, text: str | None = None
    ):
        self.type = "content_block_delta"
        self.index = 0
        self.delta = _Delta(dtype, thinking=thinking, text=text)


class _FakeStreamContext:
    def __init__(self, final: _FakeFinalMessage, captured: dict) -> None:
        self._final = final
        self.captured = captured

    def __call__(self, **kwargs):
        self.captured.clear()
        self.captured.update(kwargs)
        return self

    async def __aenter__(self):
        return _FakeAnthropicEvents(self._final)

    async def __aexit__(self, *exc):
        return False


class _FakeMessages:
    def __init__(self, final: _FakeFinalMessage, captured: dict) -> None:
        self.stream = _FakeStreamContext(final, captured)


class _FakeAnthropicClient:
    def __init__(self, final: _FakeFinalMessage, captured: dict) -> None:
        self.messages = _FakeMessages(final, captured)


@pytest.mark.asyncio
async def test_anthropic_captures_signature_and_resends() -> None:
    """The Anthropic adapter reads the signature from the final message and
    emits a ``ReasoningDoneEvent`` carrying it, so the loop can re-send the
    signed thinking block on the next turn."""
    from phoson_llm.chats.anthropic import AnthropicChat

    captured: dict = {}
    final = _FakeFinalMessage(
        content=[
            _FakeBlock("thinking", signature="sig-xyz", thinking="I am thinking"),
            _FakeBlock("text", text="the answer"),
        ]
    )
    chat = AnthropicChat(api_key="test-key")
    chat._client = _FakeAnthropicClient(final, captured)

    config = ModelConfig(
        model="anthropic/claude-sonnet-4-6", max_tokens=64, thinking_budget=1024
    )
    events = [
        e
        async for e in chat.stream(
            messages=[Message(role="user", content="hi")],
            config=config,
        )
    ]

    done = [e for e in events if isinstance(e, ReasoningDoneEvent)]
    assert len(done) == 1
    assert done[0].content == "I am thinking"
    assert done[0].signature == "sig-xyz"

    # The request carried the thinking budget.
    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 1024}


@pytest.mark.asyncio
async def test_anthropic_no_signature_when_no_thinking() -> None:
    """A stream with no thinking block emits no ``ReasoningDoneEvent``."""
    from phoson_llm.chats.anthropic import AnthropicChat

    captured: dict = {}
    final = _FakeFinalMessage(content=[_FakeBlock("text", text="plain")])
    chat = AnthropicChat(api_key="test-key")
    chat._client = _FakeAnthropicClient(final, captured)

    events = [
        e
        async for e in chat.stream(
            messages=[Message(role="user", content="hi")],
            config=ModelConfig(model="anthropic/claude-sonnet-4-6", max_tokens=64),
        )
    ]
    assert not any(isinstance(e, ReasoningDoneEvent) for e in events)


# ─── Session serialization round-trip ────────────────────────────────────────


def test_serialization_round_trip_preserves_reasoning() -> None:
    """reasoning + signature survive a dict round-trip; absent → omitted."""
    with_reasoning = Message(
        role="assistant",
        content=[TextBlock(text="hi")],
        reasoning="thoughts",
        reasoning_signature="sig",
    )
    d = message_to_dict(with_reasoning)
    assert d["reasoning"] == "thoughts"
    assert d["reasoning_signature"] == "sig"
    restored = message_from_dict(d)
    assert restored.reasoning == "thoughts"
    assert restored.reasoning_signature == "sig"

    # A message without reasoning must not gain the keys (back-compat).
    plain = Message(role="assistant", content="hi")
    d_plain = message_to_dict(plain)
    assert "reasoning" not in d_plain
    assert "reasoning_signature" not in d_plain
    assert message_from_dict(d_plain).reasoning is None


def test_serialization_legacy_dict_without_reasoning() -> None:
    """A legacy on-disk message dict (no reasoning keys) still deserializes."""
    legacy = {"role": "assistant", "content": "old message"}
    restored = message_from_dict(legacy)
    assert restored.content == "old message"
    assert restored.reasoning is None
    assert restored.reasoning_signature is None


# ─── CLI config: PHOSON_PRESERVE_THINKING tri-state ──────────────────────────


def test_config_preserve_thinking_default_none(monkeypatch, tmp_path) -> None:
    """Unset → ``None`` (the adapter decides)."""
    from phoson_cli.config import load_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_PRESERVE_THINKING", raising=False)

    assert load_config().preserve_thinking is None


def test_config_preserve_thinking_env_true(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import load_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PHOSON_PRESERVE_THINKING", "true")

    assert load_config().preserve_thinking is True


def test_config_preserve_thinking_env_false(monkeypatch, tmp_path) -> None:
    from phoson_cli.config import load_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PHOSON_PRESERVE_THINKING", "false")

    assert load_config().preserve_thinking is False


def test_config_preserve_thinking_file(monkeypatch, tmp_path) -> None:
    """A config.toml ``preserve_thinking`` is honored when the env is unset."""
    from phoson_cli.config import load_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    (home / ".phoson" / "config.toml").write_text(
        "[defaults]\npreserve_thinking = true\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_PRESERVE_THINKING", raising=False)

    assert load_config().preserve_thinking is True


def test_config_preserve_thinking_env_overrides_file(monkeypatch, tmp_path) -> None:
    """The env var wins over the file value."""
    from phoson_cli.config import load_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    (home / ".phoson" / "config.toml").write_text(
        "[defaults]\npreserve_thinking = true\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PHOSON_PRESERVE_THINKING", "false")

    assert load_config().preserve_thinking is False


def test_config_preserve_thinking_save_round_trip(monkeypatch, tmp_path) -> None:
    """save_config persists the tri-state and drops it when ``None``."""
    from phoson_cli.config import PhosonConfig, load_config, save_config

    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_PRESERVE_THINKING", raising=False)

    save_config(PhosonConfig(preserve_thinking=False))
    content = (home / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert "preserve_thinking = false" in content
    assert load_config().preserve_thinking is False

    # Setting it back to None removes the line (adapter decides again).
    save_config(PhosonConfig(preserve_thinking=None))
    content2 = (home / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert "preserve_thinking" not in content2
    assert load_config().preserve_thinking is None
