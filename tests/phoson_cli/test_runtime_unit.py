"""Shared runtime assembly (``phoson_cli.runtime``).

The REPL controller and the one-shot path both build their engine through
:func:`build_engine_runtime`; these tests pin what that shared sequence
guarantees so the two hosts cannot drift apart again (#174/F-02).
"""

from unittest.mock import patch

import pytest

from phoson_cli.config import PhosonConfig
from phoson_cli.runtime import build_engine_runtime, build_effort_scheduler
from phoson_cli.session_utils import build_offload, build_summarizer
from phoson_cli.tools.compact import compact_context
from phoson_cli.permissions_store import build_permission_middleware


class _FakeChat:
    async def aclose(self) -> None:
        pass


def _build(config: PhosonConfig, **kwargs):
    summarizer = build_summarizer(config)
    runtime = build_engine_runtime(
        config,
        chat=_FakeChat(),  # type: ignore[arg-type]
        plugins=[],
        offload=build_offload(config),
        summarizer=summarizer,
        permission=build_permission_middleware(on_ask=None),
        main_model="main",
        subagent_model="sub",
        **kwargs,
    )
    return runtime, summarizer


def test_effort_scheduler_only_when_sandwich_enabled(tmp_path) -> None:
    assert build_effort_scheduler(PhosonConfig(sessions_dir=tmp_path)) is None
    config = PhosonConfig(sessions_dir=tmp_path, reasoning_sandwich=True)
    assert build_effort_scheduler(config) is not None


def test_runtime_injects_subagent_context(tmp_path) -> None:
    config = PhosonConfig(
        sessions_dir=tmp_path,
        safe_mode=True,
        subagent_max_parallel=5,
        subagent_timeout_seconds=42.0,
    )
    runtime, _ = _build(config)
    extra = runtime.engine.context.extra

    assert extra["safe_mode"] is True
    assert extra["default_model"] == "sub"
    assert extra["main_model"] == "main"
    assert extra["subagent_max_parallel"] == 5
    assert extra["subagent_timeout_seconds"] == 42.0
    assert extra["middlewares"] is runtime.middlewares
    assert extra["available_tools"] is runtime.tools_dict


def test_extra_tools_reach_engine_but_not_subagents(tmp_path) -> None:
    runtime, _ = _build(
        PhosonConfig(sessions_dir=tmp_path), extra_tools=[compact_context]
    )

    assert compact_context.name in {tool.name for tool in runtime.engine.tools}
    assert compact_context.name not in runtime.tools_dict


def test_summarizer_gets_chat_and_tool_schemas(tmp_path) -> None:
    runtime, summarizer = _build(PhosonConfig(sessions_dir=tmp_path))

    assert summarizer.chat is runtime.engine.context.extra["chat"]
    names = {definition.name for definition in summarizer.tool_definitions}
    assert {"read_file", "bash"} <= names


def test_permission_gate_is_last_before_extras(tmp_path) -> None:
    sentinel = object()
    runtime, _ = _build(
        PhosonConfig(sessions_dir=tmp_path),
        extra_middlewares=[sentinel],  # type: ignore[list-item]
    )
    kinds = [type(mw).__name__ for mw in runtime.middlewares]

    assert runtime.middlewares[-1] is sentinel
    assert kinds[-2] == "PermissionMiddleware"


@pytest.mark.asyncio
async def test_oneshot_and_controller_share_the_middleware_chain(tmp_path) -> None:
    """Both hosts produce the same chain (one-shot may add tracing on top)."""
    from phoson_cli.__main__ import _run_oneshot
    from phoson_cli.controller import SessionController

    captured: dict = {}

    class _Engine:
        def __init__(self, **kwargs) -> None:
            captured["oneshot"] = [type(mw).__name__ for mw in kwargs["middlewares"]]
            captured["effort_scheduler"] = kwargs["effort_scheduler"]
            self.tools = kwargs["tools"]
            self.context = type("Ctx", (), {"extra": {}})()

        async def run(self, *args, **kwargs):
            return type("R", (), {"final_content": "ok"})()

    config = PhosonConfig(
        provider="ollama", sessions_dir=tmp_path, reasoning_sandwich=True
    )
    with (
        patch("phoson_cli.__main__.build_chat", return_value=_FakeChat()),
        patch("phoson_agent.AgentEngine", _Engine),
    ):
        assert await _run_oneshot(config, "task") == 0

    class _Sink:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    with patch("phoson_cli.controller.build_chat", return_value=_FakeChat()):
        controller = SessionController(config, _Sink())  # type: ignore[arg-type]
    try:
        interactive = [type(mw).__name__ for mw in controller._middlewares]
        assert captured["oneshot"] == interactive
        # One-shot used to ignore ``reasoning_sandwich`` entirely.
        assert captured["effort_scheduler"] is not None
    finally:
        await controller.shutdown()
