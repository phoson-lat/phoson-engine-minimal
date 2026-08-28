#!/usr/bin/env python
"""I-84 phase 0: CPU / render-pass baseline for the full-screen TUI.

Runs the real ``PhosonApp`` headless (PipeInput + DummyOutput, same
pattern as ``tests/phoson_cli/fullscreen/test_e2e_tui.py``) against a
synthetic streaming scenario and measures, per phase:

  * idle        — the app sits waiting for input
  * thinking    — a turn is active, provider silent (activity ticker only)
  * streaming   — tokens arrive at --token-rate (touch_streaming throttle)

Metrics per phase:
  * wall duration
  * CPU seconds consumed by the process (os.times: user + system)
  * CPU%         = cpu_seconds / wall * 100
  * render passes (prompt_toolkit ``Application.render_counter`` delta)
  * fps         = renders / wall

Usage:
  python scripts/bench_i84_cpu.py                 # defaults
  python scripts/bench_i84_cpu.py --tokens 2000 --token-rate 60
  python scripts/bench_i84_cpu.py --idle 5 --thinking 3 --stream 4

Exit code 0; the output table is copy-pasteable into the PR evidence.
"""

import os
import sys
import time
import asyncio
import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from phoson_agent import (  # noqa: E402
    AgentDoneEvent,
    AgentStartEvent,
    AgentTokenEvent,
    AgentStepDoneEvent,
)
from phoson_cli.config import PhosonConfig  # noqa: E402
from phoson_llm.schemas import Message  # noqa: E402
from phoson_agent.models import AgentRunResult  # noqa: E402
from phoson_cli.fullscreen.app import PhosonApp  # noqa: E402


def _done_result() -> AgentRunResult:
    user = Message(role="user", content="bench turn")
    assistant = Message(role="assistant", content="streamed answer")
    return AgentRunResult(
        final_content="streamed answer",
        history=[user, assistant],
        input_messages=[user],
        steps=[],
    )


def _cpu_seconds() -> float:
    t = os.times()
    return t.user + t.system


async def _measure(label: str, duration: float, render_counter) -> dict:
    start_wall = time.monotonic()
    start_cpu = _cpu_seconds()
    start_renders = render_counter()
    await asyncio.sleep(duration)
    wall = time.monotonic() - start_wall
    cpu = _cpu_seconds() - start_cpu
    renders = render_counter() - start_renders
    row = {
        "phase": label,
        "wall": wall,
        "cpu": cpu,
        "cpu_pct": cpu / wall * 100 if wall > 0 else 0.0,
        "renders": renders,
        "fps": renders / wall if wall > 0 else 0.0,
    }
    print(
        f"  {row['phase']:<10} wall={row['wall']:6.2f}s "
        f"cpu={row['cpu']:6.3f}s ({row['cpu_pct']:5.1f}%) "
        f"renders={row['renders']:5d} ({row['fps']:5.1f} fps)",
        flush=True,
    )
    return row


async def _run(args: argparse.Namespace) -> None:
    import tempfile

    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        config = PhosonConfig(
            provider="ollama",
            sessions_dir=Path(tempfile.mkdtemp(prefix="phoson-bench-")),
            history_file=Path(tempfile.mkdtemp(prefix="phoson-bench-")) / "h.txt",
        )
        with create_pipe_input() as pipe:
            app = PhosonApp(config)
            app.app.input = pipe
            # The Renderer captured the session's real stdout at
            # __init__ time; swap its output (not just the app attribute)
            # so we measure layout+ANSI cost without terminal writes.
            # The final redraw (render_as_done, after Ctrl+C) queries the
            # output's color depth; DummyOutput lacks that method.
            dummy = DummyOutput()
            dummy.get_default_color_depth = lambda: "truecolor"
            app.app.output = dummy
            app.app.renderer.output = dummy

            def render_counter() -> int:
                return app.app.render_counter or 0

            async def app_task():
                try:
                    await app.app.run_async()
                finally:
                    await app.repl.shutdown()

            app_p = asyncio.create_task(app_task())

            # Give the app a moment to paint the first frame.
            await asyncio.sleep(0.3)

            rows: list[dict] = []

            # ── Phase 1: idle (no turn in flight) ─────────────────────
            rows.append(await _measure("idle", args.idle, render_counter))

            # ── Phases 2+3: synthetic turn (thinking + streaming) ─────
            # Patch _run_agent the same way the e2e tests do, so the
            # real PhosonApp code path (ticker, throttle, invalidation)
            # runs unchanged — only the provider is synthetic.

            async def mock_run_agent(prompt: str) -> None:  # noqa: ARG001
                # Thinking phase: provider silent, activity ticker runs.
                app.sink.on_event(
                    AgentStartEvent(
                        model="bench",
                        message_count=2,
                        max_iterations=8,
                    )
                )
                await _measure("thinking", args.thinking, render_counter)

                # Streaming phase: N tokens at a fixed rate.
                start_renders = render_counter()
                start_wall = time.monotonic()
                start_cpu = _cpu_seconds()
                # Streaming phase: N tokens emitted in bursts (default 10
                # tokens / 0.08 s pause) to mimic network chunking — a
                # perfectly even 1 token/tick loop would let each token
                # win its own repaint and overstate renders.
                burst = max(1, args.token_burst)
                emitted = 0
                while emitted < args.tokens:
                    for _ in range(min(burst, args.tokens - emitted)):
                        app.sink.on_event(AgentTokenEvent(content="x "))
                        emitted += 1
                    await asyncio.sleep(args.token_burst_pause)
                app.sink.on_event(AgentStepDoneEvent(step=MagicMock(cost_usd=0.0)))
                wall = time.monotonic() - start_wall
                cpu = _cpu_seconds() - start_cpu
                renders = render_counter() - start_renders
                row = {
                    "phase": "streaming",
                    "wall": wall,
                    "cpu": cpu,
                    "cpu_pct": cpu / wall * 100 if wall > 0 else 0.0,
                    "renders": renders,
                    "fps": renders / wall if wall > 0 else 0.0,
                }
                print(
                    f"  {row['phase']:<10} wall={row['wall']:6.2f}s "
                    f"cpu={row['cpu']:6.3f}s ({row['cpu_pct']:5.1f}%) "
                    f"renders={row['renders']:5d} ({row['fps']:5.1f} fps)",
                    flush=True,
                )
                rows.append(row)
                app.sink.on_event(AgentDoneEvent(result=_done_result()))

            with patch.object(app.repl, "_run_agent", side_effect=mock_run_agent):
                # Drive a real turn through the app's submit path; the
                # mock does the thinking+streaming measurement. Must be a
                # background task: pipe.send_text blocks until read and
                # the reader only runs while the loop is free.
                async def drive():
                    await asyncio.sleep(0.1)
                    pipe.send_text("bench turn")
                    await asyncio.sleep(0.1)
                    pipe.send_text("\r")

                asyncio.create_task(drive())
                # Wait for the turn to fully settle (run task done and no
                # live turn in the sink) before leaving the patched scope.
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    run_task = app._run_task
                    if (
                        run_task is not None
                        and run_task.done()
                        and app.sink.current_turn is None
                    ):
                        break
                    await asyncio.sleep(0.05)

                # ── Phase 4: idle again (ticker must be cancelled) ────
                await asyncio.sleep(0.3)
                rows.append(await _measure("idle-post", args.idle, render_counter))

            # ── Summary ───────────────────────────────────────────────
            print()
            print(f"{'phase':<10} {'wall':>8} {'cpu%':>7} {'renders':>8} {'fps':>7}")
            for r in rows:
                print(
                    f"{r['phase']:<10} {r['wall']:8.2f} {r['cpu_pct']:7.1f} "
                    f"{r['renders']:8d} {r['fps']:7.1f}"
                )

            pipe.send_text("\x03")  # Ctrl+C exit
            await asyncio.wait_for(app_p, timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--idle",
        type=float,
        default=3.0,
        help="idle measurement duration seconds (default 3)",
    )
    parser.add_argument(
        "--thinking", type=float, default=3.0, help="thinking phase seconds (default 3)"
    )
    parser.add_argument(
        "--stream", type=float, default=4.0, help="streaming phase seconds (default 4)"
    )
    parser.add_argument(
        "--tokens", type=int, default=240, help="token count (default 240)"
    )
    parser.add_argument(
        "--token-rate", type=float, default=60.0, help="tokens per second (default 60)"
    )
    parser.add_argument(
        "--token-burst", type=int, default=10, help="tokens per burst (default 10)"
    )
    parser.add_argument(
        "--token-burst-pause",
        type=float,
        default=0.08,
        help="pause between bursts, seconds (default 0.08)",
    )
    args = parser.parse_args()

    args.tokens = int(args.stream * args.token_rate)
    print(
        f"bench i84: idle={args.idle}s thinking={args.thinking}s "
        f"streaming={args.stream}s @ {args.token_rate} tok/s "
        f"(burst {args.token_burst} / {args.token_burst_pause}s, "
        f"{args.tokens} tokens)",
        flush=True,
    )
    # Keep the process in the foreground so the measurement window is
    # not dominated by interpreter warm-up.
    os.environ.setdefault("PHOSON_PERF", "1")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
