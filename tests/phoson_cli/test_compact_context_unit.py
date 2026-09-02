"""Unit tests for the agent-controlled ``compact_context`` tool (#147).

The tool must:
- be registered on the *main* engine's tool list (not the shared registry
  sub-agents select from);
- perform the same structured compaction as ``/compact`` and the automatic
  gate (tool-pair-safe cut, structured summary, empty-summary abort);
- splice the engine's in-flight history in place and queue a tree-rebase
  event rather than mutating the tree directly;
- leave the automatic threshold gate untouched as a safety net.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message, LLMDoneEvent
from phoson_cli.controller import SessionController
from phoson_cli.tools.compact import compact_context
from phoson_agent.plugins.summarizer import SummarizationEvent

# ── Fake sink (minimal — mirrors FakeSink in test_controller_unit) ──────────


class _Sink:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []

    def on_user_message(self, text, message) -> None:
        pass

    def on_attachments(self, sources) -> None:
        pass

    def on_event(self, event) -> None:
        pass

    def flush_line(self) -> None:
        pass

    def capture_partial_reasoning(self) -> None:
        pass

    def take_reasoning(self) -> str:
        return ""

    def set_session(self, session_id) -> None:
        pass

    def print_history(self, path, tail=None) -> None:
        pass

    def notify(self, kind, message) -> None:
        self.notifications.append((kind, message))

    def on_subagent_progress(self, progress) -> None:
        pass


def _make_controller(tmp_path, **cfg) -> SessionController:
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
        return SessionController(config, _Sink())


def _seed_engine_history(controller: SessionController, msgs: list[Message]) -> None:
    """Put messages into the engine's in-flight history (what the tool reads)."""
    controller.engine._history[:] = list(msgs)


# ── Registration: main engine only ──────────────────────────────────────────


def test_compact_context_is_on_main_engine_tools(tmp_path) -> None:
    controller = _make_controller(tmp_path)
    tool_names = {t.name for t in controller.engine.tools}
    assert "compact_context" in tool_names


def test_compact_context_not_in_shared_registry_for_subagents(tmp_path) -> None:
    """Sub-agents select from ``tools_dict``; the tool must not be there."""
    controller = _make_controller(tmp_path)
    assert "compact_context" not in controller.tools_dict
    # Also confirm it is not part of the controller's shared ``tools`` list
    # (it is appended to the engine's list only, not merged into self.tools).
    assert "compact_context" not in {t.name for t in controller.tools}


def test_do_compact_is_injected(tmp_path) -> None:
    """The tool's injected callable must be the controller's in-flight fn."""
    controller = _make_controller(tmp_path)
    fn = controller.engine.context.extra["do_compact"]
    # A bound method: bound to this controller, pointing at _compact_inflight.
    assert fn.__self__ is controller
    assert fn.__func__.__name__ == "_compact_inflight"


# ── Tool schema: no visible args ────────────────────────────────────────────


def test_compact_context_has_no_visible_parameters() -> None:
    """The model must see a no-arg tool: the policy is the session's, not
    model-supplied cut points."""
    # ``do_compact`` is injected → excluded from the schema.
    assert compact_context.name == "compact_context"
    props = (compact_context.parameters or {}).get("properties", {})
    assert props == {}


# ── _compact_inflight: happy path ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_compact_inflight_splices_engine_history_in_place(tmp_path) -> None:
    controller = _make_controller(tmp_path, compact_min_keep_messages=4)

    # 8 non-system turns: enough to compact (cut > 0 with min_keep=4).
    msgs = [Message(role="user", content=f"q{i}") for i in range(4)] + [
        Message(role="assistant", content=f"a{i}") for i in range(4)
    ]
    _seed_engine_history(controller, msgs)
    original_list_obj = controller.engine._history

    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(
            content="Goal: X. Completed: Y.", has_tool_calls=False
        )
    )

    result = await controller._compact_inflight()

    # The report indicates a compaction happened.
    assert "Compacted" in result

    # Spliced **in place**: same list object, now shorter.
    assert controller.engine._history is original_list_obj
    # summary message + recent tail (min_keep=4)
    new_len = len(controller.engine._history)
    assert new_len == 1 + 4  # summary + 4 kept
    assert "Conversation summary" in controller.engine._history[0].content

    # A compaction event was queued for the run-end tree rebase.
    events = controller.summarizer.pop_compact_events()
    assert len(events) == 1
    assert isinstance(events[0], SummarizationEvent)
    assert events[0].original_tokens > events[0].compacted_tokens


@pytest.mark.asyncio
async def test_compact_inflight_does_not_mutate_tree(tmp_path) -> None:
    """The tool must NOT touch the tree directly — it queues an event and
    lets the run-end ``_rebase_after_compaction`` do the graft (like a
    mid-run auto-compaction)."""
    controller = _make_controller(tmp_path, compact_min_keep_messages=2)

    # Commit some history to the tree and capture its shape.
    seed = [Message(role="user", content=f"q{i}") for i in range(6)]
    controller.tree.append_many(None, seed)
    leaves = controller.tree.get_leaves()
    controller.current_node_id = max(
        leaves, key=lambda nid: controller.tree.nodes[nid].created_at
    )
    nodes_before = controller.tree.node_count()

    # In-flight history (not yet in the tree).
    _seed_engine_history(
        controller,
        [Message(role="user", content=f"live{i}") for i in range(6)],
    )
    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(content="S", has_tool_calls=False)
    )

    await controller._compact_inflight()

    # The tree itself was not grafted by the tool.
    assert controller.tree.node_count() == nodes_before
    # But an event is pending for the run-end rebase.
    assert len(controller.summarizer.pop_compact_events()) == 1


# ── _compact_inflight: edge cases ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_compact_inflight_empty_summary_aborts(tmp_path) -> None:
    """F-11: an empty summary must leave the history unchanged and queue no
    event."""
    controller = _make_controller(tmp_path, compact_min_keep_messages=2)
    msgs = [Message(role="user", content=f"q{i}") for i in range(6)]
    _seed_engine_history(controller, msgs)
    before_snapshot = list(controller.engine._history)

    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(content="   ", has_tool_calls=False)
    )
    result = await controller._compact_inflight()

    assert "empty summary" in result
    # History untouched (same objects, same length).
    assert controller.engine._history == before_snapshot
    # No event queued.
    assert controller.summarizer.pop_compact_events() == []


@pytest.mark.asyncio
async def test_compact_inflight_short_context_is_noop(tmp_path) -> None:
    """Nothing worth compacting → no LLM call, no change, no event."""
    controller = _make_controller(tmp_path, compact_min_keep_messages=4)
    msgs = [Message(role="user", content=f"q{i}") for i in range(4)]  # == min_keep
    _seed_engine_history(controller, msgs)
    controller.chat.complete = AsyncMock()

    result = await controller._compact_inflight()

    assert "nothing worth compacting" in result
    controller.chat.complete.assert_not_awaited()
    assert len(controller.engine._history) == 4
    assert controller.summarizer.pop_compact_events() == []


@pytest.mark.asyncio
async def test_compact_inflight_empty_session_is_noop(tmp_path) -> None:
    controller = _make_controller(tmp_path)
    _seed_engine_history(controller, [])
    result = await controller._compact_inflight()
    assert "empty" in result.lower()


# ── safe_cut_index: no orphaned tool_result at the top of the kept tail ─────


@pytest.mark.asyncio
async def test_compact_inflight_cut_is_tool_pair_safe(tmp_path) -> None:
    """If the recent-tail cut would land on a tool_result, the cut must back
    up to the matching tool_use so no orphaned tool_result survives at the
    top of the kept tail (F-10 / #176)."""
    from phoson_llm.schemas import ToolUseBlock, ToolResultBlock

    # Build a history whose naive tail boundary would orphan a tool_result:
    #   [user q0, assistant a0, assistant(ToolUse), user(ToolResult),
    #    user q1, assistant a1]
    # With min_keep=3 the naive cut = len-3 = 3 → others[3] is the
    # tool_result user → safe_cut_index must back up to index 2 (the
    # assistant carrying the matching ToolUseBlock) so the pair is kept
    # together and no orphaned tool_result survives at the top of the tail.
    tool_use = Message(
        role="assistant",
        content=[
            ToolUseBlock(tool_call_id="t1", tool_name="bash", args={"command": "ls"})
        ],
    )
    tool_result = Message(
        role="user",
        content=[ToolResultBlock(tool_call_id="t1", result="ok")],
    )
    msgs = [
        Message(role="user", content="q0"),
        Message(role="assistant", content="a0"),
        tool_use,
        tool_result,
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
    ]
    controller = _make_controller(tmp_path, compact_min_keep_messages=3)
    _seed_engine_history(controller, msgs)
    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(content="S", has_tool_calls=False)
    )

    result = await controller._compact_inflight()
    assert "Compacted" in result

    kept = controller.engine._history
    # The kept tail (after the summary) must not start with the orphaned
    # tool_result — the pair (tool_use + tool_result) must be kept together.
    tail = kept[1:]
    assert tail[0] is tool_use, "cut backed up to the tool_use (pair kept)"
    assert tail[1] is tool_result
    # The event is queued.
    assert len(controller.summarizer.pop_compact_events()) == 1


# ── End-to-end through the tool handler (injected callable path) ────────────


@pytest.mark.asyncio
async def test_tool_handler_invokes_do_compact_via_context(tmp_path) -> None:
    """Call the tool the way the engine would: pass ``args`` + the engine's
    ``context``; the injected ``do_compact`` must come from ``context.extra``
    and run the real in-flight compaction."""
    controller = _make_controller(tmp_path, compact_min_keep_messages=2)
    _seed_engine_history(
        controller,
        [Message(role="user", content=f"q{i}") for i in range(6)],
    )
    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(content="S", has_tool_calls=False)
    )

    # This is exactly how the ReAct loop invokes a tool:
    #   handler(args, context) where context is the engine's AgentContext.
    out = await compact_context.handler(args={}, context=controller.engine.context)
    assert "Compacted" in str(out)
    # The engine history was actually compacted by the injected callable.
    assert "Conversation summary" in controller.engine._history[0].content
    assert controller.chat.complete.await_count == 1
