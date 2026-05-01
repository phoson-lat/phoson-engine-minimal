from phoson_cli.config import PhosonConfig, build_chat
from phoson_llm.schemas import TokenUsage, UsageEvent
from phoson_llm.chats.openrouter import (
    OpenRouterChat,
    _parse_tool_args,
    _extract_reasoning_delta,
)


class _Delta:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_build_chat_returns_openrouter_chat_for_openrouter_provider() -> None:
    chat = build_chat(
        PhosonConfig(
            provider="openrouter",
            openrouter_api_key="test-key",
        )
    )

    assert isinstance(chat, OpenRouterChat)


def test_extract_reasoning_delta_supports_reasoning_content() -> None:
    assert (
        _extract_reasoning_delta(_Delta(reasoning_content="step by step"))
        == "step by step"
    )


def test_extract_reasoning_delta_supports_reasoning_alias() -> None:
    assert _extract_reasoning_delta(_Delta(reasoning="trace")) == "trace"


def test_usage_event_can_mark_openrouter_cost_as_unknown() -> None:
    event = UsageEvent(
        model="minimax/minimax-m2.5:free",
        usage=TokenUsage(input=12, output=7),
        cost_usd=0.0,
        cost_known=False,
    )

    assert event.usage.input == 12
    assert event.usage.output == 7
    assert event.cost_usd == 0.0
    assert event.cost_known is False


def test_parse_tool_args_accepts_plain_json_object() -> None:
    assert _parse_tool_args('{"command":"git status"}') == {"command": "git status"}


def test_parse_tool_args_maps_plain_string_to_command() -> None:
    assert _parse_tool_args('"git log -1 --oneline"') == {
        "command": "git log -1 --oneline"
    }


def test_parse_tool_args_maps_invalid_json_to_command_fallback() -> None:
    assert _parse_tool_args("git diff --stat") == {"command": "git diff --stat"}


def test_openrouter_tool_chunks_can_arrive_before_id_and_name() -> None:
    tool_args_acc: dict[int, str] = {}
    tool_names: dict[int, str] = {}
    tool_ids: dict[int, str] = {}

    idx = 0
    tool_args_acc.setdefault(idx, "")
    tool_args_acc[idx] += '{"city":"Qro"}'

    assert tool_args_acc[idx] == '{"city":"Qro"}'

    tool_ids[idx] = "call_123"
    tool_names[idx] = "get_weather"

    emitted = []
    for current_idx, raw in tool_args_acc.items():
        if current_idx not in tool_ids or current_idx not in tool_names:
            continue
        emitted.append((tool_ids[current_idx], tool_names[current_idx], raw))

    assert emitted == [("call_123", "get_weather", '{"city":"Qro"}')]
