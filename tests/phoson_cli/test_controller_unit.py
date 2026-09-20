"""Tests for the UI-independent SessionController.

The controller must run a full session lifecycle against a fake sink —
no prompt_toolkit, no Rich, no TTY. This is the guarantee that a new
front end is a sink, not a fork.
"""

import asyncio
import datetime
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_agent import Plugin, AgentMiddleware
from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message, TokenUsage, LLMDoneEvent
from phoson_agent.models import (
    RunStep,
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
)
from phoson_cli.controller import SessionController
from phoson_cli.ui_protocols import AgentEventSink

# ── Fake sink ────────────────────────────────────────────────────────────────


class FakeSink:
    """Recording AgentEventSink; asserts nothing, stores everything."""

    def __init__(self) -> None:
        self.events: list = []
        self.user_messages: list[tuple[str, Message]] = []
        self.attachments: list[list[str]] = []
        self.notifications: list[tuple[str, str]] = []
        self.session_ids: list[str] = []
        self.history_calls: list[tuple[list[Message], int]] = []
        self.reasoning = ""
        self.partial_captures = 0
        self.flushes = 0
        self.subagent_progress_events: list = []

    def on_user_message(self, text, message) -> None:
        self.user_messages.append((text, message))

    def on_attachments(self, sources) -> None:
        self.attachments.append(list(sources))

    def on_event(self, event) -> None:
        self.events.append(event)

    def flush_line(self) -> None:
        self.flushes += 1

    def capture_partial_reasoning(self) -> None:
        self.partial_captures += 1

    def take_reasoning(self) -> str:
        r, self.reasoning = self.reasoning, ""
        return r

    def set_session(self, session_id) -> None:
        self.session_ids.append(session_id)

    def print_history(self, path, tail=None, timestamps=None) -> None:
        self.history_calls.append((path, tail))

    def notify(self, kind, message) -> None:
        self.notifications.append((kind, message))

    def on_subagent_progress(self, progress) -> None:
        self.subagent_progress_events.append(progress)


assert isinstance(FakeSink(), AgentEventSink)  # runtime_checkable conformance


class TransactionalFakeSink(FakeSink):
    def __init__(self) -> None:
        super().__init__()
        self.visible_blocks: list[object] = []
        self.reset_count = 0
        self.restore_count = 0
        self.fail_replay = False

    def snapshot_session_view(self) -> object:
        return list(self.visible_blocks)

    def reset_session_view(self) -> None:
        self.reset_count += 1
        self.visible_blocks.clear()

    def restore_session_view(self, snapshot: object) -> None:
        self.restore_count += 1
        self.visible_blocks = list(snapshot)  # type: ignore[arg-type]

    def print_history(self, path, tail=None, timestamps=None) -> None:
        super().print_history(path, tail=tail, timestamps=timestamps)
        self.visible_blocks.append("loaded history")
        if self.fail_replay:
            raise RuntimeError("replay failed")


def _make_controller(tmp_path, **cfg) -> tuple[SessionController, FakeSink]:
    sink = FakeSink()
    config = PhosonConfig(
        provider="ollama",
        model="test-model",
        sessions_dir=tmp_path,
        **cfg,
    )
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        controller = SessionController(config, sink)
    return controller, sink


def _fake_stream(events):
    async def stream(path, config):
        for event in events:
            yield event

    return stream


def _done_event(answer="hello") -> AgentDoneEvent:
    return AgentDoneEvent(
        result=AgentRunResult(
            final_content=answer,
            history=[
                Message(role="user", content="q"),
                Message(role="assistant", content=answer),
            ],
            input_messages=[Message(role="user", content="q")],
            steps=[],
        )
    )


# ── Conformance + construction ───────────────────────────────────────────────


def test_controller_merges_configured_plugins_before_mcp(tmp_path) -> None:
    class CommunityPlugin(Plugin):
        @property
        def name(self) -> str:
            return "community"

    community = CommunityPlugin()
    mcp = CommunityPlugin()
    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    sink = FakeSink()

    with (
        patch("phoson_cli.controller.build_chat", return_value=MagicMock()),
        patch(
            "phoson_cli.controller.build_plugin_specs", return_value=[community, mcp]
        ),
    ):
        controller = SessionController(config, sink)

    assert controller.engine._loaded_plugins == [community, mcp]


def test_controller_cleans_initialized_plugins_when_extension_validation_fails(
    tmp_path,
) -> None:
    class InvalidPlugin(Plugin):
        def __init__(self) -> None:
            self.cleaned = False

        @property
        def name(self) -> str:
            return "invalid"

        def get_commands(self):
            from phoson_agent import CliCommandSpec

            return [CliCommandSpec(names=("/help",), help="bad", handler="missing")]

        def cleanup(self) -> None:
            self.cleaned = True

    plugin = InvalidPlugin()
    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    with (
        patch("phoson_cli.controller.build_chat", return_value=MagicMock()),
        patch("phoson_cli.controller.build_plugin_specs", return_value=[plugin]),
        pytest.raises(ValueError, match="conflicts with a native command"),
    ):
        SessionController(config, FakeSink())

    assert plugin.cleaned is True


def test_new_session_scoped_to_cwd(tmp_path) -> None:
    """#212: a newly created session is scoped to the working directory it
    starts in (persisted on first save); the picker then only lists it from
    that directory."""
    controller, _sink = _make_controller(tmp_path)
    assert controller.tree.cwd == str(Path.cwd())


def test_controller_requires_no_ui_dependencies(tmp_path) -> None:
    import inspect

    import phoson_cli.controller as mod

    source = inspect.getsource(mod)
    assert "prompt_toolkit" not in source
    assert "rich" not in source
    assert "textual" not in source
    controller, sink = _make_controller(tmp_path)
    assert controller.config.provider == "ollama"
    assert sink.session_ids  # session id announced at construction


# ── Run lifecycle: success / error / cancel ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_turn_forwards_reasoning_effort_to_model_config(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path, reasoning_effort="high")
    seen_configs = []

    async def stream(path, config):
        seen_configs.append(config)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield _done_event("hello")

    controller.engine.stream = stream

    await controller.run_turn("q")

    assert seen_configs[0].reasoning_effort == "high"


@pytest.mark.asyncio
async def test_run_turn_forwards_session_id_to_model_config(tmp_path) -> None:
    """G2: the conversation's session id must travel to ModelConfig so
    OpenRouter can pin the session to one upstream provider (sticky
    routing → warm prompt cache)."""
    controller, _sink = _make_controller(tmp_path)
    session_id = controller.tree.session_id
    seen_configs = []

    async def stream(path, config):
        seen_configs.append(config)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield _done_event("hello")

    controller.engine.stream = stream

    await controller.run_turn("q")

    assert seen_configs[0].session_id == session_id


@pytest.mark.asyncio
async def test_run_turn_reasoning_effort_defaults_to_none(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    seen_configs = []

    async def stream(path, config):
        seen_configs.append(config)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield _done_event("hello")

    controller.engine.stream = stream

    await controller.run_turn("q")

    assert seen_configs[0].reasoning_effort is None


@pytest.mark.parametrize("effort", ["xhigh", "max"])
@pytest.mark.asyncio
async def test_run_turn_forwards_extended_reasoning_efforts(tmp_path, effort) -> None:
    controller, _sink = _make_controller(tmp_path, reasoning_effort=effort)
    seen_configs = []

    async def stream(path, config):
        seen_configs.append(config)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield _done_event("hello")

    controller.engine.stream = stream

    await controller.run_turn("q")

    assert seen_configs[0].reasoning_effort == effort


@pytest.mark.asyncio
async def test_run_turn_unknown_reasoning_effort_falls_back_to_none(
    tmp_path,
) -> None:
    controller, _sink = _make_controller(tmp_path, reasoning_effort="extreme")
    seen_configs = []

    async def stream(path, config):
        seen_configs.append(config)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield _done_event("hello")

    controller.engine.stream = stream

    await controller.run_turn("q")

    assert seen_configs[0].reasoning_effort is None


@pytest.mark.asyncio
async def test_run_turn_success_end_to_end(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    controller.engine.stream = _fake_stream(
        [
            AgentStartEvent(model="m", message_count=1, max_iterations=50),
            AgentTokenEvent(content="hello"),
            _done_event("hello"),
        ]
    )

    outcome = await controller.run_turn("q")

    assert outcome.status == "done"
    assert outcome.final_content == "hello"
    assert [t for t, _ in sink.user_messages] == ["q"]
    assert [type(e).__name__ for e in sink.events] == [
        "AgentStartEvent",
        "AgentTokenEvent",
        "AgentDoneEvent",
    ]
    # Tree got user + assistant nodes; cursor on the assistant.
    node = controller.tree.nodes[controller.current_node_id]
    assert node.message.role == "assistant"
    # Session persisted.
    loaded = await controller.storage.load(controller.tree.session_id)
    assert len(loaded.nodes) == 2


def test_new_controller_session_is_not_started(tmp_path) -> None:
    """A fresh controller has an in-memory id but no session yet."""
    controller, _sink = _make_controller(tmp_path)
    assert controller.session_started is False


def test_reset_session_marks_not_started(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    controller._session_started = True
    controller._reset_session()
    assert controller.session_started is False


@pytest.mark.asyncio
async def test_session_started_when_first_turn_runs(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    assert controller.session_started is False

    controller.engine.stream = _fake_stream(
        [
            AgentStartEvent(model="m", message_count=1, max_iterations=50),
            _done_event("ok"),
        ]
    )
    await controller.run_turn("first message")

    assert controller.session_started is True


@pytest.mark.asyncio
async def test_loaded_session_is_started(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    controller._session_started = True
    controller.tree.append(parent_id=None, message=Message(role="user", content="hi"))
    await controller.storage.save(controller.tree)
    session_id = controller.tree.session_id

    other, _sink2 = _make_controller(tmp_path)
    assert other.session_started is False
    outcome = await other.load_session(session_id)
    assert outcome.ok is True
    assert other.session_started is True


@pytest.mark.asyncio
async def test_run_turn_drops_env_context_from_tree(tmp_path) -> None:
    """#212: env-context blocks (per-LLM-call request artifacts) must not be
    persisted to the tree — they would show up in the rewind picker and
    ``/tree`` and inflate ``message_count``."""
    from phoson_agent.middleware import is_env_context

    controller, _sink = _make_controller(tmp_path)
    env = Message(
        role="user", content="[env: step 1/20, time 0s elapsed, 600s remaining]"
    )
    done = AgentDoneEvent(
        result=AgentRunResult(
            final_content="hello",
            history=[
                Message(role="user", content="q"),
                env,
                Message(role="assistant", content="hello"),
            ],
            input_messages=[Message(role="user", content="q")],
            steps=[],
        )
    )
    controller.engine.stream = _fake_stream(
        [AgentStartEvent(model="m", message_count=1, max_iterations=50), done]
    )

    await controller.run_turn("q")

    # Only the genuine user + assistant nodes land in the tree.
    assert len(controller.tree.nodes) == 2
    assert not any(is_env_context(n.message) for n in controller.tree.nodes.values())
    loaded = await controller.storage.load(controller.tree.session_id)
    assert len(loaded.nodes) == 2


@pytest.mark.asyncio
async def test_run_turn_error_persists_partial_and_reports_code(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    controller.engine.stream = _fake_stream(
        [
            AgentStartEvent(model="m", message_count=1, max_iterations=50),
            AgentErrorEvent(message="boom", code="tool"),
        ]
    )

    outcome = await controller.run_turn("q")

    assert outcome.status == "error"
    assert outcome.error_code == "tool"
    # User turn persisted so the conversation is not lost.
    assert len(controller.tree.nodes) == 1
    assert "auth" not in " ".join(m for _, m in sink.notifications)
    assert controller.current_node_id is not None


@pytest.mark.asyncio
async def test_run_turn_auth_error_adds_actionable_hint(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    controller.engine.stream = _fake_stream(
        [
            AgentStartEvent(model="m", message_count=1, max_iterations=50),
            AgentErrorEvent(message="401", code="auth"),
        ]
    )

    outcome = await controller.run_turn("q")

    assert outcome.status == "error"
    assert any(
        kind == "warn" and "/setup" in message for kind, message in sink.notifications
    )


@pytest.mark.asyncio
async def test_cancel_mid_stream_saves_partial_progress(tmp_path) -> None:
    import asyncio

    controller, sink = _make_controller(tmp_path)

    async def _slow_stream(path, config):
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield AgentTokenEvent(content="part")
        await asyncio.sleep(3600)  # interrupted by cancel

    controller.engine.stream = _slow_stream

    task = asyncio.create_task(controller.run_turn("q"))
    await asyncio.sleep(0.1)  # let the stream start
    assert controller.is_running
    assert controller.cancel_current() is True

    outcome = await task

    assert outcome.status == "cancelled"
    assert "warn" in [kind for kind, _ in sink.notifications], (
        "partial save notification expected"
    )
    assert sink.partial_captures == 1
    assert sink.flushes == 1
    # User node kept.
    assert len(controller.tree.nodes) >= 1


def test_cancel_without_run_is_noop(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    assert controller.cancel_current() is False
    assert controller.is_running is False


# ── Reasoning persistence ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reasoning_persisted_on_assistant_node(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    sink.reasoning = "deep thoughts..."
    controller.engine.stream = _fake_stream(
        [
            AgentStartEvent(model="m", message_count=1, max_iterations=50),
            AgentTokenEvent(content="a"),
            _done_event("a"),
        ]
    )

    await controller.run_turn("q")

    node = controller.tree.nodes[controller.current_node_id]
    assert node.metadata["reasoning"] == "deep thoughts..."
    assert sink.reasoning == ""  # popped exactly once


# ── Metrics and context ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_updated_from_steps(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    now = datetime.datetime.now()
    result = AgentRunResult(
        final_content="a",
        history=[
            Message(role="user", content="q"),
            Message(role="assistant", content="a"),
        ],
        input_messages=[Message(role="user", content="q")],
        steps=[
            RunStep(
                kind="llm",
                started_at=now,
                ended_at=now,
                duration_ms=100,
                usage=TokenUsage(input=10, output=5),
                cost_usd=0.01,
            )
        ],
    )
    # The real engine emits an AgentStepDoneEvent per step as it runs;
    # metrics are accumulated live from those events (I-88), so the fake
    # stream must mirror that — the done event's steps list is not the
    # accumulation source (that would double-count).
    controller.engine.stream = _fake_stream(
        [
            AgentStartEvent(model="m", message_count=1, max_iterations=50),
            AgentStepDoneEvent(step=result.steps[0]),
            AgentDoneEvent(result=result),
        ]
    )

    await controller.run_turn("q")

    assert controller.session_metrics.step_count == 1
    assert controller.session_metrics.total_output_tokens == 5
    assert controller.session_metrics.total_cost_usd == pytest.approx(0.01)


def test_context_window_passthrough(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    assert controller.context_window == 128_000  # default before first run
    controller._context_window = 262_144
    assert controller.context_window == 262_144


# ── Model / provider switching ───────────────────────────────────────────────


async def test_set_model_rebuilds_engine_and_extras(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    controller.config.subagent_model = ""  # no explicit override in test
    fake_engine = SimpleNamespace(
        context=SimpleNamespace(extra={}), tools=controller.tools
    )
    with patch("phoson_cli.controller.AgentEngine", return_value=fake_engine):
        await controller.set_model("other-model")
    assert controller.current_model == "other-model"
    assert controller.config.model == "other-model"
    assert controller.engine is fake_engine
    assert (
        controller.engine.context.extra["default_model"] == "other-model"
    )  # subagent model follows


async def test_set_model_reuse_engine_applies_model_in_place(tmp_path) -> None:
    """A pure model switch must not rebuild the runtime or reload plugins.

    The chat client, tools and plugins are model-independent (the model rides
    on each request's ``ModelConfig``), so ``reuse_engine`` updates only the
    summarizer's model and the sub-agent defaults in ``context.extra``.
    """
    controller, _ = _make_controller(tmp_path)
    controller.config.subagent_model = ""
    engine = controller.engine
    engine._loaded_plugins = [object()]  # a plugin that must NOT be closed

    with (
        patch.object(
            controller._cw_resolver, "resolve", AsyncMock(return_value=128_000)
        ),
        patch("phoson_cli.controller.AgentEngine") as m_engine,
        patch("phoson_cli.controller.build_chat") as m_chat,
        patch("phoson_cli.controller.close_plugins", new=AsyncMock()) as m_close,
    ):
        await controller.set_model("other-model", reuse_engine=True)

    m_engine.assert_not_called()
    m_chat.assert_not_called()
    m_close.assert_not_called()
    assert controller.engine is engine
    assert controller.current_model == "other-model"
    assert controller.config.model == "other-model"
    assert controller.summarizer.model == "other-model"
    assert controller.engine.context.extra["main_model"] == "other-model"
    assert controller.engine.context.extra["default_model"] == "other-model"


async def test_set_model_reuse_engine_keeps_plugins_across_provider(tmp_path) -> None:
    """A provider switch rebuilds only the chat client, not plugins/tools.

    ``reuse_engine`` keeps the plugin/tool/middleware layer (so MCP
    subprocesses and monitors are not restarted) and swaps in a fresh
    provider-specific chat client; the engine is rebound in place.
    """
    controller, _ = _make_controller(tmp_path)
    engine = controller.engine
    engine._loaded_plugins = [object()]  # must NOT be closed
    new_chat = MagicMock(aclose=AsyncMock())

    with (
        patch.object(
            controller._cw_resolver, "resolve", AsyncMock(return_value=128_000)
        ),
        patch("phoson_cli.controller.build_chat", return_value=new_chat),
        patch("phoson_cli.controller.load_models_file", return_value={}),
        patch("phoson_cli.controller.close_plugins", new=AsyncMock()) as m_close,
    ):
        await controller.set_model("gpt-4o", provider="openai", reuse_engine=True)
        await asyncio.sleep(0)  # let the scheduled old-chat close run

    assert controller.config.provider == "openai"
    assert controller.engine is engine  # plugins/tools/middlewares kept
    m_close.assert_not_called()
    assert controller.chat is new_chat
    assert controller.engine.chat is new_chat
    assert controller.engine.context.extra["chat"] is new_chat
    assert controller.summarizer.chat is new_chat
    assert controller.summarizer.provider == "openai"
    assert controller.summarizer.model == "gpt-4o"


async def test_set_provider_reuse_keeps_loaded_plugins(tmp_path) -> None:
    """A provider switch must not tear down/reload an already-loaded plugin."""

    class TrackedPlugin(Plugin):
        def __init__(self) -> None:
            self.cleanup_calls = 0

        @property
        def name(self) -> str:
            return "tracked"

        def cleanup(self) -> None:
            self.cleanup_calls += 1

    plugin = TrackedPlugin()
    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    sink = FakeSink()
    new_chat = MagicMock(aclose=AsyncMock())
    with (
        patch("phoson_cli.controller.build_plugin_specs", return_value=[plugin]),
        patch(
            "phoson_cli.controller.build_chat",
            return_value=MagicMock(aclose=AsyncMock()),
        ),
    ):
        controller = SessionController(config, sink)
    assert plugin in controller.engine._loaded_plugins

    with (
        patch.object(
            controller._cw_resolver, "resolve", AsyncMock(return_value=128_000)
        ),
        patch("phoson_cli.controller.build_chat", return_value=new_chat),
        patch("phoson_cli.controller.load_models_file", return_value={}),
    ):
        await controller.set_provider("openai")
        await asyncio.sleep(0)  # let the scheduled old-chat close run

    assert controller.engine._loaded_plugins == [plugin]
    assert plugin.cleanup_calls == 0
    assert controller.chat is new_chat
    assert controller.config.provider == "openai"


async def test_set_model_reuse_engine_same_model_still_rebuilds(tmp_path) -> None:
    """A same-model ``reuse_engine`` call rebuilds (e.g. ``/mcp`` reloads)."""
    controller, _ = _make_controller(tmp_path)
    fake_engine = SimpleNamespace(
        context=SimpleNamespace(extra={}), tools=controller.tools
    )
    with (
        patch.object(
            controller._cw_resolver, "resolve", AsyncMock(return_value=128_000)
        ),
        patch(
            "phoson_cli.controller.build_chat",
            return_value=MagicMock(aclose=AsyncMock()),
        ),
        patch("phoson_cli.controller.AgentEngine", return_value=fake_engine),
    ):
        await controller.set_model("test-model", reuse_engine=True)

    assert controller.engine is fake_engine


async def test_set_model_reuse_engine_refreshes_token_estimator(tmp_path) -> None:
    """#242: crossing into openai must rebind the summarizer's tiktoken encoding.

    The estimator is built once from the provider; a stale ``cl100k_base``
    after switching to ``openai`` (``o200k_base``) skews the context meter and
    the auto-compaction gate.
    """
    controller, _ = _make_controller(tmp_path)  # provider ollama -> cl100k
    assert controller.summarizer._estimator._encoding.name == "cl100k_base"

    with (
        patch.object(
            controller._cw_resolver, "resolve", AsyncMock(return_value=128_000)
        ),
        patch(
            "phoson_cli.controller.build_chat",
            return_value=MagicMock(aclose=AsyncMock()),
        ),
        patch("phoson_cli.controller.load_models_file", return_value={}),
    ):
        await controller.set_model("gpt-4o", provider="openai", reuse_engine=True)

    assert controller.summarizer.provider == "openai"
    assert controller.summarizer._estimator._encoding.name == "o200k_base"


async def test_rebuild_engine_refreshes_token_estimator(tmp_path) -> None:
    """#242: the full rebuild path refreshes the estimator too (was pre-existing)."""
    controller, _ = _make_controller(tmp_path)
    controller.config.provider = "openai"

    with (
        patch(
            "phoson_cli.controller.build_chat",
            return_value=MagicMock(aclose=AsyncMock()),
        ),
        patch("phoson_cli.controller.load_models_file", return_value={}),
    ):
        controller._rebuild_engine()

    assert controller.summarizer._estimator._encoding.name == "o200k_base"


def test_rebind_cw_resolver_updates_endpoint(tmp_path) -> None:
    """#242: the header resolver's vLLM endpoint follows a config change."""
    controller, _ = _make_controller(tmp_path)

    with patch.object(controller, "_vllm_base_url", return_value="http://new:9999/v1"):
        controller._rebind_cw_resolver()

    assert controller._cw_resolver._vllm_base_url == "http://new:9999/v1"


async def test_set_model_refreshes_context_window(tmp_path) -> None:
    """Regression: the header's indicator must update on /model, not just

    after the next turn — set_model has to (re)resolve the context window
    for the newly selected model immediately. Uses models that hit the
    resolver's static registry so the test never touches the network.
    """
    controller, _ = _make_controller(tmp_path)
    controller.config.provider = "openai"
    fake_engine = SimpleNamespace(
        context=SimpleNamespace(extra={}), tools=controller.tools
    )
    with (
        patch(
            "phoson_cli.controller.build_chat",
            return_value=MagicMock(aclose=AsyncMock()),
        ),
        patch("phoson_cli.controller.load_models_file", return_value={}),
        patch("phoson_cli.controller.AgentEngine", return_value=fake_engine),
    ):
        await controller.set_model("gpt-4o")
        assert controller.context_window == 128_000

        controller.config.provider = "anthropic"
        await controller.set_model("claude-sonnet-4-6")
    assert controller.context_window == 200_000


async def test_set_provider_uses_default_model_when_configured(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    data = {"providers": {"openrouter": {"default_model": "qwen3.8-27b"}}}
    fake_engine = SimpleNamespace(
        context=SimpleNamespace(extra={}), tools=controller.tools
    )
    controller._cw_resolver.resolve = AsyncMock(return_value=64_000)
    with (
        patch(
            "phoson_cli.controller.build_chat",
            return_value=MagicMock(aclose=AsyncMock()),
        ),
        patch("phoson_cli.controller.load_models_file", return_value=data),
        patch("phoson_cli.controller.AgentEngine", return_value=fake_engine),
    ):
        await controller.set_provider("openrouter")
    assert controller.config.provider == "openrouter"
    assert controller.current_model == "qwen3.8-27b"
    assert controller.context_window == 64_000


# ── Sessions ─────────────────────────────────────────────────────────────────


async def test_new_session_resets_state(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    first_id = controller.tree.session_id
    controller.tree.append(parent_id=None, message=Message(role="user", content="x"))

    await controller.new_session()

    assert controller.tree.session_id != first_id
    assert controller.current_node_id is None
    assert sink.session_ids[-1] == controller.tree.session_id


async def test_new_session_resets_context_and_stamps_current_cwd(
    tmp_path, monkeypatch
) -> None:
    controller, _ = _make_controller(tmp_path)
    cwd = tmp_path / "new-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    controller._context_tokens = 1234

    await controller.new_session()

    assert controller.tree.cwd == str(cwd)
    assert controller.context_tokens == 0


@pytest.mark.asyncio
async def test_load_session_replays_tail_and_metrics(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    # Create and save a session with one turn.
    controller.tree.append(parent_id=None, message=Message(role="user", content="hi"))
    controller.tree.append(
        parent_id=controller.current_node_id,
        message=Message(role="assistant", content="hello"),
    )
    await controller.storage.save(controller.tree)
    await controller.storage.save_meta(
        controller.tree.session_id,
        {
            "total_cost_usd": 0.5,
            "total_input_tokens": 0,
            "total_output_tokens": 42,
            "step_count": 3,
            "last_model": "saved-model",
        },
    )
    saved_id = controller.tree.session_id

    controller2, sink2 = _make_controller(tmp_path)
    outcome = await controller2.load_session(saved_id)

    assert outcome.ok
    assert controller2.tree.session_id == saved_id
    assert len(controller2.tree.nodes) == 2
    # Cursor on the newest leaf.
    node = controller2.tree.nodes[controller2.current_node_id]
    assert node.message.role == "assistant"
    # Metrics restored.
    assert controller2.session_metrics.total_cost_usd == 0.5
    assert controller2.session_metrics.step_count == 3
    assert controller2.session_metrics.last_model == "saved-model"
    # History replayed through the sink (#56: full path, no fixed tail).
    assert len(sink2.history_calls) == 1
    path, tail = sink2.history_calls[0]
    assert tail is None and path[-1].content == "hello"
    # Full path replayed, not a fixed-size slice of it.
    expected = controller2.tree.get_path(controller2.current_node_id)
    assert len(path) == len(expected)


@pytest.mark.asyncio
async def test_load_session_clears_attachments_and_recomputes_context(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    loaded = controller.tree.__class__.new(session_id="loaded-session")
    loaded.append(parent_id=None, message=Message(role="user", content="loaded"))
    await controller.storage.save(loaded)
    controller.attachments._pending.append(object())  # type: ignore[arg-type]
    controller._context_tokens = 987654

    outcome = await controller.load_session(loaded.session_id)

    assert outcome.ok
    assert len(controller.attachments) == 0
    assert controller.context_tokens == controller.estimate_active_path()
    assert controller.context_tokens != 987654


@pytest.mark.asyncio
async def test_load_session_reads_metrics_from_tree_not_list_meta(tmp_path) -> None:
    """Resuming metrics come from the loaded tree, not a full re-listing.

    Regression guard for the O(all-session-bytes) resume: ``load_session``
    used to call ``storage.list_meta()`` (which parses every session file)
    just to find the one it had already loaded. The tree's own ``session_meta``
    record is authoritative, so ``list_meta`` must not be touched at all.
    """
    controller, _ = _make_controller(tmp_path)

    loaded = controller.tree.__class__.new(session_id="candidate-session")
    loaded.append(parent_id=None, message=Message(role="user", content="candidate"))
    loaded.update_session_meta(
        total_cost=2.5,
        total_tokens=100,
        total_input_tokens=60,
        total_output_tokens=40,
        step_count=3,
        last_model="test-model",
    )
    await controller.storage.save(loaded)
    controller.storage.list_meta = AsyncMock(side_effect=RuntimeError("meta failed"))

    outcome = await controller.load_session(loaded.session_id)

    assert outcome.ok
    assert controller.session_metrics.total_cost_usd == 2.5
    assert controller.session_metrics.total_input_tokens == 60
    assert controller.session_metrics.total_output_tokens == 40
    assert controller.session_metrics.step_count == 3
    assert controller.session_metrics.last_model == "test-model"
    controller.storage.list_meta.assert_not_called()


@pytest.mark.asyncio
async def test_load_session_restores_view_when_replay_sink_fails(tmp_path) -> None:
    sink = TransactionalFakeSink()
    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        controller = SessionController(config, sink)

    old_node = controller.tree.append(
        parent_id=None, message=Message(role="user", content="keep me")
    )
    controller.current_node_id = old_node.id
    controller.session_metrics.total_cost_usd = 7.5
    controller._context_tokens = 4321
    controller.attachments._pending.append(object())  # type: ignore[arg-type]
    old_tree = controller.tree
    old_metrics = controller.session_metrics
    old_block = object()
    sink.visible_blocks.append(old_block)

    loaded = controller.tree.__class__.new(session_id="replay-failure")
    loaded.append(parent_id=None, message=Message(role="user", content="candidate"))
    await controller.storage.save(loaded)
    sink.fail_replay = True

    outcome = await controller.load_session(loaded.session_id)

    assert not outcome.ok
    assert sink.reset_count == 1
    assert sink.restore_count == 1
    assert sink.visible_blocks == [old_block]
    assert sink.session_ids[-1] == old_tree.session_id
    assert controller.tree is old_tree
    assert controller.current_node_id == old_node.id
    assert controller.session_metrics is old_metrics
    assert controller.session_metrics.total_cost_usd == 7.5
    assert controller.context_tokens == 4321
    assert len(controller.attachments) == 1


@pytest.mark.asyncio
async def test_load_session_rollback_preserves_error_when_view_restore_fails(
    tmp_path, caplog
) -> None:
    class FailingRollbackSink(TransactionalFakeSink):
        old_session_id = ""

        def restore_session_view(self, snapshot: object) -> None:
            raise RuntimeError("restore failed")

        def set_session(self, session_id: str) -> None:
            if session_id == self.old_session_id:
                raise RuntimeError("set session failed")
            super().set_session(session_id)

    sink = FailingRollbackSink()
    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        controller = SessionController(config, sink)

    old_tree = controller.tree
    old_metrics = controller.session_metrics
    sink.old_session_id = old_tree.session_id
    controller._context_tokens = 55
    controller.attachments._pending.append(object())  # type: ignore[arg-type]
    loaded = controller.tree.__class__.new(session_id="rollback-errors")
    loaded.append(parent_id=None, message=Message(role="user", content="candidate"))
    await controller.storage.save(loaded)
    sink.fail_replay = True

    with caplog.at_level("WARNING"):
        outcome = await controller.load_session(loaded.session_id)

    assert not outcome.ok
    assert "replay failed" in outcome.message
    assert controller.tree is old_tree
    assert controller.session_metrics is old_metrics
    assert controller.context_tokens == 55
    assert len(controller.attachments) == 1
    assert "Could not restore session view" in caplog.text
    assert "Could not restore displayed session id" in caplog.text


@pytest.mark.asyncio
async def test_load_session_blocks_user_turn_until_switch_commits(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    loaded = controller.tree.__class__.new(session_id="loaded-before-user-turn")
    load_started = asyncio.Event()
    release_load = asyncio.Event()

    async def paused_load(session_id: str):
        assert session_id == loaded.session_id
        load_started.set()
        await release_load.wait()
        return loaded

    controller.storage.load = paused_load  # type: ignore[method-assign]
    controller.storage.list_meta = AsyncMock(return_value=[])
    controller._execute_turn = AsyncMock(return_value=SimpleNamespace(status="done"))

    load_task = asyncio.create_task(controller.load_session(loaded.session_id))
    await load_started.wait()
    turn_task = asyncio.create_task(controller.run_turn("after load"))
    await asyncio.sleep(0)

    controller._execute_turn.assert_not_awaited()
    release_load.set()
    assert (await load_task).ok
    await turn_task

    controller._execute_turn.assert_awaited_once()
    assert controller.tree.session_id == loaded.session_id


@pytest.mark.asyncio
async def test_load_preparation_keeps_previous_session_public_until_commit(
    tmp_path,
) -> None:
    controller, _ = _make_controller(tmp_path)
    old_tree = controller.tree
    old_node = old_tree.append(
        parent_id=None, message=Message(role="user", content="current")
    )
    controller.current_node_id = old_node.id
    session_id_provider = controller.engine.context.extra["session_id_provider"]

    loaded = controller.tree.__class__.new(session_id="locally-prepared-session")
    loaded.append(parent_id=None, message=Message(role="user", content="candidate"))
    preparation_started = asyncio.Event()
    release_preparation = asyncio.Event()

    async def paused_repair(candidate):
        preparation_started.set()
        await release_preparation.wait()
        return False

    controller.storage.load = AsyncMock(return_value=loaded)
    controller._repair_orphaned_run = paused_repair  # type: ignore[method-assign]

    load_task = asyncio.create_task(controller.load_session(loaded.session_id))
    await preparation_started.wait()

    assert controller.tree is old_tree
    assert controller.tree.session_id == old_tree.session_id
    assert controller.current_node_id == old_node.id
    assert session_id_provider() == old_tree.session_id

    release_preparation.set()
    assert (await load_task).ok
    assert controller.tree is loaded
    assert session_id_provider() == loaded.session_id


@pytest.mark.asyncio
async def test_load_session_blocks_wake_turn_until_switch_commits(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    loaded = controller.tree.__class__.new(session_id="loaded-before-wake-turn")
    load_started = asyncio.Event()
    release_load = asyncio.Event()

    async def paused_load(session_id: str):
        load_started.set()
        await release_load.wait()
        return loaded

    controller.storage.load = paused_load  # type: ignore[method-assign]
    controller.storage.list_meta = AsyncMock(return_value=[])
    controller._execute_turn = AsyncMock(return_value=SimpleNamespace(status="done"))
    drain = AsyncMock(return_value=[(object(), [object()])])

    with (
        patch("phoson_cli.controller.has_pending_wakes", return_value=True),
        patch("phoson_cli.controller.drain_all_wakes", drain),
        patch("phoson_cli.controller._render_wake_batches", return_value="wake"),
    ):
        load_task = asyncio.create_task(controller.load_session(loaded.session_id))
        await load_started.wait()
        wake_task = asyncio.create_task(controller._wake_loop_tick())
        await asyncio.sleep(0)

        controller._execute_turn.assert_not_awaited()
        release_load.set()
        assert (await load_task).ok
        await wake_task

    controller._execute_turn.assert_awaited_once()
    assert drain.await_args.args[1] == loaded.session_id


@pytest.mark.asyncio
async def test_new_session_waits_for_active_user_turn(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    original_id = controller.tree.session_id
    turn_started = asyncio.Event()
    release_turn = asyncio.Event()

    async def paused_turn(*args, **kwargs):
        turn_started.set()
        await release_turn.wait()
        return SimpleNamespace(status="done")

    controller._execute_turn = paused_turn  # type: ignore[method-assign]
    turn_task = asyncio.create_task(controller.run_turn("active"))
    await turn_started.wait()
    new_task = asyncio.create_task(controller.new_session())
    await asyncio.sleep(0)

    assert controller.tree.session_id == original_id
    assert not new_task.done()
    release_turn.set()
    await turn_task
    await new_task

    assert controller.tree.session_id != original_id


@pytest.mark.asyncio
async def test_sync_new_session_rejects_while_turn_lock_is_held(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    turn_started = asyncio.Event()
    release_turn = asyncio.Event()

    async def paused_turn(*args, **kwargs):
        turn_started.set()
        await release_turn.wait()
        return SimpleNamespace(status="done")

    controller._execute_turn = paused_turn  # type: ignore[method-assign]
    turn_task = asyncio.create_task(controller.run_turn("active"))
    await turn_started.wait()

    with pytest.raises(RuntimeError, match="turn is active"):
        controller.new_session_now()

    release_turn.set()
    await turn_task


@pytest.mark.asyncio
async def test_cancel_current_cancels_authoritative_turn_during_preparation(
    tmp_path,
) -> None:
    controller, _ = _make_controller(tmp_path)
    preparation_started = asyncio.Event()

    async def paused_context_refresh() -> None:
        preparation_started.set()
        await asyncio.Event().wait()

    controller._refresh_context_window = paused_context_refresh  # type: ignore[method-assign]
    run_task = asyncio.create_task(controller.run_turn("cancel before stream"))
    await preparation_started.wait()

    assert controller.is_running
    assert controller.current_task is None
    assert controller.cancel_current() is True
    outcome = await run_task

    assert outcome.status == "cancelled"
    assert controller.is_running is False


async def _seed_residual_engine_history(controller) -> None:
    """Complete real engine turns; only the provider transport is fake."""

    async def chat_stream(history, config, tools=None):
        yield LLMDoneEvent(content="residual answer from previous session")

    controller.chat.stream = chat_stream
    controller._refresh_context_window = AsyncMock()
    controller.summarizer._resolver.resolve = AsyncMock(return_value=128_000)
    # Longer than the loaded path too, so slicing by base_count cannot hide
    # the leak in either session-switch case.
    for index in range(3):
        assert (
            await controller.run_turn(f"previous question {index}")
        ).status == "done"
    assert len(controller.engine.get_partial_history()) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["new", "new_now", "load"])
@pytest.mark.parametrize(
    "cancel_at",
    ["refresh", "task_pending", "engine_lock", "start_middleware", "started"],
)
async def test_cancel_after_session_switch_never_imports_residual_history(
    tmp_path, monkeypatch, switch, cancel_at
) -> None:
    controller, sink = _make_controller(tmp_path)
    await _seed_residual_engine_history(controller)
    engine = controller.engine
    residual = engine.get_partial_history()
    previous_id = controller.tree.session_id
    previous_run_id = controller.tree.last_run_id
    expected = []
    if switch == "load":
        saved, _ = _make_controller(tmp_path)
        expected = [
            Message(role="user", content="loaded question"),
            Message(role="assistant", content="loaded answer"),
        ]
        saved.tree.append_many(None, expected)
        await saved.storage.save(saved.tree)
        assert (await controller.load_session(saved.tree.session_id)).ok
    elif switch == "new_now":
        controller.new_session_now()
    else:
        await controller.new_session()
    assert controller.engine is engine
    assert engine.get_partial_history() == residual
    sink.events.clear()
    reached = asyncio.Event()

    async def pause_refresh():
        reached.set()
        await asyncio.Event().wait()

    class PauseStart(AgentMiddleware):
        async def on_agent_event(self, event):
            if isinstance(event, AgentStartEvent):
                reached.set()
                await asyncio.Event().wait()

    async def pause_chat(history, config, tools=None):
        reached.set()
        await asyncio.Event().wait()
        yield LLMDoneEvent(content="must not finish")

    if cancel_at == "refresh":
        controller._refresh_context_window = pause_refresh
    elif cancel_at == "start_middleware":
        engine.middlewares.insert(0, PauseStart())
    elif cancel_at == "started":
        controller.chat.stream = pause_chat
    else:
        # Hold the real engine before _stream_impl initializes its history.
        await engine._running_lock.acquire()
        create_task = asyncio.create_task

        def observe_stream_task(coro, **kwargs):
            task = create_task(coro, **kwargs)
            if coro.cr_code.co_name == "consume":
                reached.set()
                if cancel_at == "task_pending":
                    task.cancel()  # no byte of the consumer has run yet
            return task

        monkeypatch.setattr(asyncio, "create_task", observe_stream_task)

    task = asyncio.create_task(controller.run_turn("new question to preserve"))
    try:
        await asyncio.wait_for(reached.wait(), timeout=5)
        if cancel_at != "task_pending":
            assert controller.cancel_current()
        outcome = await asyncio.wait_for(task, timeout=5)
    finally:
        if cancel_at in {"task_pending", "engine_lock"}:
            engine._running_lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert sink.user_messages[-1][0] == "new question to preserve"
    expected = [*expected, sink.user_messages[-1][1]]
    assert outcome.status == "cancelled"
    assert not controller.is_running
    assert controller.current_task is None
    assert controller.tree.get_path(controller.current_node_id) == expected
    assert len(controller.tree.nodes) == len(expected)
    assert controller.tree.status == "aborted"
    assert controller.tree.last_run_id
    assert controller.tree.last_run_id != previous_run_id
    loaded = await controller.storage.load(controller.tree.session_id)
    assert loaded.get_path(controller.current_node_id) == expected
    assert len(loaded.nodes) == len(expected)
    assert loaded.status == "aborted"
    assert loaded.last_run_id == controller.tree.last_run_id
    meta = next(
        m
        for m in await controller.storage.list_meta()
        if str(m.id) == controller.tree.session_id
    )
    assert meta.status == "aborted"
    assert meta.last_run_id == controller.tree.last_run_id
    previous = await controller.storage.load(previous_id)
    assert len(previous.nodes) == 6
    assert previous.status == "completed"
    assert sink.partial_captures == 1
    assert any("Partial progress saved" in text for _, text in sink.notifications)
    assert any(isinstance(e, AgentStartEvent) for e in sink.events) == (
        cancel_at == "started"
    )


@pytest.mark.asyncio
async def test_cancel_initialized_turn_keeps_current_history_not_previous(
    tmp_path,
) -> None:
    controller, _ = _make_controller(tmp_path)
    await _seed_residual_engine_history(controller)
    await controller.new_session()
    reached = asyncio.Event()

    class PauseDone(AgentMiddleware):
        async def on_agent_event(self, event):
            if isinstance(event, AgentDoneEvent):
                # The real engine has appended the current assistant message,
                # but the controller has not received the terminal event yet.
                reached.set()
                await asyncio.Event().wait()

    async def chat_stream(history, config, tools=None):
        yield LLMDoneEvent(content="current answer worth preserving")

    controller.chat.stream = chat_stream
    controller.engine.middlewares.insert(0, PauseDone())
    task = asyncio.create_task(controller.run_turn("current question"))
    try:
        await asyncio.wait_for(reached.wait(), timeout=5)
        expected = controller.engine.get_partial_history()
        assert len(expected) == 2
        assert "current answer worth preserving" in str(expected[-1].content)
        assert controller.cancel_current()
        assert (await asyncio.wait_for(task, timeout=5)).status == "cancelled"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert controller.tree.get_path(controller.current_node_id) == expected
    loaded = await controller.storage.load(controller.tree.session_id)
    assert loaded.get_path(controller.current_node_id) == expected
    assert loaded.status == "aborted"


@pytest.mark.asyncio
async def test_cancel_current_tracks_wake_owner_and_clears_it(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    wake_started = asyncio.Event()

    async def paused_wake(*args, **kwargs):
        wake_started.set()
        await asyncio.Event().wait()

    controller._execute_turn = paused_wake  # type: ignore[method-assign]
    with (
        patch(
            "phoson_cli.controller.drain_all_wakes",
            new=AsyncMock(return_value=[(object(), [object()])]),
        ),
        patch("phoson_cli.controller._render_wake_batches", return_value="wake"),
    ):
        wake_task = asyncio.create_task(controller._run_wake_turn([object()]))
        await wake_started.wait()
        assert controller.cancel_current() is True
        with pytest.raises(asyncio.CancelledError):
            await wake_task

    assert controller.is_running is False


@pytest.mark.asyncio
async def test_cancel_current_does_not_cancel_calling_turn_owner(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)

    async def self_probe(*args, **kwargs):
        assert controller.cancel_current() is False
        return SimpleNamespace(status="done")

    controller._execute_turn = self_probe  # type: ignore[method-assign]

    outcome = await controller.run_turn("self probe")

    assert outcome.status == "done"


@pytest.mark.asyncio
async def test_load_missing_session_reports_error(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    outcome = await controller.load_session("doesnotexist000000000000")
    assert outcome.ok is False
    assert sink.notifications and sink.notifications[-1][0] == "error"


# ── Undo / labels / tree ─────────────────────────────────────────────────────


def test_undo_moves_cursor_before_last_user_turn(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    n1 = controller.tree.append(
        parent_id=None, message=Message(role="user", content="first")
    )
    n2 = controller.tree.append(
        parent_id=n1.id, message=Message(role="assistant", content="one")
    )
    n3 = controller.tree.append(
        parent_id=n2.id, message=Message(role="user", content="second")
    )
    controller.current_node_id = n3.id

    ok, node_id = controller.undo_last_turn()

    assert ok
    assert node_id == n2.id
    assert controller.current_node_id == n2.id


def test_undo_single_turn_has_nothing_to_undo(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    n1 = controller.tree.append(
        parent_id=None, message=Message(role="user", content="only")
    )
    controller.current_node_id = n1.id

    ok, message = controller.undo_last_turn()

    assert ok is False
    assert "nothing to undo" in message.lower()


def test_label_current_node(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    n1 = controller.tree.append(
        parent_id=None, message=Message(role="user", content="x")
    )
    controller.current_node_id = n1.id
    controller.label_current_node("checkpoint")
    assert controller.tree.nodes[n1.id].metadata.get("label") == "checkpoint"


def test_find_latest_node_id_prefers_newest_leaf(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    n1 = controller.tree.append(
        parent_id=None, message=Message(role="user", content="root")
    )
    n2 = controller.tree.append(
        parent_id=n1.id, message=Message(role="assistant", content="a")
    )
    assert controller.find_latest_node_id() == n2.id


# ── Shutdown ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_closes_chat_and_plugins(tmp_path) -> None:
    class _Plugin:
        def __init__(self) -> None:
            self.cleaned = 0

        def cleanup(self) -> None:
            self.cleaned += 1

    controller, _ = _make_controller(tmp_path)
    controller.chat = MagicMock()
    controller.chat.aclose = AsyncMock()
    plugin = _Plugin()
    controller.engine = SimpleNamespace(_loaded_plugins=[plugin])

    await controller.shutdown()

    controller.chat.aclose.assert_awaited_once()
    assert plugin.cleaned == 1


# ── System prompt ────────────────────────────────────────────────────────────


def test_system_prompt_lists_loaded_tools(tmp_path) -> None:
    controller, _ = _make_controller(tmp_path)
    prompt = controller.build_system_prompt()
    assert "bash" in prompt
    assert "Phos" in prompt


@pytest.mark.asyncio
async def test_load_session_replay_caps_very_long_history(tmp_path) -> None:
    """#56: paths beyond MAX_RESUME_REPLAY_MESSAGES replay truncated,
    with the tail count so render_history announces the truncation."""
    from phoson_cli.controller import MAX_RESUME_REPLAY_MESSAGES

    controller, sink = _make_controller(tmp_path)
    parent = None
    for i in range(MAX_RESUME_REPLAY_MESSAGES + 25):
        node = controller.tree.append(
            parent_id=parent,
            message=Message(
                role="user" if i % 2 == 0 else "assistant", content=f"m{i}"
            ),
        )
        parent = node.id
    await controller.storage.save(controller.tree)
    saved_id = controller.tree.session_id

    controller2, sink2 = _make_controller(tmp_path)
    outcome = await controller2.load_session(saved_id)

    assert outcome.ok
    assert len(sink2.history_calls) == 1
    path, tail = sink2.history_calls[0]
    assert tail == MAX_RESUME_REPLAY_MESSAGES
    assert len(path) == MAX_RESUME_REPLAY_MESSAGES + 25


# ── I-91: mid-run compaction rebases the tree ─────────────────────────────────


@pytest.mark.asyncio
async def test_run_turn_rebases_tree_on_mid_run_compaction(tmp_path) -> None:
    """When the summarizer compacts mid-run, the tree must be grafted as a
    new root branch (like manual /compact) instead of duplicating the
    compacted tail onto the old path (I-91)."""
    controller, sink = _make_controller(tmp_path)

    # Seed a long-ish history so the run has something to compact.
    seed = [Message(role="user", content=f"old {i}") for i in range(6)] + [
        Message(role="assistant", content=f"ans {i}") for i in range(6)
    ]
    controller.tree.append_many(None, seed)
    leaves = controller.tree.get_leaves()
    controller.current_node_id = max(
        leaves, key=lambda nid: controller.tree.nodes[nid].created_at
    )
    old_path_len = len(controller.tree.get_path(controller.current_node_id))

    # The summarizer "compacted" mid-run: the event is queued *during*
    # the stream (as the real middleware does) and the done event's
    # history is the compacted list.
    compacted = [
        Message(role="user", content="[Conversation summary up to this point: S]"),
        Message(role="user", content="ans 4"),
        Message(role="assistant", content="ans 5"),
    ]
    from phoson_agent.plugins.summarizer import SummarizationEvent

    def _stream(path, config):
        async def _gen():
            controller.summarizer._pending_compact_events.append(
                SummarizationEvent(
                    original_tokens=9000,
                    compacted_tokens=2000,
                    messages_removed=10,
                    summary_length=100,
                )
            )
            yield AgentStartEvent(model="m", message_count=len(path), max_iterations=50)
            yield AgentDoneEvent(
                result=AgentRunResult(
                    final_content="done",
                    history=list(compacted),
                    input_messages=list(path),
                    steps=[],
                )
            )

        return _gen()

    controller.engine.stream = _stream
    outcome = await controller.run_turn("next question")

    assert outcome.status == "done"

    # The tree holds the compacted history as a new root branch.
    path = controller.tree.get_path(controller.current_node_id)
    assert len(path) == len(compacted)
    assert any("Conversation summary" in str(m.content) for m in path)
    # The old (pre-compaction) branch is still intact in the tree.
    assert controller.tree.node_count() >= old_path_len + len(compacted)
    # The front end was told about the compaction.
    assert any("auto-compacted" in msg for _kind, msg in sink.notifications)
    # The header estimate was refreshed from the compacted path.
    assert controller._context_tokens > 0


@pytest.mark.asyncio
async def test_run_turn_without_compaction_appends_tail(tmp_path) -> None:
    """No compaction events → the normal tail-append path is used (the
    I-91 rebase must not fire spuriously)."""
    controller, sink = _make_controller(tmp_path)

    async def stream(path, config):
        yield AgentStartEvent(model="m", message_count=len(path), max_iterations=50)
        yield AgentDoneEvent(
            result=AgentRunResult(
                final_content="hello",
                history=[
                    Message(role="user", content="q"),
                    Message(role="assistant", content="hello"),
                ],
                input_messages=[Message(role="user", content="q")],
                steps=[],
            )
        )

    controller.engine.stream = stream
    outcome = await controller.run_turn("q")

    assert outcome.status == "done"
    path = controller.tree.get_path(controller.current_node_id)
    # user turn + assistant answer appended to the (empty) root.
    assert [m.role for m in path] == ["user", "assistant"]
    assert not any("auto-compacted" in msg for _kind, msg in sink.notifications)


@pytest.mark.asyncio
async def test_estimate_active_path_counts_system_and_tools(tmp_path) -> None:
    """The header indicator must use the same conservative estimate as
    the gate (messages + system prompt + tool schemas) (I-91)."""
    controller, _ = _make_controller(tmp_path)

    controller.tree.append_many(
        None,
        [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ],
    )
    leaves = controller.tree.get_leaves()
    controller.current_node_id = max(
        leaves, key=lambda nid: controller.tree.nodes[nid].created_at
    )

    baseline = controller.estimate_active_path()
    assert baseline > 0

    # The estimate must include the tool schemas: with no tools at all
    # the number is strictly smaller.
    saved = controller.summarizer.tool_definitions
    controller.summarizer.tool_definitions = None
    without_tools = controller.estimate_active_path()
    controller.summarizer.tool_definitions = saved
    assert without_tools < baseline


# ── I-88: live metrics (cost + tokens track the run, no double count) ────────


def _step(kind="llm", cost=0.0, input_tokens=0, output_tokens=0):
    now = datetime.datetime.now()
    return RunStep(
        kind=kind,
        started_at=now,
        ended_at=now,
        duration_ms=10,
        usage=TokenUsage(input=input_tokens, output=output_tokens),
        cost_usd=cost,
    )


@pytest.mark.asyncio
async def test_live_metrics_accumulate_per_step_and_no_double_count(tmp_path) -> None:
    """Cost/tokens are folded into session_metrics as each step completes
    (live), and _finalize_run must NOT re-add them — total equals the sum
    of the steps exactly once (I-88)."""
    controller, _sink = _make_controller(tmp_path)

    step1 = _step(cost=0.01, input_tokens=10, output_tokens=5)
    step2 = _step(cost=0.02, input_tokens=20, output_tokens=8)
    step3 = _step(kind="tool", cost=0.0, input_tokens=0, output_tokens=0)

    result = AgentRunResult(
        final_content="a",
        history=[
            Message(role="user", content="q"),
            Message(role="assistant", content="a"),
        ],
        input_messages=[Message(role="user", content="q")],
        steps=[step1, step2, step3],
    )

    # Snapshot the live totals right before the done event is emitted —
    # all three steps must already be folded in (live, not at the end).
    live_totals: dict[str, object] = {}

    async def stream(path, config):
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield AgentStepDoneEvent(step=step1)
        yield AgentStepDoneEvent(step=step2)
        yield AgentStepDoneEvent(step=step3)
        live_totals["cost"] = controller.session_metrics.total_cost_usd
        live_totals["steps"] = controller.session_metrics.step_count
        yield AgentDoneEvent(result=result)

    controller.engine.stream = stream
    await controller.run_turn("q")

    # Live: by the time the done event was emitted, all 3 steps were in.
    assert live_totals["cost"] == pytest.approx(0.03)
    assert live_totals["steps"] == 3

    # Final: still exactly the sum — no double count from _finalize_run.
    assert controller.session_metrics.step_count == 3
    assert controller.session_metrics.total_cost_usd == pytest.approx(0.03)
    assert controller.session_metrics.total_input_tokens == 30
    assert controller.session_metrics.total_output_tokens == 13


@pytest.mark.asyncio
async def test_live_context_tokens_track_in_flight_history(tmp_path) -> None:
    """The context indicator is refreshed against the engine's in-flight
    history as steps complete, so it grows with the run (I-88)."""
    controller, _sink = _make_controller(tmp_path)

    # Seed the engine's in-flight history so the estimate has something to
    # count (the real engine appends messages as the run progresses).
    controller.engine._history = [
        Message(role="user", content="word " * 200),
        Message(role="assistant", content="word " * 200),
    ]

    step = _step(cost=0.01, input_tokens=10, output_tokens=5)
    result = AgentRunResult(
        final_content="a",
        history=[
            Message(role="user", content="q"),
            Message(role="assistant", content="a"),
        ],
        input_messages=[Message(role="user", content="q")],
        steps=[step],
    )

    seen_context_tokens: list[int] = []

    async def stream(path, config):
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield AgentStepDoneEvent(step=step)
        seen_context_tokens.append(controller._context_tokens)
        yield AgentDoneEvent(result=result)

    controller.engine.stream = stream
    await controller.run_turn("q")

    # The live refresh saw the in-flight history (a non-trivial estimate).
    assert seen_context_tokens and seen_context_tokens[0] > 0
    # And the final indicator is also populated from the committed tree.
    assert controller._context_tokens > 0


@pytest.mark.asyncio
async def test_live_metrics_survive_error_and_cancel(tmp_path) -> None:
    """Steps that completed before a failure/cancel keep their cost/tokens
    in the session metrics (live accumulation), so partial work is not
    lost from the accounting (I-88)."""
    controller, _sink = _make_controller(tmp_path)

    step = _step(cost=0.05, input_tokens=10, output_tokens=5)

    async def stream(path, config):
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield AgentStepDoneEvent(step=step)
        yield AgentErrorEvent(message="boom", code="server_error", retryable=False)

    controller.engine.stream = stream
    outcome = await controller.run_turn("q")

    assert outcome.status == "error"
    assert controller.session_metrics.step_count == 1
    assert controller.session_metrics.total_cost_usd == pytest.approx(0.05)
    assert controller.session_metrics.total_output_tokens == 5
