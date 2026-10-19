"""Unit tests for the Google Code Assist adapter (no network, no real creds).

The transport is always a :class:`httpx.MockTransport` and the credential
store is redirected with ``PHOSON_GOOGLE_AUTH_FILE`` to ``tmp_path`` so the
real ``~/.phoson/auth-google.json`` is never read or written.
"""

import json
import time
import base64
import hashlib

import httpx
import pytest

from phoson_llm.schemas import (
    Message,
    ErrorEvent,
    ImageBlock,
    TokenEvent,
    UsageEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolUseBlock,
    LLMStartEvent,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ReasoningDoneEvent,
    ReasoningStartEvent,
    ReasoningTokenEvent,
)
from phoson_llm.auth.code_assist import (
    AUTH_MODE,
    CodeAssistTokens,
    CodeAssistAuthError,
    has_tokens,
    auth_status,
    load_tokens,
    save_tokens,
    clear_tokens,
    auth_file_path,
    generate_state,
    _code_from_input,
    generate_pkce_pair,
)
from phoson_llm.chats.code_assist import (
    STREAM_PATH,
    DEFAULT_MODELS,
    CODE_ASSIST_BASE_URL,
    CodeAssistChat,
    _sse_error,
    user_agent,
    list_models,
    unwrap_frame,
    _block_reason,
    build_headers,
    ensure_project,
    load_code_assist,
    _iter_sse_payloads,
    build_generate_body,
    _usage_from_metadata,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _sse_body(frames: list[dict]) -> str:
    """Serialise frames as an SSE stream (one ``data:`` frame per object)."""
    return "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)


def _wrapped(response: dict) -> dict:
    """Wrap a ``GenerateContentResponse`` in the subscription envelope."""
    return {"response": response}


async def _async_iter(items: list[str]):
    for item in items:
        yield item


def _tokens(**overrides) -> CodeAssistTokens:
    base: dict = {"access_token": "access-token", "refresh_token": "refresh-token"}
    base.update(overrides)
    return CodeAssistTokens(**base)


def _messages() -> list[Message]:
    return [Message(role="user", content="hi")]


def _config() -> ModelConfig:
    return ModelConfig(model="gemini-2.5-pro")


@pytest.fixture
def auth_env(tmp_path, monkeypatch) -> object:
    """Redirect the credential store to a throwaway file; return its path."""
    path = tmp_path / "auth-google.json"
    monkeypatch.setenv("PHOSON_GOOGLE_AUTH_FILE", str(path))
    monkeypatch.delenv("PHOSON_HOME", raising=False)
    return path


# ─── Credential store ────────────────────────────────────────────────────────


def test_save_tokens_writes_google_shape_with_owner_only_mode(auth_env) -> None:
    saved = save_tokens(
        _tokens(
            expiry=time.time() + 3600,
            email="user@example.com",
            tier="standard-tier",
            project_id="proj-1",
        )
    )

    assert saved == auth_env
    assert auth_env.exists()
    assert auth_env.stat().st_mode & 0o777 == 0o600

    data = json.loads(auth_env.read_text(encoding="utf-8"))
    assert data["auth_mode"] == AUTH_MODE == "google-code-assist"
    assert isinstance(data["tokens"], dict)
    assert data["tokens"]["access_token"] == "access-token"
    assert data["tokens"]["refresh_token"] == "refresh-token"
    assert data["tokens"]["email"] == "user@example.com"
    assert data["tokens"]["tier"] == "standard-tier"
    assert data["tokens"]["project_id"] == "proj-1"
    assert "last_refresh" in data


def test_token_round_trip_and_clear(auth_env) -> None:
    assert has_tokens() is False

    save_tokens(_tokens(project_id="proj-1"))
    loaded = load_tokens()

    assert loaded is not None
    assert loaded.access_token == "access-token"
    assert loaded.refresh_token == "refresh-token"
    assert loaded.project_id == "proj-1"
    assert has_tokens() is True

    assert clear_tokens() is True
    assert clear_tokens() is False
    assert has_tokens() is False


def test_load_tokens_malformed_json_returns_none(auth_env) -> None:
    auth_env.write_text("{ this is not json", encoding="utf-8")
    assert load_tokens() is None


def test_auth_file_path_honours_the_env_override(auth_env) -> None:
    assert auth_file_path() == auth_env


def test_from_auth_json_rejects_foreign_auth_mode() -> None:
    chatgpt_file = {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "openai-token", "account_id": "acct"},
    }
    assert CodeAssistTokens.from_auth_json(chatgpt_file) is None


def test_from_auth_json_accepts_google_payload_and_aliases() -> None:
    tokens = CodeAssistTokens.from_auth_json(
        {
            "auth_mode": AUTH_MODE,
            "tokens": {
                "access_token": "a",
                "refresh_token": "r",
                "tier": "free-tier",
                "project_id": "proj-7",
            },
            "last_refresh": "2026-10-16T18:04:05Z",
        }
    )

    assert tokens is not None
    assert tokens.access_token == "a"
    # ``plan_type``/``account_id`` alias the Code Assist vocabulary.
    assert tokens.plan_type == "free-tier"
    assert tokens.account_id == "proj-7"


# ─── auth_status ─────────────────────────────────────────────────────────────


def test_auth_status_reports_missing_credentials(auth_env) -> None:
    status = auth_status()

    assert status.logged_in is False
    assert status.email is None
    assert status.plan_type is None
    assert status.account_id is None
    assert status.refreshable is False
    assert status.path == auth_env


def test_auth_status_reports_present_credentials(auth_env) -> None:
    save_tokens(
        _tokens(
            expiry=time.time() + 3600,
            email="user@example.com",
            tier="standard-tier",
            project_id="proj-1",
        )
    )

    status = auth_status()

    assert status.logged_in is True
    assert status.email == "user@example.com"
    assert status.plan_type == "standard-tier"
    assert status.account_id == "proj-1"
    assert status.tier == "standard-tier"
    assert status.project_id == "proj-1"
    assert status.expires_at is not None
    assert status.refreshable is True
    assert status.path == auth_env


# ─── Pasted-code parsing ─────────────────────────────────────────────────────


def test_code_from_input_reads_full_redirect_url() -> None:
    url = "http://localhost:8085/oauth2callback?code=4%2F0Abc&state=xyz"
    assert _code_from_input({"code": url}) == "4/0Abc"


def test_code_from_input_accepts_raw_code() -> None:
    assert _code_from_input({"code": "  4/0AeanS0abc-XYZ_123  "}) == (
        "4/0AeanS0abc-XYZ_123"
    )


def test_code_from_input_accepts_code_query_fragment() -> None:
    assert _code_from_input({"code": "state=xyz&code=raw-code"}) == "raw-code"


def test_code_from_input_rejects_denied_login() -> None:
    url = (
        "https://codeassist.google.com/authcode"
        "?error=access_denied&error_description=user%20said%20no"
    )

    with pytest.raises(CodeAssistAuthError) as excinfo:
        _code_from_input({"code": url})

    assert excinfo.value.terminal is True
    assert "user said no" in str(excinfo.value)


def test_code_from_input_rejects_empty_input() -> None:
    with pytest.raises(CodeAssistAuthError):
        _code_from_input({"code": "   "})


# ─── PKCE helpers ────────────────────────────────────────────────────────────


def test_pkce_pair_is_the_s256_challenge_of_the_verifier() -> None:
    verifier, challenge = generate_pkce_pair()

    assert "=" not in verifier
    assert "=" not in challenge
    assert len(verifier) >= 43
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii")
    assert challenge == expected.rstrip("=")


def test_generate_state_is_unpadded_base64url() -> None:
    state = generate_state()

    assert "=" not in state
    assert len(state) >= 43


# ─── Request shaping ─────────────────────────────────────────────────────────


def test_build_generate_body_wraps_a_plain_text_turn() -> None:
    body = build_generate_body(
        messages=_messages(), config=_config(), project_id="proj-1"
    )

    assert body["model"] == "gemini-2.5-pro"
    assert body["project"] == "proj-1"
    assert body["request"]["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert body["request"]["generationConfig"] == {
        "temperature": 0.7,
        "maxOutputTokens": 32 * 1024,
    }
    assert isinstance(body["user_prompt_id"], str) and body["user_prompt_id"]


def test_build_generate_body_omits_an_unknown_project() -> None:
    body = build_generate_body(messages=_messages(), config=_config(), project_id=None)
    assert "project" not in body


def test_build_generate_body_inlines_a_file_image(tmp_path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"png-bytes")
    messages = [
        Message(
            role="user",
            content=[ImageBlock(source=f"file://{image}", media_type="image/png")],
        )
    ]

    body = build_generate_body(messages=messages, config=_config(), project_id=None)
    part = body["request"]["contents"][0]["parts"][0]

    assert part["inlineData"]["mimeType"] == "image/png"
    assert part["inlineData"]["data"] == base64.b64encode(b"png-bytes").decode()


def test_build_generate_body_degrades_a_missing_file_image(tmp_path) -> None:
    missing = tmp_path / "gone.png"
    messages = [Message(role="user", content=[ImageBlock(source=f"file://{missing}")])]

    body = build_generate_body(messages=messages, config=_config(), project_id=None)
    part = body["request"]["contents"][0]["parts"][0]

    assert part["text"] == "[image no longer available: gone.png]"


def test_build_generate_body_keys_a_tool_result_by_name() -> None:
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    tool_call_id="call-1",
                    tool_name="get_weather",
                    args={"city": "MX"},
                )
            ],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_call_id="call-1", result="sunny")],
        ),
    ]

    body = build_generate_body(messages=messages, config=_config(), project_id=None)
    contents = body["request"]["contents"]

    assert contents[0]["role"] == "model"
    assert contents[0]["parts"][0]["functionCall"] == {
        "name": "get_weather",
        "args": {"city": "MX"},
        "id": "call-1",
    }
    assert contents[1]["parts"][0]["functionResponse"] == {
        "name": "get_weather",
        "response": {"result": "sunny"},
        "id": "call-1",
    }


def test_build_generate_body_puts_the_system_instruction_apart() -> None:
    messages = [
        Message(role="system", content="be brief"),
        Message(role="user", content="hi"),
    ]
    config = ModelConfig(model="gemini-2.5-pro", system="config wins")

    body = build_generate_body(messages=messages, config=config, project_id=None)

    assert body["request"]["systemInstruction"] == {"parts": [{"text": "config wins"}]}
    assert [entry["role"] for entry in body["request"]["contents"]] == ["user"]


def test_build_generate_body_emits_function_declarations() -> None:
    tools = [
        ToolDefinition(
            name="get_weather",
            description="Look up the weather",
            parameters={"type": "object", "properties": {}},
        )
    ]

    body = build_generate_body(
        messages=_messages(), config=_config(), project_id=None, tools=tools
    )

    declaration = body["request"]["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "get_weather"
    assert declaration["description"] == "Look up the weather"


def test_build_headers_send_bearer_and_client_identity() -> None:
    headers = build_headers(_tokens(), model="gemini-2.5-pro")

    assert headers["Authorization"] == "Bearer access-token"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "text/event-stream"
    assert headers["X-Goog-Api-Client"] == "gl-python/1.0.0"
    assert headers["User-Agent"] == user_agent("gemini-2.5-pro")


def test_user_agent_names_the_client_and_falls_back_for_a_blank_model() -> None:
    agent = user_agent("gemini-2.5-pro", version="9.9.9")

    assert agent.startswith("GeminiCLI/9.9.9/gemini-2.5-pro (")
    assert agent.endswith("; terminal)")
    assert "GeminiCLI/1.0.0/unknown" in user_agent("")


# ─── SSE parsing ─────────────────────────────────────────────────────────────


async def test_iter_sse_payloads_skips_comments_and_done() -> None:
    lines = _async_iter(
        [
            'data: {"response": {"a": 1}}',
            "",
            ": keep-alive comment",
            "event: message",
            'data: {"response": {"b": 2}}',
            "",
            "data: [DONE]",
            "",
        ]
    )

    payloads = [payload async for payload in _iter_sse_payloads(lines)]
    assert payloads == [{"response": {"a": 1}}, {"response": {"b": 2}}]


async def test_iter_sse_payloads_ignores_malformed_and_non_object_frames() -> None:
    lines = _async_iter(["data: { not json", "", "data: [1, 2]", ""])

    payloads = [payload async for payload in _iter_sse_payloads(lines)]
    assert payloads == []


def test_unwrap_frame_accepts_envelope_and_bare_response() -> None:
    assert unwrap_frame({"response": {"candidates": []}}) == {"candidates": []}
    assert unwrap_frame({"candidates": []}) == {"candidates": []}
    assert unwrap_frame({"usageMetadata": {"promptTokenCount": 1}}) == {
        "usageMetadata": {"promptTokenCount": 1}
    }
    assert unwrap_frame({"traceId": "abc"}) is None


def test_usage_from_metadata_maps_gemini_counters() -> None:
    usage = _usage_from_metadata(
        {
            "promptTokenCount": 11,
            "candidatesTokenCount": 4,
            "cachedContentTokenCount": 3,
        }
    )

    assert usage is not None
    assert (usage.input, usage.output, usage.cache_read) == (11, 4, 3)
    assert usage.cache_write == 0
    assert _usage_from_metadata(None) is None
    assert _usage_from_metadata("nope") is None


def test_sse_error_maps_a_google_status_to_a_retryable_code() -> None:
    message, code, retryable = _sse_error(
        {"error": {"message": "slow down", "status": "RESOURCE_EXHAUSTED"}}
    )

    assert message == "slow down"
    assert code == "rate_limit"
    assert retryable is True


def test_sse_error_reads_an_error_nested_in_the_envelope() -> None:
    message, code, retryable = _sse_error(
        {"response": {"error": {"message": "boom", "status": "INTERNAL"}}}
    )

    assert message == "boom"
    assert code == "server_error"
    assert retryable is True


def test_sse_error_defaults_and_context_overflow() -> None:
    message, code, retryable = _sse_error({"error": {"message": "unspecified"}})
    assert message == "unspecified"
    assert code == "provider_error"
    assert retryable is False

    _, code, retryable = _sse_error(
        {"error": {"message": "prompt is too long: 90000 tokens"}}
    )
    assert code == "context_length_exceeded"
    assert retryable is False


def test_block_reason_reads_prompt_feedback() -> None:
    assert _block_reason({"promptFeedback": {"blockReason": "SAFETY"}}) == "SAFETY"
    assert _block_reason({"promptFeedback": {}}) is None
    assert _block_reason({}) is None


# ─── Streaming ───────────────────────────────────────────────────────────────


def _stream_frames() -> list[dict]:
    """One turn: a thought, some text, a tool call, usage and a ``STOP``."""
    return [
        _wrapped(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "Let me think", "thought": True}]}}
                ]
            }
        ),
        _wrapped(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Hello"},
                                {
                                    "functionCall": {
                                        "id": "call-1",
                                        "name": "get_weather",
                                        "args": {"city": "MX"},
                                    }
                                },
                            ]
                        }
                    }
                ]
            }
        ),
        _wrapped(
            {
                "candidates": [{"finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 11,
                    "candidatesTokenCount": 4,
                    "cachedContentTokenCount": 3,
                },
            }
        ),
    ]


def _stream_handler(requests: list[dict], *, frames: list[dict] | None = None):
    """MockTransport handler: onboarding handshake, then the SSE stub."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(
                200,
                json={
                    "cloudaicompanionProject": "proj-1",
                    "currentTier": {"id": "standard-tier"},
                },
            )
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=_sse_body(frames or _stream_frames()))

    return handler


@pytest.mark.asyncio
async def test_stream_emits_the_documented_event_sequence(auth_env) -> None:
    save_tokens(_tokens(expiry=time.time() + 3600))
    chat = CodeAssistChat(transport=httpx.MockTransport(_stream_handler([])))

    events = [event async for event in chat.stream(_messages(), _config())]

    assert [type(event).__name__ for event in events] == [
        "LLMStartEvent",
        "ReasoningStartEvent",
        "ReasoningTokenEvent",
        "TokenEvent",
        "ToolCallEvent",
        "ReasoningDoneEvent",
        "UsageEvent",
        "LLMDoneEvent",
    ]

    start = events[0]
    assert isinstance(start, LLMStartEvent)
    assert start.model == "gemini-2.5-pro"
    assert start.message_count == 1

    assert isinstance(events[1], ReasoningStartEvent)
    assert isinstance(events[2], ReasoningTokenEvent)
    assert events[2].content == "Let me think"
    assert isinstance(events[3], TokenEvent)
    assert events[3].content == "Hello"

    call = events[4]
    assert isinstance(call, ToolCallEvent)
    assert call.index == 0
    assert call.tool_call_id == "call-1"
    assert call.tool_name == "get_weather"
    assert call.args == {"city": "MX"}

    assert isinstance(events[5], ReasoningDoneEvent)
    assert events[5].content == "Let me think"

    usage_event = events[6]
    assert isinstance(usage_event, UsageEvent)
    assert usage_event.model == "gemini-2.5-pro"
    assert usage_event.usage.input == 11
    assert usage_event.usage.output == 4
    assert usage_event.usage.cache_read == 3
    assert usage_event.cost_known is False

    done = events[7]
    assert isinstance(done, LLMDoneEvent)
    assert done.content == "Hello"
    assert done.has_tool_calls is True
    assert done.stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_stream_sends_build_generate_body_output(auth_env) -> None:
    save_tokens(_tokens(expiry=time.time() + 3600))
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": "proj-1"})
        assert request.headers["Authorization"] == "Bearer access-token"
        assert request.url.params["alt"] == "sse"
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=_sse_body([_wrapped({"candidates": []})]))

    chat = CodeAssistChat(transport=httpx.MockTransport(handler))

    async for _ in chat.stream(_messages(), _config()):
        pass

    expected = build_generate_body(
        messages=_messages(), config=_config(), project_id="proj-1"
    )
    assert len(sent) == 1
    prompt_id = sent[0].pop("user_prompt_id")
    expected.pop("user_prompt_id")
    assert isinstance(prompt_id, str) and prompt_id
    assert sent[0] == expected


@pytest.mark.asyncio
async def test_stream_surfaces_an_error_frame_as_an_error_event(auth_env) -> None:
    save_tokens(_tokens(expiry=time.time() + 3600))
    frames = [{"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}}]
    chat = CodeAssistChat(
        transport=httpx.MockTransport(_stream_handler([], frames=frames))
    )

    events = [event async for event in chat.stream(_messages(), _config())]

    assert [type(event).__name__ for event in events] == [
        "LLMStartEvent",
        "ErrorEvent",
    ]
    error = events[1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "rate_limit"
    assert error.retryable is True


@pytest.mark.asyncio
async def test_stream_surfaces_a_nested_envelope_error(auth_env) -> None:
    """The v1internal surface wraps errors in ``{"response": {"error": …}}``."""
    save_tokens(_tokens(expiry=time.time() + 3600))
    frames = [
        _wrapped(
            {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
        )
    ]
    chat = CodeAssistChat(
        transport=httpx.MockTransport(_stream_handler([], frames=frames))
    )

    events = [event async for event in chat.stream(_messages(), _config())]

    assert [type(event).__name__ for event in events] == [
        "LLMStartEvent",
        "ErrorEvent",
    ]
    error = events[1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "rate_limit"
    assert error.retryable is True


@pytest.mark.asyncio
async def test_stream_reports_a_prompt_block_as_a_content_filter(auth_env) -> None:
    save_tokens(_tokens(expiry=time.time() + 3600))
    frames = [_wrapped({"promptFeedback": {"blockReason": "SAFETY"}})]
    chat = CodeAssistChat(
        transport=httpx.MockTransport(_stream_handler([], frames=frames))
    )

    events = [event async for event in chat.stream(_messages(), _config())]

    assert [type(event).__name__ for event in events] == [
        "LLMStartEvent",
        "ErrorEvent",
    ]
    error = events[1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "content_filter"
    assert error.retryable is False


@pytest.mark.asyncio
async def test_stream_without_a_login_yields_a_terminal_auth_error(auth_env) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be issued without credentials")

    chat = CodeAssistChat(transport=httpx.MockTransport(handler))

    events = [event async for event in chat.stream(_messages(), _config())]

    assert [type(event).__name__ for event in events] == [
        "LLMStartEvent",
        "ErrorEvent",
    ]
    error = events[1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "auth"
    assert error.retryable is False


# ─── Onboarding ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_code_assist_posts_the_client_metadata() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "cloudaicompanionProject": {"id": "proj-9"},
                "allowedTiers": [{"id": "legacy-tier", "isDefault": True}],
            },
        )

    payload = await load_code_assist(_tokens(), transport=httpx.MockTransport(handler))

    assert seen["path"] == "/v1internal:loadCodeAssist"
    assert seen["body"] == {
        "metadata": {
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        }
    }
    assert payload["cloudaicompanionProject"] == {"id": "proj-9"}


@pytest.mark.asyncio
async def test_load_code_assist_raises_a_terminal_error_on_a_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "denied"}})

    with pytest.raises(CodeAssistAuthError) as excinfo:
        await load_code_assist(_tokens(), transport=httpx.MockTransport(handler))

    assert excinfo.value.terminal is True


@pytest.mark.asyncio
async def test_ensure_project_onboards_the_free_tier_without_a_project(
    auth_env, monkeypatch
) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    bodies: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(200, json={})
        assert request.url.path.endswith(":onboardUser")
        bodies["onboard"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"done": True, "response": {"cloudaicompanionProject": "proj-new"}},
        )

    setup = await ensure_project(
        _tokens(), path=auth_env, transport=httpx.MockTransport(handler)
    )

    assert setup.project_id == "proj-new"
    assert setup.tier == "free-tier"
    # The free tier must not be asked for with a project attached.
    assert bodies["onboard"] == {"tierId": "free-tier"}

    stored = load_tokens()
    assert stored is not None
    assert stored.project_id == "proj-new"


@pytest.mark.asyncio
async def test_ensure_project_explains_a_required_user_project(
    auth_env, monkeypatch
) -> None:
    """``userDefinedCloudaicompanionProject`` must point the user at config/env."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    onboarding = {
        "allowedTiers": [
            {
                "id": "standard-tier",
                "isDefault": True,
                "userDefinedCloudaicompanionProject": True,
            }
        ],
        "ineligibleTiers": [
            {
                "reasonCode": "UNSUPPORTED_CLIENT",
                "reasonMessage": "this client is no longer supported",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(200, json=onboarding)
        assert request.url.path.endswith(":onboardUser")
        return httpx.Response(
            200, json={"done": True, "response": {"cloudaicompanionProject": {}}}
        )

    with pytest.raises(CodeAssistAuthError) as excinfo:
        await ensure_project(
            _tokens(), path=auth_env, transport=httpx.MockTransport(handler)
        )

    message = str(excinfo.value)
    assert "GOOGLE_CLOUD_PROJECT" in message
    assert "this client is no longer supported" in message


@pytest.mark.asyncio
async def test_ensure_project_rejects_a_project_the_account_cannot_use(
    auth_env, monkeypatch
) -> None:
    """A backend ``projectValidationError`` becomes a terminal, clear error."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":loadCodeAssist")
        return httpx.Response(
            200,
            json={
                "projectValidationError": {
                    "code": 7,
                    "message": "lacks the required IAM permission",
                },
                "allowedTiers": [{"id": "standard-tier"}],
            },
        )

    with pytest.raises(CodeAssistAuthError) as excinfo:
        await ensure_project(
            _tokens(project_id="proj-x"),
            path=auth_env,
            transport=httpx.MockTransport(handler),
        )

    assert "refused project" in str(excinfo.value)
    assert "lacks the required IAM permission" in str(excinfo.value)
    assert excinfo.value.terminal is True


# ─── Model catalogue ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_models_reads_the_quota_buckets(auth_env) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": "proj-1"})
        assert request.url.path.endswith(":retrieveUserQuota")
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "buckets": [
                    {"modelId": "gemini-2.5-pro"},
                    {"modelId": "gemini-3.0-ultra"},
                    {"modelId": "gemini-2.5-pro"},
                ]
            },
        )

    models = await list_models(
        tokens=_tokens(project_id="proj-1"),
        auth_file=auth_env,
        transport=httpx.MockTransport(handler),
    )

    assert seen["body"] == {"project": "proj-1"}
    assert seen["auth"] == "Bearer access-token"
    assert [model.slug for model in models] == ["gemini-2.5-pro", "gemini-3.0-ultra"]
    assert models[0].label == "Gemini 2.5 Pro"
    assert models[1].label == "gemini-3.0-ultra"


@pytest.mark.asyncio
async def test_list_models_falls_back_to_the_default_catalogue(auth_env) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": "proj-1"})
        return httpx.Response(200, json={"buckets": []})

    models = await list_models(
        tokens=_tokens(project_id="proj-1"),
        auth_file=auth_env,
        transport=httpx.MockTransport(handler),
    )

    assert [(model.slug, model.label) for model in models] == list(DEFAULT_MODELS)


@pytest.mark.asyncio
async def test_list_models_without_a_login_raises_an_auth_error(auth_env) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be issued without credentials")

    with pytest.raises(CodeAssistAuthError):
        await list_models(auth_file=auth_env, transport=httpx.MockTransport(handler))


# ─── Factory / lazy export surface ───────────────────────────────────────────


def test_factory_build_chat_returns_code_assist() -> None:
    from phoson_llm import build_chat

    chat = build_chat("google-code-assist")

    assert isinstance(chat, CodeAssistChat)
    assert chat.base_url == CODE_ASSIST_BASE_URL
    assert chat.stream_url == f"{CODE_ASSIST_BASE_URL}{STREAM_PATH}"


def test_lazy_exports_resolve_from_package_root_and_chats() -> None:
    from phoson_llm import CodeAssistChat as FromRoot
    from phoson_llm.chats import CodeAssistChat as FromChats

    assert FromRoot is CodeAssistChat
    assert FromChats is CodeAssistChat


def test_base_url_override_is_honoured() -> None:
    chat = CodeAssistChat(base_url="https://gateway.example/v1internal/")

    assert chat.base_url == "https://gateway.example/v1internal"
    assert chat.stream_url == (
        "https://gateway.example/v1internal:streamGenerateContent"
    )
