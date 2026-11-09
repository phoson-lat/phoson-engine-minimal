"""Bridge <-> engine contract: the ``PhosonRepl`` API the bridge uses exists.

Run it with the engine's interpreter (the bridge needs `phoson_cli`):

    ../phoson-engine-minimal/.venv/bin/python bridge/tests/test_engine_api.py

The bridge reuses ``PhosonRepl`` as a headless engine and calls its methods by
name: if the engine renames one (v0.54.0 replaced ``_run_agent`` with
``run_turn``), the build packages a sidecar that fails on the first turn. This
test builds a real ``PhosonRepl`` from the installed engine and checks every
attribute the bridge uses, so that bumping the engine pin catches it before the
release.

In the other direction, the engine calls the ``GuiSink`` according to its
``AgentEventSink`` protocol: if it adds a parameter (v0.55.0 added
``tool_durations`` to ``print_history``), a sink that does not accept it breaks
session loading. The second test compares each protocol method with the
``GuiSink`` one.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inspect  # noqa: E402

from phoson_cli.repl import PhosonRepl  # noqa: E402
from phoson_cli.config import PhosonConfig  # noqa: E402
from phoson_bridge.sink import GuiSink  # noqa: E402
from phoson_cli.ui_protocols import AgentEventSink  # noqa: E402

#: What `server.py` calls on each session's `PhosonRepl`.
REPL_API = (
    "config",
    "current_model",
    "engine",
    "tree",
    "storage",
    "is_running",
    "session_metrics",
    "run_turn",
    "cancel_current",
    "set_model",
    "set_provider",
    "load_session",
    "undo_last_turn",
    "jump_to_user_turn",
    "jump_candidates",
    "plan_compaction",
    "compact_context",
    "shutdown",
    "_controller",
)

#: What the bridge uses from the `SessionController` (`repl._controller`).
CONTROLLER_API = (
    "attachments",
    "tree",
    "sink",
    "engine",
    "plugin_ui",
    "context_tokens",
    "context_window",
    "command_catalog",
    "_node_path",
    "set_operation_starter",
    "start_monitor_wake_loop",
)


def test_repl_api() -> None:
    config = PhosonConfig(
        provider="ollama", model="test-model", sessions_dir=Path(tempfile.mkdtemp())
    )
    with patch(
        "phoson_cli.controller.build_chat", return_value=MagicMock(aclose=AsyncMock())
    ):
        repl = PhosonRepl(config)
    missing = [name for name in REPL_API if not hasattr(repl, name)]
    missing += [
        f"_controller.{name}"
        for name in CONTROLLER_API
        if not hasattr(repl._controller, name)
    ]
    assert not missing, f"el engine ya no expone: {', '.join(missing)}"


def test_sink_matches_protocol() -> None:
    problems: list[str] = []
    for name, proto_fn in inspect.getmembers(AgentEventSink, inspect.isfunction):
        if name.startswith("_"):
            continue
        impl = getattr(GuiSink, name, None)
        if impl is None:
            problems.append(f"falta {name}()")
            continue
        impl_params = inspect.signature(impl).parameters
        if any(p.kind is p.VAR_KEYWORD for p in impl_params.values()):
            continue
        missing = [
            p for p in inspect.signature(proto_fn).parameters if p not in impl_params
        ]
        if missing:
            problems.append(f"{name}() no acepta {', '.join(missing)}")
    assert not problems, "GuiSink no cumple AgentEventSink: " + "; ".join(problems)


def main() -> None:
    test_repl_api()
    print("ok  test_repl_api")
    test_sink_matches_protocol()
    print("ok  test_sink_matches_protocol")


if __name__ == "__main__":
    main()
