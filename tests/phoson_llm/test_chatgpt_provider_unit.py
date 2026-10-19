"""Unit tests for the ChatGPT subscription adapter (no network, no real creds).

The transport is always a :class:`httpx.MockTransport` and the credential
store is redirected with ``PHOSON_AUTH_FILE`` to ``tmp_path`` so the real
``~/.phoson/auth.json`` is never read or written.
"""

import json
import time
import base64

import httpx
import pytest

from phoson_llm.schemas import (
    Message,
    ErrorEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
)
from phoson_llm.auth.chatgpt import (
    ChatGPTTokens,
    jwt_expiry,
    load_tokens,
    save_tokens,
    jwt_account_id,
    decode_jwt_claims,
    import_codex_tokens,
    parse_id_token_claims,
)
from phoson_llm.chats.chatgpt import (
    CHATGPT_CODEX_BASE_URL,
    ChatGPTChat,
    ChatGPTModel,
    _sse_error,
    list_models,
    build_headers,
    _iter_sse_payloads,
    build_responses_body,
    _terminal_stop_reason,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _b64url(data: dict) -> str:
    """base64url-encode a JSON object without padding (a JWT segment)."""
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    """Hand-build an unsigned ``header.payload.signature`` JWT."""
    header = _b64url({"alg": "none", "typ": "JWT"})
    return f"{header}.{_b64url(claims)}.signature"


def _sse_body(frames: list[dict]) -> str:
    """Serialise frames as an SSE stream (one ``data:`` frame per object)."""
    return "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)


async def _async_iter(items: list[str]):
    for item in items:
        yield item


@pytest.fixture
def auth_env(tmp_path, monkeypatch) -> object:
    """Redirect the credential store to a throwaway file; return its path."""
    path = tmp_path / "auth.json"
    monkeypatch.setenv("PHOSON_AUTH_FILE", str(path))
    return path


def _completed_stream(text: str = "hello") -> str:
    return _sse_body(
        [
            {"type": "response.output_text.delta", "delta": text},
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 4,
                        "input_tokens_details": {"cached_tokens": 3},
                    },
                    "end_turn": True,
                },
            },
        ]
    )


def _messages() -> list[Message]:
    return [Message(role="user", content="hi")]


def _config() -> ModelConfig:
    return ModelConfig(model="gpt-5-codex")


# ─── Factory / lazy export surface ───────────────────────────────────────────


def test_factory_build_chat_returns_chatgpt() -> None:
    from phoson_llm import ChatGPTChat as FromChatgptModule

    chat = FromChatgptModule()
    assert isinstance(chat, ChatGPTChat)
    assert chat.base_url == CHATGPT_CODEX_BASE_URL


def test_factory_build_chat_unknown_provider_raises() -> None:
    from phoson_llm import build_chat
    from phoson_llm.exceptions import PhosonLLMError

    with pytest.raises(PhosonLLMError):
        build_chat("does-not-exist")


def test_lazy_exports_resolve_from_package_root_and_chats() -> None:
    from phoson_llm import ChatGPTChat as FromRoot
    from phoson_llm.chats import ChatGPTChat as FromChats

    assert FromRoot is ChatGPTChat
    assert FromChats is ChatGPTChat


def test_auth_package_lazily_exposes_has_tokens() -> None:
    """``phoson_llm.auth`` must re-export the credential-store helpers."""
    from phoson_llm.auth import has_tokens

    assert callable(has_tokens)


# ─── Credential store ────────────────────────────────────────────────────────


def test_save_tokens_writes_codex_shape_with_owner_only_mode(auth_env) -> None:
    target = save_tokens(
        ChatGPTTokens(
            access_token="access-token",
            refresh_token="refresh-token",
            account_id="acct-123",
        )
    )

    assert target == auth_env
    assert auth_env.exists()
    assert auth_env.stat().st_mode & 0o777 == 0o600

    data = json.loads(auth_env.read_text(encoding="utf-8"))
    assert data["auth_mode"] == "chatgpt"
    assert isinstance(data["tokens"], dict)
    assert data["tokens"]["access_token"] == "access-token"
    assert data["tokens"]["refresh_token"] == "refresh-token"
    assert data["tokens"]["account_id"] == "acct-123"
    assert "last_refresh" in data


def test_load_tokens_round_trips_access_token(auth_env) -> None:
    save_tokens(ChatGPTTokens(access_token="round-trip", refresh_token="r"))

    loaded = load_tokens()
    assert loaded is not None
    assert loaded.access_token == "round-trip"
    assert loaded.refresh_token == "r"


def test_clear_tokens_returns_true_then_false(auth_env) -> None:
    from phoson_llm.auth.chatgpt import clear_tokens

    save_tokens(ChatGPTTokens(access_token="a", refresh_token="r"))
    assert clear_tokens() is True
    assert clear_tokens() is False


def test_has_tokens_false_when_file_absent(auth_env) -> None:
    from phoson_llm.auth.chatgpt import has_tokens

    assert auth_env.exists() is False
    assert has_tokens() is False


def test_load_tokens_malformed_json_returns_none(auth_env) -> None:
    auth_env.write_text("{ this is not json", encoding="utf-8")
    assert load_tokens() is None


def test_from_auth_json_rejects_flat_payload() -> None:
    flat = {"access_token": "a", "refresh_token": "r", "last_refresh": "now"}
    assert ChatGPTTokens.from_auth_json(flat) is None


# ─── JWT helpers ─────────────────────────────────────────────────────────────


def test_parse_id_token_claims_reads_namespaced_claims() -> None:
    id_token = _jwt(
        {
            "email": "user@example.com",
            "exp": 4102444800,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
                "chatgpt_account_id": "acct-42",
            },
        }
    )

    claims = parse_id_token_claims(id_token)
    assert claims["email"] == "user@example.com"
    assert claims["chatgpt_plan_type"] == "plus"
    assert claims["chatgpt_account_id"] == "acct-42"
    assert jwt_account_id(id_token) == "acct-42"


def test_decode_jwt_claims_and_expiry_handle_bad_input() -> None:
    token = _jwt({"exp": 4102444800})
    claims = decode_jwt_claims(token)
    assert claims is not None
    assert claims["exp"] == 4102444800
    assert jwt_expiry(token) == 4102444800.0

    assert decode_jwt_claims("not-a-jwt") is None
    assert decode_jwt_claims(None) is None
    assert jwt_expiry(None) is None


def test_tokens_expires_at_and_is_expired_for_past_and_future() -> None:
    expired = ChatGPTTokens(
        access_token=_jwt({"exp": time.time() - 60}), refresh_token="r"
    )
    future = ChatGPTTokens(
        access_token=_jwt({"exp": time.time() + 3600}), refresh_token="r"
    )

    assert expired.expires_at is not None
    assert expired.is_expired() is True
    assert future.is_expired() is False


# ─── Codex credential adoption ───────────────────────────────────────────────


def test_import_codex_tokens_adopts_codex_home_login(tmp_path, monkeypatch) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "codex-access",
                    "refresh_token": "codex-refresh",
                    "account_id": "codex-acct",
                },
                "last_refresh": "2026-10-16T18:04:05Z",
            }
        ),
        encoding="utf-8",
    )
    phoson_file = tmp_path / "phoson-auth.json"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PHOSON_AUTH_FILE", str(phoson_file))

    tokens = import_codex_tokens()

    assert tokens is not None
    assert tokens.access_token == "codex-access"
    assert phoson_file.exists()
    stored = load_tokens()
    assert stored is not None
    assert stored.access_token == "codex-access"


# ─── Request shaping ─────────────────────────────────────────────────────────


def test_build_responses_body_puts_developer_instruction_first() -> None:
    messages = [
        Message(role="system", content="be brief"),
        Message(role="user", content="hi"),
    ]

    body = build_responses_body(messages=messages, config=_config())

    items = body["input"]
    assert items[0]["role"] == "developer"
    assert items[0]["content"][0]["text"] == "be brief"
    roles = [item.get("role") for item in items if item.get("type") == "message"]
    assert roles.index("developer") < roles.index("user")


def test_build_responses_body_drops_historical_reasoning() -> None:
    messages = [
        Message(role="assistant", content="the answer", reasoning="secret reasoning")
    ]

    body = build_responses_body(messages=messages, config=_config())

    serialised = json.dumps(body)
    assert "the answer" in serialised
    assert "secret reasoning" not in serialised


def test_build_headers_sends_bearer_and_account_id() -> None:
    headers = build_headers(
        ChatGPTTokens(access_token="abc", refresh_token="r", account_id="acct-9")
    )

    assert headers["Authorization"] == "Bearer abc"
    assert headers["ChatGPT-Account-ID"] == "acct-9"


def test_base_url_override_is_honoured() -> None:
    chat = ChatGPTChat(base_url="https://gateway.example/codex/")

    assert chat.base_url == "https://gateway.example/codex"
    assert chat.responses_url == "https://gateway.example/codex/responses"


def test_chatgpt_model_label_falls_back_to_slug() -> None:
    assert ChatGPTModel(slug="gpt-5-codex").label == "gpt-5-codex"
    assert ChatGPTModel(slug="gpt-5", display_name="GPT-5").label == "GPT-5"


# ─── SSE parsing ─────────────────────────────────────────────────────────────


async def test_iter_sse_payloads_yields_json_data_frames() -> None:
    lines = _async_iter(
        [
            'data: {"type": "a"}',
            "",
            ": keep-alive comment",
            "event: response.completed",
            'data: {"type": "b"}',
            "",
            "data: [DONE]",
            "",
        ]
    )

    payloads = [payload async for payload in _iter_sse_payloads(lines)]
    assert payloads == [{"type": "a"}, {"type": "b"}]


def test_sse_error_extracts_message_code_and_retryability() -> None:
    message, code, retryable = _sse_error(
        {"response": {"error": {"message": "slow down", "code": "rate_limit_exceeded"}}}
    )
    assert message == "slow down"
    assert code == "rate_limit"
    assert retryable is True


def test_sse_error_marks_server_errors_retryable() -> None:
    message, code, retryable = _sse_error(
        {"error": {"message": "boom", "code": "server_error"}}
    )
    assert message == "boom"
    assert code == "overloaded"
    assert retryable is True


def test_terminal_stop_reason_maps_completed_and_incomplete() -> None:
    assert (
        _terminal_stop_reason(
            incomplete=False, response={"end_turn": True}, has_tool_calls=False
        )
        == "end_turn"
    )
    assert (
        _terminal_stop_reason(
            incomplete=False, response={"end_turn": False}, has_tool_calls=False
        )
        == "pause_turn"
    )
    assert (
        _terminal_stop_reason(
            incomplete=True,
            response={"incomplete_details": {"reason": "max_output_tokens"}},
            has_tool_calls=False,
        )
        == "max_tokens"
    )
    assert (
        _terminal_stop_reason(incomplete=False, response={}, has_tool_calls=True)
        == "tool_use"
    )


# ─── Streaming, usage accounting and 401 handling ────────────────────────────


@pytest.mark.asyncio
async def test_stream_reports_subscription_usage_without_cost(auth_env) -> None:
    save_tokens(ChatGPTTokens(access_token="access", refresh_token="refresh"))
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=_completed_stream())
    )
    chat = ChatGPTChat(transport=transport)

    events = [event async for event in chat.stream(_messages(), _config())]

    usage_events = [event for event in events if isinstance(event, UsageEvent)]
    assert len(usage_events) == 1
    usage_event = usage_events[0]
    assert usage_event.usage.input == 11
    assert usage_event.usage.output == 4
    assert usage_event.usage.cache_read == 3
    assert usage_event.cost_known is False

    done = [event for event in events if isinstance(event, LLMDoneEvent)]
    assert [event.content for event in done] == ["hello"]
    assert done[0].stop_reason == "end_turn"


def _run_401_scenario(path):
    """Drive one 401-then-200 turn; return the chat + recorded calls + tokens."""
    future = time.time() + 3600
    auth_claim = {"chatgpt_account_id": "acct-1"}
    old_access = _jwt(
        {"exp": future, "jti": "old", "https://api.openai.com/auth": auth_claim}
    )
    new_access = _jwt(
        {"exp": future + 60, "jti": "new", "https://api.openai.com/auth": auth_claim}
    )
    save_tokens(
        ChatGPTTokens(
            access_token=old_access,
            refresh_token="refresh-1",
            account_id="acct-1",
        ),
        path,
    )

    calls = {"response_auth": [], "refresh": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            calls["response_auth"].append(request.headers.get("authorization"))
            if len(calls["response_auth"]) == 1:
                return httpx.Response(401, json={"error": {"message": "expired"}})
            return httpx.Response(200, text=_completed_stream())
        calls["refresh"] += 1
        return httpx.Response(
            200, json={"access_token": new_access, "refresh_token": "refresh-2"}
        )

    chat = ChatGPTChat(transport=httpx.MockTransport(handler))
    return chat, calls, old_access, new_access


@pytest.mark.asyncio
async def test_stream_401_retries_with_refreshed_bearer(auth_env) -> None:
    chat, calls, old_access, new_access = _run_401_scenario(auth_env)

    events = [event async for event in chat.stream(_messages(), _config())]

    assert any(isinstance(event, LLMDoneEvent) for event in events)
    assert calls["response_auth"][0] == f"Bearer {old_access}"
    assert calls["response_auth"][1] == f"Bearer {new_access}"


@pytest.mark.asyncio
async def test_stream_401_retry_hits_refresh_endpoint_once(auth_env) -> None:
    chat, calls, _, _ = _run_401_scenario(auth_env)

    async for _ in chat.stream(_messages(), _config()):
        pass

    assert calls["refresh"] == 1


@pytest.mark.asyncio
async def test_explicit_api_key_does_not_refresh_or_retry() -> None:
    calls = {"responses": 0, "refresh": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            calls["responses"] += 1
            return httpx.Response(401, json={"error": {"message": "nope"}})
        calls["refresh"] += 1
        return httpx.Response(500)

    chat = ChatGPTChat(api_key="explicit-token", transport=httpx.MockTransport(handler))

    events = [event async for event in chat.stream(_messages(), _config())]

    assert any(isinstance(event, ErrorEvent) for event in events)
    assert calls["responses"] == 1
    assert calls["refresh"] == 0


# ─── Model catalogue ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_models_maps_catalogue_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers["Authorization"] == "Bearer access"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "gpt-5-codex", "display_name": "GPT-5 Codex"},
                    {"id": "gpt-5"},
                ]
            },
        )

    tokens = ChatGPTTokens(access_token="access", refresh_token="r", account_id="a")
    models = await list_models(tokens=tokens, transport=httpx.MockTransport(handler))

    assert [model.slug for model in models] == ["gpt-5-codex", "gpt-5"]
    assert models[0].label == "GPT-5 Codex"
    assert models[1].label == "gpt-5"
