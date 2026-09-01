"""Regression tests for #174 / F-01, F-02 (and #141 / H-7).

The final review found two execution paths that built an
:class:`AgentEngine` *without* the REPL's middleware chain
(Offload → Summarizer → Permission), so the ``permissions.json`` policy,
``safe_mode`` and auto-compaction did not apply:

1. **Sub-agents** (``agent`` / ``agents``) — the child engine ran with no
   middlewares, so a ``deny``-level tool was still invocable and ``bash``
   ran with ``safe_mode=False`` and no confirmation.
2. **One-shot** (``-p`` / stdin) — ``_run_oneshot`` built the engine without
   Offload / Summarizer / Permission, and printed ``None`` for empty output.

These tests pin each fix: a ``deny`` tool is refused inside a sub-agent,
the one-shot engine carries the chain (permission gate fail-closed, no
callback), empty content prints as an empty string (not ``None``), and the
non-interactive wall-clock budget (#141) terminates a hung run with
exit code 124.
"""

import types
from unittest.mock import patch
from collections.abc import AsyncIterator

import pytest

from phoson_agent.tool import tool
from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ModelConfig,
    LLMDoneEvent,
    ToolCallEvent,
)
from phoson_cli.__main__ import _run_oneshot
from phoson_llm.chats.base import BaseLLMChat
from phoson_agent.permissions import (
    LEVEL_DENY,
    PermissionPolicy,
    PermissionMiddleware,
)
from phoson_cli.tools.subagent import _run_one_subagent

# ── fakes ─────────────────────────────────────────────────────────────────────


class _ToolCallChat(BaseLLMChat):
    """Yields one tool call on the first stream, then a final answer.

    Proves whether a gate (or the tool itself) intercepts the call: if the
    tool body is *never* invoked, the call was blocked before execution.
    ``tool_name`` / ``args`` are configurable so the same fake can drive a
    permission-denied tool or an injected-flag probe tool.
    """

    def __init__(
        self,
        *,
        final: str = "done",
        tool_name: str = "bash",
        args: dict | None = None,
    ) -> None:
        self.final = final
        self.tool_name = tool_name
        self.args = args or {}
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        if self.calls == 1:
            yield ToolCallEvent(
                index=0, tool_call_id="t1", tool_name=self.tool_name, args=self.args
            )
            yield LLMDoneEvent(content="", has_tool_calls=True)
        else:
            yield LLMDoneEvent(content=self.final, has_tool_calls=False)


class _FakeChat:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def _deny_bash_middleware() -> PermissionMiddleware:
    return PermissionMiddleware(policy=PermissionPolicy(levels={"bash": LEVEL_DENY}))


# ── F-01: sub-agents inherit the permission gate ─────────────────────────────


@pytest.mark.asyncio
async def test_subagent_refuses_denied_tool() -> None:
    """A `deny`-level tool is blocked inside a sub-agent (F-01)."""
    calls: list[str] = []

    @tool
    async def bash(command: str) -> str:  # noqa: ARG001
        calls.append(command)
        return "RAN"

    content, _fallback = await _run_one_subagent(
        task="run bash",
        chat=_ToolCallChat(final="done", args={"command": "true"}),
        selected_tools=[bash],
        model="fake",
        max_iterations=4,
        middlewares=[_deny_bash_middleware()],
    )

    # The sub-agent still reaches a final answer (the denial is fed back to
    # the model as the tool result) — but the tool body itself never ran.
    assert content == "done"
    assert calls == []


@pytest.mark.asyncio
async def test_subagent_without_middleware_runs_tool() -> None:
    """Counter-case: without a gate the tool runs (the old, broken behavior)."""
    calls: list[str] = []

    @tool
    async def bash(command: str) -> str:  # noqa: ARG001
        calls.append(command)
        return "RAN"

    content, _ = await _run_one_subagent(
        task="run bash",
        chat=_ToolCallChat(final="done", args={"command": "true"}),
        selected_tools=[bash],
        model="fake",
        max_iterations=4,
        # No middlewares → the tool is invoked (the F-01 hole, pre-fix).
        middlewares=None,
    )

    assert content == "done"
    assert calls == ["true"]


def _probe_tool() -> tuple[dict[str, object], object]:
    """Build a tool that records what the engine injected into it.

    The sub-agent chat asks the model to call ``probe`` with no args; the
    handler stashes the injected values it receives (from the sub-engine's
    context) so the test can assert the parent's flags propagated. Returns
    the tool plus the capture dict.
    """
    captured: dict[str, object] = {}

    @tool(inject=["safe_mode", "bash_confirmation"])
    async def probe(safe_mode: bool = False, bash_confirmation: object = None) -> str:
        captured["safe_mode"] = safe_mode
        captured["bash_confirmation"] = bash_confirmation
        return "ok"

    return captured, probe


@pytest.mark.asyncio
async def test_subagent_inherits_runtime_flags() -> None:
    """The sub-engine context carries the parent's `safe_mode` (F-01).

    The old ``# noqa: ARG001 — propagated via context`` claim was false: the
    child engine ran with an *empty* context, so `safe_mode` always arrived
    as False. This pins that the flag now actually reaches the sub-tool.
    """
    captured, probe = _probe_tool()
    await _run_one_subagent(
        task="probe",
        chat=_ToolCallChat(final="done", tool_name="probe", args={}),
        selected_tools=[probe],
        model="fake",
        max_iterations=4,
        middlewares=[],
        safe_mode=True,
        bash_confirmation="SENTINEL",
    )

    assert captured["safe_mode"] is True
    assert captured["bash_confirmation"] == "SENTINEL"


@pytest.mark.asyncio
async def test_agents_parallel_subagent_refuses_denied_tool() -> None:
    """The parallel ``agents`` tool also applies the permission gate (F-01)."""
    from phoson_cli.tools.subagent import agents

    calls: list[str] = []

    @tool
    async def bash(command: str) -> str:  # noqa: ARG001
        calls.append(command)
        return "RAN"

    result = await agents.handler(
        {"tasks": ["run A", "run B"]},
        {
            "chat": _ToolCallChat(final="done", args={"command": "true"}),
            "available_tools": {"bash": bash},
            "default_model": "fake",
            "max_iterations": 4,
            "safe_mode": False,
            "middlewares": [_deny_bash_middleware()],
        },
    )

    # Both parallel sub-agents reach a final answer but never run the tool.
    assert "=== Agent 0: run A ===" in result
    assert "=== Agent 1: run B ===" in result
    assert calls == []


# ── F-02: one-shot carries the chain, fail-closed, prints empty ──────────────


class _CapturingEngine:
    """Stand-in for AgentEngine that records construction kwargs + context."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.context = types.SimpleNamespace(extra={})
        self.tools = []
        self._loaded_plugins = []
        # final_content is set by the test to exercise the print path.
        self.final_content: str | None = "RESULT"

    async def run(self, messages, config):  # noqa: ANN001
        return types.SimpleNamespace(final_content=self.final_content)


@pytest.mark.asyncio
async def test_oneshot_builds_permission_chain(tmp_path) -> None:
    """The one-shot engine carries Offload → Summarizer → Permission (F-02)."""
    from phoson_agent.plugins.offload import OffloadMiddleware
    from phoson_agent.plugins.summarizer import SummarizationMiddleware

    captured: dict = {}

    class _Cap(_CapturingEngine):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            captured.update(kwargs)

    with (
        patch("phoson_cli.__main__.build_chat", return_value=_FakeChat()),
        patch("phoson_agent.AgentEngine", _Cap),
    ):
        rc = await _run_oneshot(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path), "do it"
        )

    assert rc == 0
    mws = captured.get("middlewares", [])
    # Default config: offload_tool_outputs=True → the full chain is present.
    assert any(isinstance(m, OffloadMiddleware) for m in mws)
    assert any(isinstance(m, SummarizationMiddleware) for m in mws)
    # The permission gate must be present and is always last in the chain.
    assert isinstance(mws[-1], PermissionMiddleware)
    # Non-interactive: no confirmation callback → ask fails closed.
    assert mws[-1].on_ask is None


@pytest.mark.asyncio
async def test_oneshot_fail_closed_ask_without_tty(tmp_path) -> None:
    """A `bash: ask` policy is *refused* (not run) in non-interactive mode.

    The middleware with ``on_ask=None`` raises for an ``ask`` decision —
    the same gate that one-shot now attaches. This pins the "fail-closed"
    half of F-02 at the middleware level the one-shot engine uses.
    """
    from phoson_llm.schemas import ToolCallEvent as _TCE
    from phoson_agent.permissions import ToolBlockedError

    mw = PermissionMiddleware(
        policy=PermissionPolicy(levels={"bash": "ask"}), on_ask=None
    )
    call = _TCE(index=0, tool_call_id="t1", tool_name="bash", args={"command": "ls"})
    with pytest.raises(ToolBlockedError):
        await mw.on_before_tool(call)


@pytest.mark.asyncio
async def test_oneshot_empty_content_prints_blank_not_none(capsys, tmp_path) -> None:
    """``-p`` prints an empty string (not ``None``) when there is no content."""
    chat = _FakeChat()

    class _EmptyEngine(_CapturingEngine):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.final_content = None  # simulate no content

    with (
        patch("phoson_cli.__main__.build_chat", return_value=chat),
        patch("phoson_agent.AgentEngine", _EmptyEngine),
    ):
        rc = await _run_oneshot(
            PhosonConfig(provider="ollama", sessions_dir=tmp_path), "do it"
        )

    assert rc == 0
    out = capsys.readouterr().out
    assert "None" not in out
    assert out.strip() == ""


# ── #141 / H-7: non-interactive wall-clock budget ─────────────────────────────


@pytest.mark.asyncio
async def test_oneshot_hang_hits_budget_exit_124(capsys, tmp_path) -> None:
    """A hung run terminates at the budget with exit code 124 and a clean msg."""

    class _HangEngine:
        def __init__(self, **kwargs) -> None:
            self.context = types.SimpleNamespace(extra={})
            self.tools = []
            self._loaded_plugins = []

        async def run(self, messages, config):  # noqa: ANN001
            import asyncio

            await asyncio.sleep(3600)  # never returns
            return types.SimpleNamespace(final_content="never")

    chat = _FakeChat()
    with (
        patch("phoson_cli.__main__.build_chat", return_value=chat),
        patch("phoson_agent.AgentEngine", _HangEngine),
    ):
        rc = await _run_oneshot(
            PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                run_budget_seconds=0.05,
            ),
            "hang",
        )

    assert rc == 124
    err = capsys.readouterr().err
    assert "budget" in err
    assert "PHOSON_RUN_BUDGET_SECONDS" in err
    # Teardown still closes the chat client on the budget path.
    assert chat.closed == 1


@pytest.mark.asyncio
async def test_oneshot_budget_zero_disables(capsys, tmp_path) -> None:
    """``run_budget_seconds=0`` disables the budget (run proceeds)."""

    class _FastEngine(_CapturingEngine):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.final_content = "ok"

    chat = _FakeChat()
    with (
        patch("phoson_cli.__main__.build_chat", return_value=chat),
        patch("phoson_agent.AgentEngine", _FastEngine),
    ):
        rc = await _run_oneshot(
            PhosonConfig(
                provider="ollama",
                sessions_dir=tmp_path,
                run_budget_seconds=0,
            ),
            "ok",
        )

    assert rc == 0
    assert "ok" in capsys.readouterr().out


# ── config plumbing for PHOSON_RUN_BUDGET_SECONDS ─────────────────────────────


def test_run_budget_env_override(monkeypatch) -> None:
    from phoson_cli.config import load_config

    monkeypatch.setenv("PHOSON_RUN_BUDGET_SECONDS", "120")
    cfg = load_config()
    assert cfg.run_budget_seconds == 120.0


def test_run_budget_zero_via_env(monkeypatch) -> None:
    from phoson_cli.config import load_config

    monkeypatch.setenv("PHOSON_RUN_BUDGET_SECONDS", "0")
    cfg = load_config()
    assert cfg.run_budget_seconds == 0.0


def test_run_budget_default_is_600() -> None:
    from phoson_cli.config import PhosonConfig

    assert PhosonConfig().run_budget_seconds == 600.0
