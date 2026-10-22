"""Tool-call detail levels (/details: full · headers · summary) and replays.

Covers the three layers of the feature:

- replay: persisted tool calls render as the same cards as the live view,
  grouped into runs ("tramos") between two pieces of text;
- the full-screen sink: every run (live and replayed) re-renders in place,
  ``summary`` folding a finished run into one ``N tools used`` line;
- the classic renderer (append-only): ``summary`` holds a run's cards and
  prints one line when the run ends;
- the controller: tool durations are persisted on the tree so a resumed
  session shows them.
"""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from phoson_cli.theme import DARK
from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import Message, TextBlock, ToolUseBlock, ToolResultBlock
from phoson_agent.models import (
    RunStep,
    AgentDoneEvent,
    AgentRunResult,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_cli.renderer import Renderer
from phoson_agent.sessions import JsonlStorage, ConversationTree
from phoson_cli.controller import SessionController
from phoson_cli.formatting import ReplayToolGroup, history_blocks, render_history
from phoson_cli.tool_history import (
    TOOL_DURATIONS_KEY,
    next_tool_detail,
    stamp_tool_durations,
    tool_durations_from_nodes,
)
from phoson_cli.fullscreen.sink import FullScreenSink
from phoson_cli.fullscreen.render import render_chat

# ── helpers ──────────────────────────────────────────────────────────────────


def _text(renderable) -> str:
    console = Console(record=True, width=120, highlight=False)
    console.print(renderable)
    return console.export_text()


def _use(call_id: str, tool: str, **args) -> ToolUseBlock:
    return ToolUseBlock(tool_call_id=call_id, tool_name=tool, args=args)


def _result(call_id: str, result: str, error: bool = False) -> ToolResultBlock:
    return ToolResultBlock(tool_call_id=call_id, result=result, error=error)


def _history() -> list[Message]:
    """Two runs: [read_file, bash] then [grep], split by assistant text."""
    return [
        Message(role="user", content="fix it"),
        Message(
            role="assistant",
            content=[
                TextBlock(text="Looking first."),
                _use("r1", "read_file", path="a.py"),
            ],
        ),
        Message(role="user", content=[_result("r1", "print(1)")]),
        # Tool-only message: continues the same run.
        Message(role="assistant", content=[_use("b1", "bash", command="pytest")]),
        Message(role="user", content=[_result("b1", "1 failed", error=True)]),
        Message(
            role="assistant",
            content=[
                TextBlock(text="One test fails."),
                _use("g1", "grep", pattern="def x"),
            ],
        ),
        Message(role="user", content=[_result("g1", "a.py:3")]),
        Message(role="assistant", content="Done."),
    ]


def _make_sink(tool_detail: str = "full") -> FullScreenSink:
    return FullScreenSink(
        on_invalidate=lambda: None, theme=DARK, tool_detail=tool_detail
    )


def _run_tools(sink, calls, *, text_after: str = "") -> None:
    """Drive one live turn with *calls* = [(id, tool, args, result, ms)]."""
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentTokenEvent(content="Before tools.\n"))
    for call_id, tool, args, result, ms in calls:
        sink.on_event(
            AgentToolStartEvent(tool_name=tool, args=args, tool_call_id=call_id)
        )
        sink.on_event(
            AgentToolDoneEvent(
                tool_name=tool, result=result, tool_call_id=call_id, duration_ms=ms
            )
        )
    if text_after:
        sink.on_event(AgentTokenEvent(content=text_after))
    sink.on_event(
        AgentDoneEvent(
            result=AgentRunResult(final_content="", history=[], input_messages=[])
        )
    )


_BASH = ("b1", "bash", {"command": "echo hi"}, "hi-output", 40)
_READ = ("r1", "read_file", {"path": "a.py"}, "contents", 2)


# ── tool_history ─────────────────────────────────────────────────────────────


def test_next_tool_detail_cycles_full_headers_summary() -> None:
    assert next_tool_detail("full") == "headers"
    assert next_tool_detail("headers") == "summary"
    assert next_tool_detail("summary") == "full"
    assert next_tool_detail("bogus") == "summary"


def test_tool_durations_stamped_on_result_nodes_and_read_back() -> None:
    tree = ConversationTree(session_id="s")
    nodes = tree.append_many(
        None,
        [
            Message(role="assistant", content=[_use("r1", "read_file")]),
            Message(role="user", content=[_result("r1", "x")]),
        ],
    )

    stamp_tool_durations(nodes, {"r1": 15, "unrelated": 9})

    assert TOOL_DURATIONS_KEY not in nodes[0].metadata
    assert nodes[1].metadata[TOOL_DURATIONS_KEY] == {"r1": 15}
    assert tool_durations_from_nodes(nodes) == {"r1": 15}


# ── replay grouping and rendering ────────────────────────────────────────────


def test_history_blocks_groups_tool_runs_between_text() -> None:
    blocks = history_blocks(_history(), DARK, tool_durations={"r1": 5, "b1": 1200})
    groups = [b for b in blocks if isinstance(b, ReplayToolGroup)]

    assert [[e.tool_name for e, _ in g.calls] for g in groups] == [
        ["read_file", "bash"],
        ["grep"],
    ]
    read, bash = groups[0].calls
    assert read[0].result == "print(1)" and read[0].duration_ms == 5
    assert read[1] == {"path": "a.py"}
    # A failed result becomes the card's error, not its output.
    assert bash[0].error == "1 failed" and bash[0].result == ""
    assert bash[0].duration_ms == 1200
    assert groups[1].calls[0][0].duration_ms == 0  # unknown


def test_render_history_full_shows_real_tool_cards() -> None:
    out = _text(render_history(_history(), DARK, tool_detail="full"))

    assert "reading file" in out and "a.py" in out
    assert "running command" in out and "pytest" in out
    assert "✗ 1 failed" in out
    # Unknown durations render no "0ms" placeholder.
    assert "0ms" not in out


def test_render_history_summary_folds_each_run() -> None:
    out = _text(
        render_history(
            _history(),
            DARK,
            tool_durations={"r1": 5, "b1": 1200},
            tool_detail="summary",
        )
    )

    assert "2 tools used" in out
    assert "read_file, bash" in out
    assert "1 error" in out
    assert "1.2s" in out  # 5ms + 1200ms
    assert "1 tool used" in out and "grep" in out
    assert "reading file" not in out  # no per-call cards
    # The text around the runs is untouched.
    assert "Looking first." in out and "One test fails." in out


def test_summary_line_counts_repeated_tools() -> None:
    history = [
        Message(
            role="assistant",
            content=[_use(f"r{i}", "read_file", path=f"{i}.py") for i in range(3)],
        ),
        Message(role="user", content=[_result(f"r{i}", "x") for i in range(3)]),
    ]
    out = _text(render_history(history, DARK, tool_detail="summary"))
    assert "3 tools used" in out and "read_file ×3" in out


# ── full-screen sink: live runs ──────────────────────────────────────────────


def test_live_run_folds_into_summary_when_text_follows() -> None:
    sink = _make_sink("summary")
    _run_tools(sink, [_READ, _BASH], text_after="All good.")

    out = render_chat(sink, 120)
    assert "2 tools used" in out
    assert "hi-output" not in out
    # Order is preserved: text, the folded run, then the next text.
    assert (
        out.index("Before tools.") < out.index("2 tools used") < out.index("All good.")
    )


def test_open_run_shows_cards_until_it_ends() -> None:
    """While the run is in flight its calls stay visible as headers."""
    sink = _make_sink("summary")
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(
        AgentToolStartEvent(tool_name="bash", args={"command": "ls"}, tool_call_id="b")
    )
    sink.on_event(AgentToolDoneEvent(tool_name="bash", result="out", tool_call_id="b"))

    out = render_chat(sink, 120)
    assert "running command" in out
    assert "tools used" not in out


def test_details_cycle_rerenders_live_runs_in_place() -> None:
    sink = _make_sink("summary")
    sink.on_user_message("hi", Message(role="user", content="hi"))
    _run_tools(sink, [_READ, _BASH], text_after="After.")
    user_block = sink.blocks[0]

    assert sink.set_tool_details("full") == "full"
    full = render_chat(sink, 120)
    assert "hi-output" in full and "tools used" not in full
    assert (
        full.index("Before tools.") < full.index("reading file") < full.index("After.")
    )

    assert sink.set_tool_details() == "headers"
    headers = render_chat(sink, 120)
    assert "running command" in headers and "hi-output" not in headers

    assert sink.set_tool_details() == "summary"
    assert "2 tools used" in render_chat(sink, 120)
    assert sink.blocks[0] is user_block


def test_error_notice_survives_folding_blocks_before_it() -> None:
    """The notice is tracked by identity: folding earlier runs shifts indexes."""
    from phoson_agent.models import AgentErrorEvent

    sink = _make_sink("full")
    _run_tools(sink, [_READ, _BASH], text_after="After.")
    sink.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    sink.on_event(AgentErrorEvent(message="boom", code=None, retryable=True))

    sink.set_tool_details("summary")  # removes a block before the notice
    sink.drop_error_notice()

    out = render_chat(sink, 120)
    assert "boom" not in out
    assert "2 tools used" in out and "After." in out


def test_user_bang_command_keeps_output_in_summary_mode() -> None:
    sink = _make_sink("summary")
    sink.add_bash_card("ls", "file.py", duration_ms=3)

    out = render_chat(sink, 120)
    assert "file.py" in out
    assert "tools used" not in out


# ── full-screen sink: replays ────────────────────────────────────────────────


def test_replayed_runs_follow_details_toggle() -> None:
    sink = _make_sink("summary")
    sink.print_history(_history(), tool_durations={"b1": 12})

    folded = render_chat(sink, 120)
    assert "2 tools used" in folded and "1 tool used" in folded

    sink.set_tool_details("full")
    expanded = render_chat(sink, 120)
    assert "tools used" not in expanded
    assert "reading file" in expanded and "searching files" in expanded
    assert "12ms" in expanded
    assert expanded.index("Looking first.") < expanded.index("reading file")
    assert expanded.index("reading file") < expanded.index("One test fails.")


# ── classic renderer ─────────────────────────────────────────────────────────


def _classic(tool_detail: str) -> tuple[Renderer, Console]:
    console = Console(record=True, width=120, highlight=False)
    renderer = Renderer(console=console, tool_detail=tool_detail)
    renderer._spinner._enabled = False
    return renderer, console


def _classic_run(renderer: Renderer) -> None:
    renderer.on_event(AgentStartEvent(model="m", message_count=1, max_iterations=5))
    for call_id, tool, args, result, ms in (_READ, _BASH):
        renderer.on_event(
            AgentToolStartEvent(tool_name=tool, args=args, tool_call_id=call_id)
        )
        renderer.on_event(
            AgentToolDoneEvent(
                tool_name=tool, result=result, tool_call_id=call_id, duration_ms=ms
            )
        )
    renderer.on_event(AgentTokenEvent(content="Text after."))
    renderer.on_event(
        AgentDoneEvent(
            result=AgentRunResult(final_content="", history=[], input_messages=[])
        )
    )


def test_classic_summary_prints_one_line_per_run() -> None:
    renderer, console = _classic("summary")
    _classic_run(renderer)

    out = console.export_text()
    assert out.count("tools used") == 1
    assert "2 tools used" in out
    assert "hi-output" not in out
    assert out.index("2 tools used") < out.index("Text after.")


def test_classic_full_and_headers_print_cards() -> None:
    renderer, console = _classic("full")
    _classic_run(renderer)
    assert "hi-output" in console.export_text()

    renderer, console = _classic("headers")
    _classic_run(renderer)
    out = console.export_text()
    assert "running command" in out and "hi-output" not in out


def test_classic_replay_uses_detail_level() -> None:
    renderer, console = _classic("summary")
    renderer.print_history(_history())
    assert "2 tools used" in console.export_text()

    assert renderer.set_tool_details() == "full"
    renderer.print_history(_history())
    assert "reading file" in console.export_text()


# ── controller: durations persisted for replays ──────────────────────────────


class _Sink:
    def __init__(self) -> None:
        self.replays: list[dict | None] = []

    def on_user_message(self, text, message) -> None: ...
    def on_attachments(self, sources) -> None: ...
    def on_event(self, event) -> None: ...
    def flush_line(self) -> None: ...
    def capture_partial_reasoning(self) -> None: ...
    def take_reasoning(self) -> str:
        return ""

    def set_session(self, session_id) -> None: ...
    def notify(self, kind, message) -> None: ...
    def on_subagent_progress(self, progress) -> None: ...

    def print_history(self, path, tail=None, timestamps=None, tool_durations=None):
        self.replays.append(tool_durations)


def _controller(tmp_path) -> tuple[SessionController, _Sink]:
    sink = _Sink()
    config = PhosonConfig(provider="ollama", model="test-model", sessions_dir=tmp_path)
    with patch(
        "phoson_cli.controller.build_chat",
        return_value=MagicMock(aclose=AsyncMock()),
    ):
        return SessionController(config, sink), sink


@pytest.mark.asyncio
async def test_tool_durations_persist_and_reach_the_replay(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    session_id = controller.tree.session_id
    now = datetime.datetime.now(datetime.UTC)
    step = AgentStepDoneEvent(
        step=RunStep(kind="tool", started_at=now, ended_at=now, duration_ms=1)
    )

    async def stream(path, config):
        history = controller.engine._history
        history[:] = list(path)
        yield AgentStartEvent(model="m", message_count=1, max_iterations=5)
        history.append(Message(role="assistant", content=[_use("tc_1", "bash")]))
        yield AgentToolStartEvent(tool_name="bash", tool_call_id="tc_1")
        yield AgentToolDoneEvent(
            tool_name="bash", result="ok", tool_call_id="tc_1", duration_ms=1234
        )
        history.append(Message(role="user", content=[_result("tc_1", "ok")]))
        yield step
        history.append(Message(role="assistant", content="done"))
        yield AgentDoneEvent(
            result=AgentRunResult(
                final_content="done", history=list(history), input_messages=[]
            )
        )

    controller.engine.stream = stream
    await controller.run_turn("q")

    tree = await JsonlStorage(base_path=tmp_path).load(session_id)
    stored = [
        node.metadata[TOOL_DURATIONS_KEY]
        for node in tree.nodes.values()
        if TOOL_DURATIONS_KEY in node.metadata
    ]
    assert stored == [{"tc_1": 1234}]

    resumed, resumed_sink = _controller(tmp_path)
    outcome = await resumed.load_session(session_id)
    assert outcome.ok
    assert resumed_sink.replays == [{"tc_1": 1234}]


# ── config + /details command ────────────────────────────────────────────────


def _isolated_home(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".phoson").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PHOSON_TOOL_DETAIL", raising=False)


def test_config_tool_detail_defaults_to_summary_and_round_trips(
    monkeypatch, tmp_path
) -> None:
    from phoson_cli.config import load_config, save_config

    _isolated_home(monkeypatch, tmp_path)
    cfg = load_config()
    assert cfg.tool_detail == "summary"

    cfg.tool_detail = "headers"
    save_config(cfg, only_fields={"tool_detail"})
    assert load_config().tool_detail == "headers"


def test_config_invalid_tool_detail_falls_back_with_warning(
    monkeypatch, tmp_path
) -> None:
    from phoson_cli.config import load_config

    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("PHOSON_TOOL_DETAIL", "verbose")
    with pytest.warns(UserWarning, match="tool_detail"):
        cfg = load_config()
    assert cfg.tool_detail == "summary"


class _Host:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_warn(self, message: str) -> None: ...

    def print_error(self, message: str) -> None:
        self.errors.append(message)


@pytest.mark.asyncio
async def test_details_command_sets_level_and_saves_it() -> None:
    from types import SimpleNamespace

    from phoson_cli.commands import Command, CommandHandler

    sink = _make_sink("full")
    session = SimpleNamespace(
        is_running=False, sink=sink, config=PhosonConfig(tool_detail="full")
    )
    host = _Host()
    handler = CommandHandler(session, host=host)  # type: ignore[arg-type]

    with patch("phoson_cli.commands.save_config") as save:
        await handler.handle(Command(name="/details", args=""))
        await handler.handle(Command(name="/details", args="summary"))
        await handler.handle(Command(name="/details", args="verbose"))

    assert sink.tool_detail == "summary"
    assert session.config.tool_detail == "summary"
    assert save.call_count == 2
    assert save.call_args.kwargs == {"only_fields": {"tool_detail"}}
    assert host.infos == [
        "Tool details → headers  ·  saved",
        "Tool details → summary  ·  saved",
    ]
    assert host.errors and "verbose" in host.errors[0]
