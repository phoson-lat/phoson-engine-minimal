"""Tests for LLM-generated session titles (#55 follow-up).

The controller seeds a cheap heuristic title immediately, then upgrades it
with one tool-free, time-bounded model call in the background. The heuristic
is the fallback for every failure path; the user's ``/title`` always wins.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message, LLMDoneEvent
from phoson_cli.controller import (
    SessionController,
    _sanitize_title,
    _title_candidate_models,
)


class _Sink:
    """Minimal recording sink: enough for the controller's init + notify."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.user_messages: list[tuple[str, Message]] = []
        self.session_ids: list[str] = []
        self.title_refreshes = 0

    def set_session(self, session_id: str) -> None:
        self.session_ids.append(session_id)

    def on_session_title(self) -> None:
        """Optional chrome-repaint hook (no visible notice)."""
        self.title_refreshes += 1

    def notify(self, kind: str, message: str) -> None:
        self.notifications.append((kind, message))

    def on_user_message(self, text: str, message: Message) -> None:
        self.user_messages.append((text, message))

    def on_attachments(self, sources) -> None:  # noqa: ANN001
        pass

    def on_event(self, event) -> None:  # noqa: ANN001
        pass

    def flush_line(self) -> None:
        pass

    def capture_partial_reasoning(self) -> None:
        pass

    def take_reasoning(self) -> str:
        return ""

    def on_subagent_progress(self, progress) -> None:  # noqa: ANN001
        pass


def _make_controller(tmp_path, **cfg) -> tuple[SessionController, _Sink]:
    sink = _Sink()
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


def _add_user(controller: SessionController, text: str) -> None:
    controller.tree.append(parent_id=None, message=Message(role="user", content=text))


# ── _sanitize_title ──────────────────────────────────────────────────────────


def test_sanitize_title_strips_quotes_and_markdown() -> None:
    assert (
        _sanitize_title('"Fix the docker healthcheck"') == "Fix the docker healthcheck"
    )
    assert _sanitize_title("**Refactor auth layer**") == "Refactor auth layer"
    assert _sanitize_title("- Investigate flaky tests") == "Investigate flaky tests"
    assert _sanitize_title("## Summary of the session") == "Summary of the session"


def test_sanitize_title_keeps_first_line_and_collapses_space() -> None:
    assert _sanitize_title("First line\nSecond line") == "First line"
    assert _sanitize_title("  lots    of\t space  ") == "lots of space"


def test_sanitize_title_empty_and_non_string() -> None:
    assert _sanitize_title("") == ""
    assert _sanitize_title("   \n  ") == ""
    assert _sanitize_title(None) == ""  # type: ignore[arg-type]
    assert _sanitize_title(MagicMock()) == ""  # defensive: never raises


def test_sanitize_title_truncates_with_ellipsis() -> None:
    title = _sanitize_title("x" * 200)
    assert len(title) == 80
    assert title.endswith("…")


# ── model fallback ───────────────────────────────────────────────────────────


def test_title_candidate_models_prefers_title_then_subagent_then_active() -> None:
    cfg = PhosonConfig(title_model="title-model", subagent_model="cheap-model")
    assert _title_candidate_models(cfg, "active-model") == [
        "title-model",
        "cheap-model",
        "active-model",
    ]
    cfg = PhosonConfig(title_model="", subagent_model="cheap-model")
    assert _title_candidate_models(cfg, "active-model") == [
        "cheap-model",
        "active-model",
    ]
    cfg = PhosonConfig(title_model="", subagent_model="")
    assert _title_candidate_models(cfg, "active-model") == ["active-model"]


def test_title_candidate_models_deduplicates() -> None:
    """The default subagent model is often the active model too — try once."""
    cfg = PhosonConfig(title_model="same", subagent_model="same")
    assert _title_candidate_models(cfg, "same") == ["same"]


# ── heuristic seed ───────────────────────────────────────────────────────────


def test_ensure_session_title_seeds_heuristic_and_marks_auto(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    _add_user(controller, "How do I fix the docker healthcheck?")
    controller._ensure_session_title()
    assert controller.tree.title == "How do I fix the docker healthcheck?"
    assert controller._title_is_auto is True


def test_ensure_session_title_skips_commands_and_wakes(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    _add_user(controller, "/model gpt-4o")
    controller._ensure_session_title()
    assert controller.tree.title is None
    assert controller._title_is_auto is False

    controller2, _sink2 = _make_controller(tmp_path)
    _add_user(
        controller2,
        "[MONITOR EVENTS] A background monitor fired while you were idle.",
    )
    controller2._ensure_session_title()
    assert controller2.tree.title is None


def test_ensure_session_title_never_overwrites_existing(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    controller.tree.title = "User chose this"
    _add_user(controller, "something else entirely")
    controller._ensure_session_title()
    assert controller.tree.title == "User chose this"
    assert controller._title_is_auto is False


# ── LLM generation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_llm_title_success_replaces_heuristic(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    _add_user(controller, "the docker healthcheck keeps failing")
    controller._ensure_session_title()
    await controller.storage.save(controller.tree)

    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(
            content='"Docker healthcheck failure"', has_tool_calls=False
        )
    )
    await controller._generate_llm_title()

    assert controller.tree.title == "Docker healthcheck failure"
    # No transcript notice; the header is repainted via the silent hook.
    assert sink.notifications == []
    assert sink.title_refreshes == 1

    # Persisted to the session meta record so the picker sees it.
    metas = await controller.storage.list_meta()
    assert any(m.title == "Docker healthcheck failure" for m in metas)


@pytest.mark.asyncio
async def test_generate_llm_title_passes_fallback_model(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path, title_model="")
    _add_user(controller, "name me")
    controller._ensure_session_title()

    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(content="A name", has_tool_calls=False)
    )
    await controller._generate_llm_title()

    _messages, request = controller.chat.complete.await_args.args
    assert request.model == controller.config.subagent_model
    # Reasoning is disabled for the title call (fast + content always emitted).
    assert request.think is False


@pytest.mark.asyncio
async def test_generate_llm_title_falls_back_to_active_model(tmp_path) -> None:
    """A stale subagent model (wrong provider) must not leave the heuristic:
    the active model is tried next and the title is still generated."""
    controller, sink = _make_controller(tmp_path, title_model="")
    _add_user(controller, "investigate navier-stokes and openai")
    controller._ensure_session_title()
    await controller.storage.save(controller.tree)

    calls: list[str] = []

    async def _complete(_messages, request):
        calls.append(request.model)
        if len(calls) == 1:
            raise RuntimeError("model not available on this provider")
        return LLMDoneEvent(content="Navier-Stokes research", has_tool_calls=False)

    controller.chat.complete = AsyncMock(side_effect=_complete)
    await controller._generate_llm_title()

    assert calls == [controller.config.subagent_model, controller.current_model]
    assert controller.tree.title == "Navier-Stokes research"
    assert sink.notifications == []
    assert sink.title_refreshes == 1


@pytest.mark.asyncio
async def test_generate_llm_title_failure_keeps_heuristic(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    _add_user(controller, "keep this heuristic title")
    controller._ensure_session_title()
    heuristic = controller.tree.title

    controller.chat.complete = AsyncMock(side_effect=RuntimeError("provider down"))
    await controller._generate_llm_title()

    assert controller.tree.title == heuristic
    assert not any("Session titled" in msg for _kind, msg in sink.notifications)


@pytest.mark.asyncio
async def test_generate_llm_title_timeout_keeps_heuristic(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path, title_timeout_s=0.01)
    _add_user(controller, "slow provider")
    controller._ensure_session_title()
    heuristic = controller.tree.title

    async def _slow(*_a, **_k):
        await asyncio.sleep(0.5)
        return LLMDoneEvent(content="Too late", has_tool_calls=False)

    controller.chat.complete = AsyncMock(side_effect=_slow)
    await controller._generate_llm_title()

    assert controller.tree.title == heuristic


@pytest.mark.asyncio
async def test_generate_llm_title_never_overwrites_user_title(tmp_path) -> None:
    controller, sink = _make_controller(tmp_path)
    _add_user(controller, "whatever")
    controller.tree.title = "Mine"
    controller.note_user_title()  # user-set → not auto

    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(content="LLM guess", has_tool_calls=False)
    )
    await controller._generate_llm_title()

    assert controller.tree.title == "Mine"
    assert not any("Session titled" in msg for _kind, msg in sink.notifications)


@pytest.mark.asyncio
async def test_generate_llm_title_empty_reply_keeps_heuristic(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    _add_user(controller, "seed")
    controller._ensure_session_title()
    heuristic = controller.tree.title

    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(content="   ", has_tool_calls=False)
    )
    await controller._generate_llm_title()

    assert controller.tree.title == heuristic


@pytest.mark.asyncio
async def test_generate_llm_title_ignored_after_session_switch(tmp_path) -> None:
    """A /new while the title call is in flight must not rename either tree."""
    controller, sink = _make_controller(tmp_path)
    _add_user(controller, "old session")
    controller._ensure_session_title()
    old_tree = controller.tree
    heuristic = old_tree.title

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked(*_a, **_k):
        started.set()
        await release.wait()
        return LLMDoneEvent(content="Late title", has_tool_calls=False)

    controller.chat.complete = AsyncMock(side_effect=_blocked)
    task = asyncio.create_task(controller._generate_llm_title())
    await started.wait()
    controller._reset_session()  # session swapped mid-flight
    release.set()
    await task

    assert old_tree.title == heuristic
    assert controller.tree.title is None
    assert not any("Session titled" in msg for _kind, msg in sink.notifications)


# ── scheduling ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_llm_title_disabled_does_not_create_task(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path, llm_titles=False)
    _add_user(controller, "hello")
    controller._ensure_session_title()
    controller._schedule_llm_title()
    assert controller._title_task is None


@pytest.mark.asyncio
async def test_schedule_llm_title_is_one_shot(tmp_path) -> None:
    controller, _sink = _make_controller(tmp_path)
    _add_user(controller, "hello")
    controller._ensure_session_title()

    controller.chat.complete = AsyncMock(
        return_value=LLMDoneEvent(content="A title", has_tool_calls=False)
    )
    controller._schedule_llm_title()
    first = controller._title_task
    assert first is not None
    await first

    controller._schedule_llm_title()
    assert controller._title_task is first  # not rescheduled
