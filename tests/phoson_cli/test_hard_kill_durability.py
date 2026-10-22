"""Durable mid-turn checkpoints: a hard kill must not lose a finished step.

A turn used to be persisted only by its terminal path (``_finalize_run`` /
``_append_partial_history`` / ``_abort_turn``). A hard kill — crash,
``taskkill``, power loss — runs no terminal path at all, so the whole turn
(user prompt + every step the model already produced) vanished.

The controller now mirrors the engine's in-flight history into the tree and
saves *while the stream is running*: once before the first LLM call (the user
turn) and after every ``AgentToolStartEvent`` / ``AgentStepDoneEvent``.

These tests assert the on-disk file (a fresh ``JsonlStorage`` load, i.e. what a
new process would see), never the in-memory tree, so they fail if persistence
ever regresses back to end-of-turn only.
"""

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message, ToolUseBlock, ToolResultBlock
from phoson_agent.models import (
    RunStep,
    AgentDoneEvent,
    AgentRunResult,
    AgentErrorEvent,
    AgentStartEvent,
    AgentStepDoneEvent,
    AgentToolStartEvent,
)
from phoson_agent.sessions import JsonlStorage
from phoson_cli.controller import SessionController

# ── helpers ──────────────────────────────────────────────────────────────────


class FakeSink:
    """Minimal recording sink — the controller only needs a sink to run."""

    def __init__(self) -> None:
        self.events: list = []
        self.notifications: list[tuple[str, str]] = []
        self.reasoning = ""
        self.partial_captures = 0
        self.flushes = 0

    def on_user_message(self, text, message) -> None:
        pass

    def on_attachments(self, sources) -> None:
        pass

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
        pass

    def print_history(
        self, path, tail=None, timestamps=None, tool_durations=None
    ) -> None:
        pass

    def notify(self, kind, message) -> None:
        self.notifications.append((kind, message))

    def on_subagent_progress(self, progress) -> None:
        pass


def _done_event(answer: str = "hello") -> AgentDoneEvent:
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


class HardKill(BaseException):
    """Stands in for the process dying mid-turn.

    Derived from ``BaseException`` (not ``Exception``) on purpose: the
    controller catches ``asyncio.CancelledError`` and ``Exception`` on the
    abort/terminal paths, so this escapes them exactly like SIGKILL does —
    no cleanup, no save, nothing but whatever is already on disk.
    """


# Upper bound for every wait a cancelled run has to satisfy. It replaces an
# unbounded ``asyncio.sleep(3600)`` plus a bare ``await task``: with no cap, a
# cancellation that silently fails to land turns a 30-second failure into an
# hour-long silent hang — which is what left CI's ``tests`` job sitting at 18%
# until somebody cancelled it by hand.
_CANCEL_GRACE = 30.0


def _make_controller(tmp_path) -> tuple[SessionController, FakeSink]:
    sink = FakeSink()
    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        controller = SessionController(config, sink)
    return controller, sink


def _latest_leaf(tree) -> str | None:
    """The tree's continuation point (same rule as ``find_latest_node_id``).

    ``created_at`` first, insertion order as the tie-break — see #277. Kept in
    sync with the controller on purpose: this is the on-disk assertion's notion
    of "where a new process would resume", so it must not encode a different
    rule.
    """
    leaves = tree.get_leaves()
    if not leaves:
        return None
    order = {node_id: index for index, node_id in enumerate(tree.nodes)}
    return max(leaves, key=lambda nid: (tree.nodes[nid].created_at, order[nid]))


async def _disk_tree(tmp_path, session_id):
    """Load the session straight from disk — what a new process would see."""
    return await JsonlStorage(base_path=tmp_path).load(session_id)


async def _disk_messages(tmp_path, session_id) -> list[Message]:
    tree = await _disk_tree(tmp_path, session_id)
    return [node.message for node in tree.get_node_path(_latest_leaf(tree))]


def _text(message: Message) -> str:
    """Flatten a message's text (the JSONL round-trip normalizes to blocks)."""
    if isinstance(message.content, str):
        return message.content
    return "".join(
        block.text for block in message.content if getattr(block, "text", None)
    )


def _assistant_tool_use(tool_call_id: str, tool_name: str = "bash") -> Message:
    return Message(
        role="assistant",
        content=[ToolUseBlock(tool_call_id=tool_call_id, tool_name=tool_name, args={})],
    )


def _tool_result(tool_call_id: str, result: str = "ok") -> Message:
    return Message(
        role="user",
        content=[
            ToolResultBlock(tool_call_id=tool_call_id, result=result, error=False)
        ],
    )


def _step(kind: str = "llm") -> AgentStepDoneEvent:
    now = datetime.datetime.now(datetime.UTC)
    return AgentStepDoneEvent(
        step=RunStep(
            kind=kind, started_at=now, ended_at=now, duration_ms=1, tool_name="bash"
        )
    )


# ── the user turn is durable before the model is ever called ─────────────────


@pytest.mark.asyncio
async def test_user_turn_reaches_disk_before_first_llm_call(tmp_path) -> None:
    """The prompt is persisted before the engine streams anything.

    Killed while the model is still thinking, the session must already exist
    on disk with the user's message and an ``active`` status — that is what
    makes the run resumable (and orphan-checkable, #129) instead of a
    session that never happened.
    """
    controller, _sink = _make_controller(tmp_path)
    session_id = controller.tree.session_id
    on_disk: dict[str, object] = {}

    async def stream(path, config):
        # Nothing yielded yet: at this instant the engine has been entered
        # but no token has been produced.
        on_disk["messages"] = await _disk_messages(tmp_path, session_id)
        on_disk["status"] = (await _disk_tree(tmp_path, session_id)).status
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        yield _done_event("hello")

    controller.engine.stream = stream
    await controller.run_turn("q")

    assert [m.role for m in on_disk["messages"]] == ["user"]
    assert _text(on_disk["messages"][0]) == "q"
    assert on_disk["status"] == "active"


# ── every completed step lands on disk as it finishes ───────────────────────


@pytest.mark.asyncio
async def test_each_step_is_persisted_as_it_finishes(tmp_path) -> None:
    """After each step the finished work is on disk, not just in memory.

    The run never terminates: the stream dies with ``HardKill`` after the
    tool result is mirrored, so the assertions can only pass because the
    checkpoints saved mid-flight.
    """
    controller, _sink = _make_controller(tmp_path)
    session_id = controller.tree.session_id
    seen: dict[str, list[Message]] = {}

    async def stream(path, config):
        controller.engine._history[:] = list(path)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)

        # 1. The model asks for a tool. The engine appends the assistant
        #    tool_use, then emits AgentToolStartEvent right before dispatching
        #    the (possibly long) handler.
        controller.engine._history.append(_assistant_tool_use("tc_1"))
        yield AgentToolStartEvent(index=0, tool_call_id="tc_1", tool_name="bash")
        seen["after_tool_start"] = await _disk_messages(tmp_path, session_id)

        # 2. The handler finished; the tool result is appended and stepped.
        controller.engine._history.append(_tool_result("tc_1"))
        yield _step("tool")
        seen["after_tool_step"] = await _disk_messages(tmp_path, session_id)

        # 3. The model answers with prose; that step completes too.
        controller.engine._history.append(Message(role="assistant", content="all done"))
        yield _step("llm")
        seen["after_llm_step"] = await _disk_messages(tmp_path, session_id)

        raise HardKill  # the process dies here — no terminal path runs

    controller.engine.stream = stream

    with pytest.raises(HardKill):
        await controller.run_turn("q")

    assert [m.role for m in seen["after_tool_start"]] == ["user", "assistant"]
    assert isinstance(seen["after_tool_start"][1].content[0], ToolUseBlock)

    assert [m.role for m in seen["after_tool_step"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert isinstance(seen["after_tool_step"][2].content[0], ToolResultBlock)

    assert _text(seen["after_llm_step"][-1]) == "all done"
    assert len(seen["after_llm_step"]) == 4


@pytest.mark.asyncio
async def test_hard_kill_mid_turn_keeps_completed_steps(tmp_path) -> None:
    """The whole point: a kill between steps loses nothing already done."""
    controller, _sink = _make_controller(tmp_path)
    session_id = controller.tree.session_id

    async def stream(path, config):
        controller.engine._history[:] = list(path)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        controller.engine._history.append(_assistant_tool_use("tc_1"))
        yield AgentToolStartEvent(index=0, tool_call_id="tc_1", tool_name="bash")
        controller.engine._history.append(_tool_result("tc_1", "42"))
        yield _step("tool")
        raise HardKill  # dies mid-turn, before any finalize/abort path

    controller.engine.stream = stream
    with pytest.raises(HardKill):
        await controller.run_turn("q")

    messages = await _disk_messages(tmp_path, session_id)
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert isinstance(messages[1].content[0], ToolUseBlock)
    assert messages[2].content[0].result == "42"
    assert _text(messages[0]) == "q"

    # The run never settled, so the session is still ``active``: the next
    # resume knows the last run died and can repair the tool pairing.
    assert (await _disk_tree(tmp_path, session_id)).status == "active"


@pytest.mark.asyncio
async def test_killed_run_is_resumable_in_a_new_controller(tmp_path) -> None:
    """End to end: kill mid-tool, resume, and the step is really usable.

    The persisted tail ends on an unanswered ``tool_use``; loading the
    session must repair it (#129) instead of handing the provider a history
    it will reject with a 400 tool-pairing error.
    """
    controller, _sink = _make_controller(tmp_path)
    session_id = controller.tree.session_id

    async def stream(path, config):
        controller.engine._history[:] = list(path)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        controller.engine._history.append(_assistant_tool_use("tc_1"))
        yield AgentToolStartEvent(index=0, tool_call_id="tc_1", tool_name="bash")
        raise HardKill  # killed while the tool was still running

    controller.engine.stream = stream
    with pytest.raises(HardKill):
        await controller.run_turn("q")

    resumed, _sink2 = _make_controller(tmp_path)
    outcome = await resumed.load_session(session_id)

    assert outcome.ok
    path = resumed.tree.get_node_path(resumed.current_node_id)
    assert any(node.metadata.get("recovery") for node in path), (
        "the orphaned tool_use must be answered by a recovery node"
    )
    assert _text(resumed.tree.get_path(resumed.current_node_id)[0]) == "q"


# ── no duplication between the mid-turn checkpoints and the final save ──────


@pytest.mark.asyncio
async def test_checkpoints_do_not_duplicate_messages_on_success(tmp_path) -> None:
    """A completed run stores each message exactly once.

    The mid-turn checkpoints and ``_finalize_run`` both mirror the same
    history; the monotonic sync counter is what keeps the second pass from
    re-appending the first's messages.
    """
    controller, _sink = _make_controller(tmp_path)
    session_id = controller.tree.session_id
    final_history: list[Message] = []

    async def stream(path, config):
        controller.engine._history[:] = list(path)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        controller.engine._history.append(_assistant_tool_use("tc_1"))
        yield AgentToolStartEvent(index=0, tool_call_id="tc_1", tool_name="bash")
        controller.engine._history.append(_tool_result("tc_1"))
        yield _step("tool")
        controller.engine._history.append(Message(role="assistant", content="done"))
        yield _step("llm")
        final_history[:] = list(controller.engine._history)
        yield AgentDoneEvent(
            result=AgentRunResult(
                final_content="done",
                history=list(final_history),
                input_messages=[Message(role="user", content="q")],
                steps=[],
            )
        )

    controller.engine.stream = stream
    outcome = await controller.run_turn("q")

    assert outcome.status == "done"
    messages = await _disk_messages(tmp_path, session_id)
    assert len(messages) == len(final_history) == 4
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert _text(messages[0]) == "q"
    assert isinstance(messages[1].content[0], ToolUseBlock)
    assert messages[2].content[0].result == "ok"
    assert _text(messages[3]) == "done"
    assert (await _disk_tree(tmp_path, session_id)).status == "completed"


@pytest.mark.asyncio
async def test_checkpoints_do_not_duplicate_messages_on_error(tmp_path) -> None:
    """Same guarantee on the error path (``_append_partial_history``)."""
    controller, _sink = _make_controller(tmp_path)
    session_id = controller.tree.session_id

    async def stream(path, config):
        controller.engine._history[:] = list(path)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        controller.engine._history.append(_assistant_tool_use("tc_1"))
        yield AgentToolStartEvent(index=0, tool_call_id="tc_1", tool_name="bash")
        controller.engine._history.append(_tool_result("tc_1"))
        yield _step("tool")
        yield AgentErrorEvent(message="boom", code="tool")

    controller.engine.stream = stream
    outcome = await controller.run_turn("q")

    assert outcome.status == "error"
    messages = await _disk_messages(tmp_path, session_id)
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert (await _disk_tree(tmp_path, session_id)).status == "aborted"


@pytest.mark.asyncio
async def test_cancel_mid_turn_does_not_duplicate_checkpointed_steps(tmp_path) -> None:
    """Ctrl+C after a checkpointed step keeps the step, once.

    The graceful abort path slices from ``base_count``; the counter floor
    makes it a no-op for messages the checkpoints already persisted.
    """
    controller, _sink = _make_controller(tmp_path)
    session_id = controller.tree.session_id
    checkpointed = asyncio.Event()

    async def stream(path, config):
        controller.engine._history[:] = list(path)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        controller.engine._history.append(_assistant_tool_use("tc_1"))
        yield AgentToolStartEvent(index=0, tool_call_id="tc_1", tool_name="bash")
        # Resuming here means the controller already consumed the tool-start
        # event, i.e. its checkpoint is done; cancel from a known state
        # instead of racing the run's (slow, git/network-touching) startup.
        checkpointed.set()
        await asyncio.sleep(_CANCEL_GRACE)  # cancelled here

    controller.engine.stream = stream
    task = asyncio.create_task(controller.run_turn("q"))
    await asyncio.wait_for(checkpointed.wait(), timeout=_CANCEL_GRACE)
    assert [m.role for m in await _disk_messages(tmp_path, session_id)] == [
        "user",
        "assistant",
    ]
    assert controller.cancel_current() is True

    # Bounded too: a cancel that never lands must fail here in seconds. With a
    # bare `await task` it would instead sit on the sleep above for an hour.
    outcome = await asyncio.wait_for(task, timeout=_CANCEL_GRACE)

    assert outcome.status == "cancelled"
    messages = await _disk_messages(tmp_path, session_id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert isinstance(messages[1].content[0], ToolUseBlock)
