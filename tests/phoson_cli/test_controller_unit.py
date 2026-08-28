"""Tests for the UI-independent SessionController.

The controller must run a full session lifecycle against a fake sink —
no prompt_toolkit, no Rich, no TTY. This is the guarantee that a new
front end is a sink, not a fork.
"""

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message, TokenUsage
from phoson_agent.models import (
    RunStep,
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
    AgentTokenEvent,
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

    def print_history(self, path, tail=None) -> None:
        self.history_calls.append((path, tail))

    def notify(self, kind, message) -> None:
        self.notifications.append((kind, message))

    def on_subagent_progress(self, progress) -> None:
        self.subagent_progress_events.append(progress)


assert isinstance(FakeSink(), AgentEventSink)  # runtime_checkable conformance


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
    controller.engine.stream = _fake_stream(
        [
            AgentStartEvent(model="m", message_count=1, max_iterations=50),
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


def test_new_session_resets_state(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    first_id = controller.tree.session_id
    controller.tree.append(parent_id=None, message=Message(role="user", content="x"))

    controller.new_session()

    assert controller.tree.session_id != first_id
    assert controller.current_node_id is None
    assert sink.session_ids[-1] == controller.tree.session_id


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
