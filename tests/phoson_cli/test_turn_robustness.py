"""Turn robustness: a cancel mid-checkpoint (R1) or any other error mid-turn
(R2) never leaves an unanswered tool call; tracked housekeeping (R7); SDK
timeouts (R8); stop_bg_job on another instance's job (R3)."""

import os
import sys
import time
import asyncio
import tempfile

import pytest
from test_hard_kill_durability import (  # type: ignore[import-not-found]
    _disk_tree,
    _done_event,
    _latest_leaf,
    _tool_result,
    _make_controller,
    _assistant_tool_use,
)

from phoson_llm.schemas import ToolUseBlock, ToolResultBlock
from phoson_agent.models import AgentStartEvent, AgentToolStartEvent


def _paired(messages) -> bool:
    pending: set[str] = set()
    for message in messages:
        if isinstance(message.content, str):
            continue
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                pending.add(block.tool_call_id)
            elif isinstance(block, ToolResultBlock):
                pending.discard(block.tool_call_id)
    return not pending


@pytest.mark.asyncio
async def test_cancel_during_a_checkpoint_keeps_the_tool_call_answered(
    tmp_path,
) -> None:
    controller, _sink = _make_controller(tmp_path)
    real_save = controller.storage.save
    calls = {"n": 0}
    in_save = asyncio.Event()

    async def slow_save(tree):
        calls["n"] += 1
        if calls["n"] == 2:  # the checkpoint taken at AgentToolStartEvent
            in_save.set()
            await asyncio.sleep(0.5)
        return await real_save(tree)

    controller.storage.save = slow_save

    async def stream(path, config):
        controller.engine._history[:] = list(path)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        controller.engine._history.append(_assistant_tool_use("tc_1"))
        try:
            yield AgentToolStartEvent(index=0, tool_call_id="tc_1", tool_name="bash")
            await asyncio.sleep(10)
        except BaseException:
            # What the real engine does when its generator is closed.
            controller.engine._history.append(_tool_result("tc_1", "cancelled"))
            raise

    controller.engine.stream = stream
    turn = asyncio.create_task(controller.run_turn("first"))
    await in_save.wait()
    controller.cancel_current()
    outcome = await turn

    assert outcome.status == "cancelled"
    memory = controller.tree.get_path(controller.current_node_id)
    assert _paired(memory)
    disk = await _disk_tree(tmp_path, controller.tree.session_id)
    assert _paired([n.message for n in disk.get_node_path(_latest_leaf(disk))])


@pytest.mark.asyncio
async def test_an_error_mid_turn_is_reported_and_the_next_turn_is_valid(
    tmp_path,
) -> None:
    controller, sink = _make_controller(tmp_path)
    real_save = controller.storage.save
    calls = {"n": 0}

    async def flaky_save(tree):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError("Access is denied (os.replace)")
        return await real_save(tree)

    controller.storage.save = flaky_save

    async def stream1(path, config):
        controller.engine._history[:] = list(path)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=50)
        controller.engine._history.append(_assistant_tool_use("tc_1"))
        yield AgentToolStartEvent(index=0, tool_call_id="tc_1", tool_name="bash")
        await asyncio.sleep(10)

    controller.engine.stream = stream1
    # The error still reaches the front end (it renders "could not save")...
    with pytest.raises(PermissionError):
        await controller.run_turn("first")

    # ...but the tree no longer ends on an unanswered call.
    assert not controller.is_running
    assert _paired(controller.tree.get_path(controller.current_node_id))

    seen: dict = {}

    async def stream2(path, config):
        seen["path"] = path
        yield AgentStartEvent(model="m", message_count=len(path), max_iterations=50)
        yield _done_event("ok")

    controller.engine.stream = stream2
    assert (await controller.run_turn("second")).status == "done"
    assert _paired(seen["path"])


@pytest.mark.asyncio
async def test_aborted_sessions_are_repaired_on_load(tmp_path) -> None:
    from phoson_agent.sessions import STATUS_ABORTED

    controller, _sink = _make_controller(tmp_path)
    controller.tree.status = STATUS_ABORTED
    session = controller._session
    first = controller.tree.append(None, _done_event("x").result.history[0])
    node = controller.tree.append(first.id, _assistant_tool_use("tc_9"))
    session.current_node_id = node.id
    assert await controller._repair_orphaned_run(session) is True
    assert _paired(controller.tree.get_path(session.current_node_id))


@pytest.mark.asyncio
async def test_housekeeping_tasks_are_tracked_and_awaited(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    done = asyncio.Event()

    async def slow_close() -> None:
        await asyncio.sleep(0.05)
        done.set()

    async def failing() -> None:
        raise RuntimeError("boom")

    task = controller._spawn(slow_close(), "test:close")
    controller._spawn(failing(), "test:fail")
    assert task in controller._background_jobs
    await controller.shutdown()
    assert done.is_set()
    assert not controller._background_jobs


def test_sdk_clients_have_bounded_timeouts(monkeypatch) -> None:
    from phoson_llm.utils import SDK_MAX_RETRIES, sdk_timeout
    from phoson_llm.chats.openai_compatible import OpenAICompatibleChat

    client = OpenAICompatibleChat(base_url="http://x", api_key="k")._client
    assert client.timeout.read == 300.0 and client.timeout.connect == 15.0
    assert client.max_retries == SDK_MAX_RETRIES == 1
    monkeypatch.setenv("PHOSON_LLM_READ_TIMEOUT", "900")
    assert sdk_timeout().read == 900.0
    monkeypatch.setenv("PHOSON_LLM_READ_TIMEOUT", "nonsense")
    assert sdk_timeout().read == 300.0


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_stop_bg_job_kills_a_job_another_instance_started() -> None:
    import gc

    from phoson_agent.procutil import process_group_alive
    from phoson_plugin_bgjobs._plugin import BgJobsPlugin

    data = tempfile.mkdtemp()
    old = BgJobsPlugin()
    old.configure({"data_dir": data})
    old.initialize()
    await old._run_bg_job("sleep 30", "srv", data, None, 0, None)
    job = old._store.list()[0]
    new = BgJobsPlugin()
    new.configure({"data_dir": data})
    new.initialize()
    await old.aclose()
    del old
    gc.collect()
    await new.ensure_started()
    try:
        started = time.monotonic()
        message = await new._stop_bg_job(job.job_id)
        assert time.monotonic() - started < 12
        assert "stopped" in message
        await asyncio.sleep(0.3)
        assert not process_group_alive(job.pgid)
    finally:
        try:
            os.killpg(job.pgid, 9)
        except ProcessLookupError:
            pass
        await new.aclose()
