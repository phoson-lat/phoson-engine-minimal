"""Tests for IMPROVEMENTS.md E1 (CLI layer) — compact profiles, preview
plan, auto-compact mode, retained-reasoning registration, and config presets.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.config import (
    COMPACT_MODES,
    PhosonConfig,
    load_config,
    save_config,
)
from phoson_llm.schemas import Message, LLMDoneEvent
from phoson_cli.commands import Command, CommandHandler
from phoson_cli.controller import CompactPlan, SessionController
from phoson_agent.sessions.models import ConversationTree

# ── config presets (E1) ──────────────────────────────────────────────────


def _clean_env(monkeypatch) -> None:
    for var in (
        "PHOSON_COMPACT_MODE",
        "PHOSON_COMPACT_THRESHOLD",
        "PHOSON_COMPACT_MIN_KEEP",
        "PHOSON_OFFLOAD_TOOL_OUTPUTS",
        "PHOSON_OFFLOAD_MAX_CHARS",
        "PHOSON_OFFLOAD_HEAD_CHARS",
        "PHOSON_OFFLOAD_TAIL_CHARS",
        "PHOSON_COMPACTED_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


def test_compact_modes_are_valid() -> None:
    assert set(COMPACT_MODES) == {"balanced", "aggressive", "off"}


def test_load_config_default_is_balanced(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _clean_env(monkeypatch)

    cfg = load_config()
    assert cfg.compact_mode == "balanced"
    assert cfg.compact_threshold == pytest.approx(0.80)
    assert cfg.compact_min_keep_messages == 4
    assert cfg.offload_tool_outputs is True


def test_load_config_aggressive_preset(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _clean_env(monkeypatch)
    monkeypatch.setenv("PHOSON_COMPACT_MODE", "aggressive")

    cfg = load_config()
    assert cfg.compact_mode == "aggressive"
    assert cfg.compact_threshold == pytest.approx(0.65)
    assert cfg.compact_min_keep_messages == 2


def test_explicit_threshold_wins_over_mode(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _clean_env(monkeypatch)
    monkeypatch.setenv("PHOSON_COMPACT_MODE", "aggressive")
    monkeypatch.setenv("PHOSON_COMPACT_THRESHOLD", "0.5")

    cfg = load_config()
    # Explicit threshold wins over the aggressive preset.
    assert cfg.compact_threshold == pytest.approx(0.5)
    # But the untouched knob still gets the preset.
    assert cfg.compact_min_keep_messages == 2


def test_invalid_mode_falls_back_to_balanced(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _clean_env(monkeypatch)
    monkeypatch.setenv("PHOSON_COMPACT_MODE", "nonsense")

    with pytest.warns(UserWarning):
        cfg = load_config()
    assert cfg.compact_mode == "balanced"
    assert cfg.compact_threshold == pytest.approx(0.80)


def test_compact_mode_persists_to_config_toml(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _clean_env(monkeypatch)

    cfg = PhosonConfig(provider="ollama")
    cfg.compact_mode = "aggressive"
    save_config(cfg, only_fields={"compact_mode"})

    loaded = load_config()
    assert loaded.compact_mode == "aggressive"


# ── controller: plan_compaction + profile ────────────────────────────────


class _FakeSink:
    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    def notify(self, kind: str, message: str) -> None:
        self.notices.append((kind, message))

    def set_session(self, session_id: str) -> None: ...


def _controller(turns: int, **cfg) -> SessionController:
    import asyncio

    with patch("phoson_cli.controller.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        controller = SessionController.__new__(SessionController)
        controller.sink = _FakeSink()
        controller.confirmation = None
        controller.attachments = MagicMock()
        controller.attachments.__bool__ = lambda self: False
        controller.attachments.__len__ = lambda self: 0
        controller._turn_lock = asyncio.Lock()
        controller.summarizer = MagicMock()
        controller.summarizer.min_keep_messages = 4
        controller.summarizer.estimate_tokens = lambda msgs: sum(
            len(str(m.content)) for m in msgs
        )
        controller.summarizer.build_summary_prompt = lambda msgs, **kw: "PROMPT"
        controller.config = SimpleNamespace(
            compact_min_keep_messages=cfg.get("min_keep", 4),
            compact_mode=cfg.get("mode", "balanced"),
            model="test-model",
            reasoning_effort=None,
        )
        controller.chat = MagicMock()
        controller.chat.complete = AsyncMock(
            return_value=LLMDoneEvent(content="Structured summary.")
        )
        controller.current_model = "test-model"
        controller.current_task = None
        controller._save_session = AsyncMock()
        controller._session = SimpleNamespace(
            tree=ConversationTree.new(), metrics=MagicMock(), current_node_id=None
        )
        tree = controller.tree
        parent = None
        for i in range(turns):
            parent = tree.append(parent, Message(role="user", content=f"u{i} " * 20)).id
            parent = tree.append(
                parent, Message(role="assistant", content=f"a{i} " * 40)
            ).id
        controller.current_node_id = parent
    return controller


def test_plan_compaction_reports_split() -> None:
    controller = _controller(turns=6)
    plan = controller.plan_compaction()
    assert plan.ok is True
    assert plan.total_messages == 12
    assert plan.keep_messages == 4  # balanced default
    assert plan.summarize_messages == 8
    assert plan.estimated_tokens > 0
    assert plan.profile == "balanced"


def test_plan_compaction_aggressive_keeps_less() -> None:
    controller = _controller(turns=6)
    plan = controller.plan_compaction("aggressive")
    assert plan.keep_messages == 2  # 4 // 2
    assert plan.summarize_messages == 10
    assert plan.profile == "aggressive"


def test_plan_compaction_short_session() -> None:
    controller = _controller(turns=1)
    plan = controller.plan_compaction()
    assert plan.ok is False
    assert "nothing" in plan.reason.lower()


def test_profile_keep_floor() -> None:
    controller = _controller(turns=1)
    # Even aggressive cannot drop below one kept message.
    controller.config.compact_min_keep_messages = 2
    assert controller._profile_keep("aggressive") == 1
    assert controller._profile_keep("balanced") == 2


@pytest.mark.asyncio
async def test_compact_aggressive_keeps_shorter_tail() -> None:
    controller = _controller(turns=6)
    before, after, changed = await controller.compact_context("aggressive")
    assert changed is True
    path = controller.tree.get_path(controller.current_node_id)
    # 1 summary + keep(2)
    assert len(path) == 3


@pytest.mark.asyncio
async def test_compact_default_profile_keeps_configured_tail() -> None:
    controller = _controller(turns=6)
    before, after, changed = await controller.compact_context()
    assert changed is True
    path = controller.tree.get_path(controller.current_node_id)
    assert len(path) == 1 + 4


def test_path_reasoning_map_reads_node_metadata() -> None:
    controller = _controller(turns=1)
    # Attach reasoning to the assistant node like _persist_run_reasoning.
    node_ids = list(controller.tree.nodes.values())
    assistant = next(n for n in node_ids if n.message.role == "assistant")
    assistant.metadata["reasoning"] = "I chose X because Y."
    mapping = controller._path_reasoning_map(
        controller.tree.get_path(controller.current_node_id)
    )
    assert "I chose X because Y." in mapping.values()


@pytest.mark.asyncio
async def test_compact_reasoning_aligns_with_history_not_path() -> None:
    """Regression (E1): with a system message in the path, the retained
    reasoning must still attach to the assistant message it belongs to
    (indices are computed against the summarized history, not the path)."""
    controller = _controller(turns=6)

    # Use the REAL prompt builder so the reasoning map is actually rendered
    # (the fixture stubs build_summary_prompt to a constant).
    from phoson_agent.plugins.summarizer import SummarizationMiddleware

    real = SummarizationMiddleware(provider="openai", model="m")
    real.estimate_tokens = lambda msgs: sum(len(str(m.content)) for m in msgs)
    controller.summarizer = real

    # Build a fresh path that starts with a system node, reusing the same
    # Message objects as the current tree path so node metadata still
    # resolves by identity.
    old_path = controller.tree.get_path(controller.current_node_id)
    system_node = controller.tree.append(
        parent_id=None, message=Message(role="system", content="be terse")
    )
    cursor = system_node.id
    for msg in old_path:
        cursor = controller.tree.append(parent_id=cursor, message=msg).id
    controller.current_node_id = cursor

    # Reasoning on the first assistant message of the path.
    first_assistant = next(
        n for n in controller.tree.nodes.values() if n.message.role == "assistant"
    )
    first_assistant.metadata["reasoning"] = "FIRST-REASON"

    captured: dict[str, str] = {}

    async def _fake_complete(messages, config):
        captured["prompt"] = str(messages[0].content)
        return LLMDoneEvent(content="## Goal\nok")

    controller.chat.complete = _fake_complete

    _before, _after, changed = await controller.compact_context()
    assert changed is True
    prompt = captured["prompt"]
    # The reasoning appears exactly once ...
    assert prompt.count("FIRST-REASON") == 1
    idx_reasoning = prompt.index("Reasoning:\nFIRST-REASON")
    # ... attached to the first assistant turn: in the rendered history it
    # must follow [ASSISTANT] a0 and come BEFORE [USER] u1. If the index
    # were computed against the full path (system message included), it
    # would be shifted onto [USER] u1 and two [USER] markers would precede
    # it instead of one.
    before_reasoning = prompt[:idx_reasoning]
    assert before_reasoning.count("[USER]") == 1
    assert before_reasoning.count("[ASSISTANT]") == 1


# ── controller: set_compact_mode ─────────────────────────────────────────


def test_set_compact_mode_updates_middlewares_and_persists(
    monkeypatch, tmp_path
) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    controller = _controller(turns=1)
    controller.config = SimpleNamespace(
        compact_min_keep_messages=4,
        compact_mode="balanced",
        model="test-model",
    )
    controller.config.compact_threshold = 0.80
    controller.config.compact_mode = "balanced"
    controller.offload = MagicMock()
    controller._apply_context_config = lambda: None
    saved = {}

    with patch(
        "phoson_cli.controller.save_config",
        side_effect=lambda c, **kw: saved.update(kw),
    ):
        assert controller.set_compact_mode("off") is True
    assert controller.config.compact_mode == "off"
    assert saved.get("only_fields") == {"compact_mode"}

    assert controller.set_compact_mode("bogus") is False
    assert controller.config.compact_mode == "off"


def test_set_compact_mode_on_reenables(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    controller = _controller(turns=1)
    controller.config = SimpleNamespace(
        compact_min_keep_messages=4, compact_mode="off", model="test-model"
    )
    controller._apply_context_config = lambda: None
    with patch("phoson_cli.controller.save_config"):
        assert controller.set_compact_mode("on") is True
    assert (
        controller.config.compact_mode in COMPACT_MODES
        and controller.config.compact_mode != "off"
    )


# ── run_turn registers + clears retained reasoning ───────────────────────


class _RunSink:
    """Minimal sink for driving run_turn without a UI."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self._reasoning = ""

    def on_user_message(self, text, message) -> None: ...
    def on_attachments(self, sources) -> None: ...
    def on_event(self, event) -> None: ...
    def flush_line(self) -> None: ...
    def capture_partial_reasoning(self) -> None: ...

    def take_reasoning(self) -> str:
        r, self._reasoning = self._reasoning, ""
        return r

    def set_session(self, session_id) -> None: ...
    def print_history(self, path, tail=None) -> None: ...

    def notify(self, kind, message) -> None:
        self.notifications.append((kind, message))

    def on_subagent_progress(self, progress) -> None: ...


@pytest.mark.asyncio
async def test_run_turn_registers_and_clears_retained_reasoning(tmp_path) -> None:
    """The controller must register this run's reasoning before the run and
    clear it when the run ends (whatever the terminal state)."""
    controller = _controller(turns=1)
    from phoson_agent.plugins.summarizer import SummarizationMiddleware

    real = SummarizationMiddleware(provider="openai", model="m")
    controller.summarizer = real
    real.estimate_tokens = lambda msgs: sum(len(str(m.content)) for m in msgs)

    # A completed run with reasoning on the assistant node.
    node_ids = list(controller.tree.nodes.values())
    assistant = next(n for n in node_ids if n.message.role == "assistant")
    assistant.metadata["reasoning"] = "chain of thought"

    sink = _RunSink()
    controller.sink = sink

    with patch("phoson_cli.controller.build_chat"):
        fake_engine = MagicMock()
        registered_during_run: list[str] = []

        async def _stream(path, config):
            from phoson_agent.models import AgentDoneEvent, AgentRunResult

            # Mid-run snapshot: reasoning must be registered while the run
            # is in flight.
            registered_during_run.extend(controller.summarizer._retained_by_id.values())
            yield AgentDoneEvent(
                result=AgentRunResult(
                    final_content="final answer",
                    history=list(path)
                    + [Message(role="assistant", content="final answer")],
                    input_messages=list(path),
                    steps=[],
                    total_cost_usd=0.0,
                    total_credits=0.0,
                )
            )

        fake_engine.stream = _stream
        controller.engine = fake_engine
        controller.current_task = None
        controller.session_metrics.add_run_step = MagicMock()
        controller._save_session = AsyncMock()
        controller._refresh_context_window = AsyncMock()

        outcome = await controller.run_turn("hello")

    assert outcome.status == "done"
    # Reasoning was registered for the run ...
    assert "chain of thought" in registered_during_run
    # ... and cleared after it finished.
    assert controller.summarizer._retained_by_id == {}


# ── /compact command (preview + confirm + mode switch) ────────────────────


class _CommandHost:
    def __init__(self, confirm: bool = True) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []
        self.errors: list[str] = []
        self.confirmations: list[str] = []
        self._confirm = confirm

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_warn(self, message: str) -> None:
        self.warns.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def print_help(self, entries) -> None: ...
    def print_renderable(self, renderable) -> None: ...

    async def pick_model(self, models, current_model, **kw): ...
    async def pick_provider(self, providers, current_provider): ...
    async def pick_session(self, sessions, current_id): ...

    async def confirm(self, prompt: str) -> bool:
        self.confirmations.append(prompt)
        return self._confirm

    async def run_setup(self) -> None: ...


def _command_repl(plan: CompactPlan | None = None, running: bool = False):
    """Repl double for /compact: real plan/compact results, stubbed modes."""
    repl = SimpleNamespace()
    repl.is_running = running
    if plan is None:
        plan = CompactPlan(
            ok=True,
            total_messages=12,
            summarize_messages=8,
            keep_messages=4,
            estimated_tokens=9000,
        )
    repl.plan_compaction = lambda profile=None: plan
    repl.compact_context = AsyncMock(return_value=(12_000, 6_000, True))
    repl.set_compact_mode = MagicMock(return_value=True)
    return repl


@pytest.mark.asyncio
async def test_compact_previews_then_confirms() -> None:
    host = _CommandHost(confirm=True)
    repl = _command_repl()
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/compact", args=""))

    assert any("Would summarize 8 of 12" in i for i in host.infos)
    assert host.confirmations
    repl.compact_context.assert_awaited_once_with(None)
    assert any("12,000 → 6,000" in i for i in host.infos)


@pytest.mark.asyncio
async def test_compact_cancelled_leaves_session_alone() -> None:
    host = _CommandHost(confirm=False)
    repl = _command_repl()
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/compact", args=""))

    repl.compact_context.assert_not_awaited()
    assert any("cancelled" in i.lower() for i in host.infos)


@pytest.mark.asyncio
async def test_compact_profile_passed_through() -> None:
    host = _CommandHost(confirm=True)
    repl = _command_repl(
        CompactPlan(
            ok=True,
            total_messages=12,
            summarize_messages=10,
            keep_messages=2,
            estimated_tokens=9000,
            profile="aggressive",
        )
    )
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/compact", args="aggressive"))

    repl.compact_context.assert_awaited_once_with("aggressive")


@pytest.mark.asyncio
async def test_compact_noop_plan_reports_reason() -> None:
    host = _CommandHost()
    repl = _command_repl(
        CompactPlan(
            ok=False, reason="Only 3 turn(s) in context — nothing worth compacting."
        )
    )
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/compact", args=""))

    repl.compact_context.assert_not_awaited()
    assert not host.confirmations
    assert any("nothing worth compacting" in i for i in host.infos)


@pytest.mark.asyncio
async def test_compact_mode_switch_does_not_compact() -> None:
    host = _CommandHost()
    repl = _command_repl()
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/compact", args="off"))

    repl.set_compact_mode.assert_called_once_with("off")
    repl.compact_context.assert_not_awaited()
    assert not host.confirmations


@pytest.mark.asyncio
async def test_compact_unknown_arg_errors() -> None:
    host = _CommandHost()
    repl = _command_repl()
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/compact", args="bogus"))

    assert host.errors
    repl.compact_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_blocked_while_run_in_flight() -> None:
    host = _CommandHost()
    repl = _command_repl(running=True)
    handler = CommandHandler(repl, host=host)

    await handler.handle(Command(name="/compact", args=""))

    repl.compact_context.assert_not_awaited()
    assert host.warns
