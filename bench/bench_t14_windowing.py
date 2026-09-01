#!/usr/bin/env python3
"""T-14 (#171) — per-frame chat-pane cost, before vs. after windowing.

The pre-fix full-screen TUI handed prompt_toolkit the *entire* transcript
every frame, so its ``to_formatted_text(ANSI(full))`` / ``split_lines`` /
``tuple()+hash`` passes ran over the whole transcript — O(total lines). The
fix windows the pane: the app caches the full transcript once per width and
hands ptk only the visible slice, so that per-frame work is O(visible lines).

This benchmark reproduces both per-frame shapes over a *real* rendered
transcript (Rich ``render_chat`` output through a ``FullScreenSink``) and
prints a table showing the full-transcript cost growing with session length
while the windowed cost stays flat. It is a pure-CPU harness (no provider,
no terminal, no network), so it runs anywhere ``pytest`` does.

Run:

    uv run python bench/bench_t14_windowing.py

Exit status is 0 when the windowed path is flat and clearly cheaper than the
full path at the largest size (the acceptance criterion for the fix);
non-zero otherwise, so it can gate a release.
"""

import time

from prompt_toolkit.formatted_text import ANSI, to_formatted_text

from phoson_cli.theme import load_theme
from phoson_agent.models import AgentToolDoneEvent
from phoson_cli.formatting import (
    render_user_turn,
    render_tool_done_line,
    render_streaming_panel,
)
from phoson_cli.fullscreen.sink import FullScreenSink
from phoson_cli.fullscreen.render import (
    render_chat,
    windowed_slice,
    _line_boundaries,
)

# A realistic turn: one user line, one bash tool card, one Markdown answer.
# Each turn is ~20 visual lines, so 1000 turns is a ~20k-line transcript —
# the "very long session" regime the original issue was measured against.
_USER = "Run the test suite and tell me what failed."
_ANSWER = """Here is what I found:

- **3 failed** in `tests/phoson_cli/`, all in the full-screen pane.
- The rest (1200+) passed.

```python
def test_example():
    assert True
```

Re-run with `--classic` to compare. The fix is small."""
_TOOL = {"tool": "bash", "args": {"command": "pytest -q"}}


def _make_sink(turns: int, theme) -> FullScreenSink:
    """Build a ``FullScreenSink`` whose transcript is *turns* realistic turns."""
    sink = FullScreenSink(on_invalidate=lambda: None, theme=theme)
    for i in range(turns):
        sink.blocks.append(render_user_turn(_USER, theme))
        done = AgentToolDoneEvent(tool_call_id=f"tc{i}", tool_name=_TOOL["tool"])
        sink.blocks.append(render_tool_done_line(done, theme))
        sink.blocks.append(render_streaming_panel(_ANSWER, "", False, theme))
    return sink


def _frame_full(full_text: str) -> int:
    """Per-frame work ptk did *before* windowing: parse the WHOLE transcript.

    Returns the fragment count produced by ``to_formatted_text(ANSI(full))``
    — the list every downstream ``split_lines`` / ``tuple()+hash`` pass then
    walked over the full transcript.
    """
    return len(to_formatted_text(ANSI(full_text)))


def _frame_windowed(full_text: str, bounds: list[int], top: int, height: int) -> int:
    """Per-frame work ptk does *after* windowing: parse the VISIBLE slice only.

    The slice is O(visible) string work and the ANSI parse is O(visible), so
    the returned fragment count stays tiny regardless of transcript length.
    """
    slice_text = windowed_slice(full_text, bounds, top, height)
    return len(to_formatted_text(ANSI(slice_text)))


def _time(fn, *, repeats: int) -> float:
    """Best-of wall time in ms over *repeats* calls (timing noise → minimum)."""
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - t0
        if elapsed < best:
            best = elapsed
    return best * 1e3


def main() -> int:
    width = 116
    height = 24  # visible chat window (≈ terminal lines minus chrome)
    sizes = [40, 200, 500, 1000]
    repeats = 5
    theme = load_theme("dark")

    print(f"T-14 windowing — per-frame chat-pane cost (width={width}, window={height})")
    print(
        f"{'turns':>6} {'lines':>7} {'KB':>7} | "
        f"{'full(ms)':>9} {'win(ms)':>9} | {'full frag':>10} {'win frag':>8}"
    )
    print("-" * 64)

    full_times: list[float] = []
    win_times: list[float] = []
    full_frags: list[int] = []
    win_frags: list[int] = []

    for turns in sizes:
        sink = _make_sink(turns, theme)
        full_text = render_chat(sink, width)
        bounds = _line_boundaries(full_text)
        total = len(bounds)
        top = max(0, total - height)  # auto-scroll: pinned to the bottom
        kb = len(full_text) / 1024

        full_frags.append(_frame_full(full_text))
        win_frags.append(_frame_windowed(full_text, bounds, top, height))
        ft = _time(lambda: _frame_full(full_text), repeats=repeats)
        wt = _time(
            lambda: _frame_windowed(full_text, bounds, top, height),
            repeats=repeats,
        )
        full_times.append(ft)
        win_times.append(wt)

        print(
            f"{turns:>6} {total:>7} {kb:>7.0f} | "
            f"{ft:>9.3f} {wt:>9.3f} | {full_frags[-1]:>10} {win_frags[-1]:>8}"
        )

    # Acceptance: the windowed cost must be (a) flat across the small→large
    # span and (b) well under the full-transcript cost at the largest size.
    win_first, win_last = win_times[0], win_times[-1]
    win_flat_ratio = win_last / win_first if win_first > 0 else float("inf")
    speedup = full_times[-1] / win_last if win_last > 0 else float("inf")

    print("-" * 64)
    print(
        f"windowed cost small→large: {win_first:.3f} → {win_last:.3f} ms "
        f"(×{win_flat_ratio:.2f} over the whole span)"
    )
    print(
        f"speedup at {sizes[-1]} turns: {full_times[-1]:.3f} ms full vs "
        f"{win_last:.3f} ms windowed (×{speedup:.1f})"
    )

    # Flat = the last windowed sample within 4× the first (timing noise).
    flat = win_flat_ratio <= 4.0
    faster = speedup >= 2.0
    fragments_smaller = win_frags[-1] < full_frags[-1]
    ok = flat and faster and fragments_smaller
    print(
        f"\nRESULT: {'PASS' if ok else 'FAIL'} "
        f"(flat={'yes' if flat else 'no'}, "
        f"windowed-cheaper-at-large={'yes' if faster else 'no'}, "
        f"fragments-smaller={'yes' if fragments_smaller else 'no'})"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
