"""Tests for IMPROVEMENTS.md C1 — rich tool cards.

Covers the pure formatters (verbs, detail lines, unified diffs, write
summaries, bash previews) and the front ends' arg-recall plumbing that
lets a done card render its detail from the matching start event.
"""

from rich.console import Console

from phoson_cli.theme import DARK
from phoson_agent.models import (
    AgentToolDoneEvent,
    AgentToolStartEvent,
)
from phoson_cli.formatting import (
    tool_verb,
    error_hint,
    tool_detail,
    unified_diff,
    render_tool_done_line,
    render_tool_start_line,
)


def _render(renderable) -> str:
    console = Console(highlight=False, width=120)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# ─── verbs and details ───────────────────────────────────────────────────────


def test_tool_verb_maps_known_tools_to_human_phrases() -> None:
    assert tool_verb("write_file") == "writing file"
    assert tool_verb("bash") == "running command"
    assert tool_verb("web_search") == "searching the web"


def test_tool_verb_falls_back_for_unknown_tools() -> None:
    assert tool_verb("my_custom_tool") == "my custom tool"


def test_tool_detail_extracts_path_query_url_and_command() -> None:
    assert tool_detail("read_file", {"path": "a/b.py"}) == "a/b.py"
    assert tool_detail("web_search", {"query": "phoson"}) == "phoson"
    assert tool_detail("web_fetch", {"url": "https://x.dev"}) == "https://x.dev"
    assert "pytest -q" in tool_detail("bash", {"command": "pytest -q"})


def test_tool_detail_truncates_long_commands() -> None:
    cmd = "x" * 100
    detail = tool_detail("bash", {"command": cmd})
    assert len(detail) == 73
    assert detail.endswith("…")


# ─── unified diff ────────────────────────────────────────────────────────────


def test_unified_diff_marks_changes_and_is_empty_when_identical() -> None:
    diff = unified_diff("a\nb\n", "a\nc\n", "f.py")
    assert any(line.startswith("-b") for line in diff)
    assert any(line.startswith("+c") for line in diff)
    assert unified_diff("same\n", "same\n", "f.py") == []


# ─── start card ──────────────────────────────────────────────────────────────


def test_start_card_shows_actionable_verb_and_path() -> None:
    event = AgentToolStartEvent(tool_name="patch_file", args={"path": "src/app.py"})
    output = _render(render_tool_start_line(event, DARK))
    assert "editing file" in output
    assert "src/app.py" in output
    # The raw tool name is no longer the headline (human verb instead).
    assert "patch_file" not in output


def test_bash_start_card_shows_the_command() -> None:
    event = AgentToolStartEvent(tool_name="bash", args={"command": "git status"})
    output = _render(render_tool_start_line(event, DARK))
    assert "running command" in output
    assert "git status" in output


# ─── done card bodies ────────────────────────────────────────────────────────


def test_patch_card_renders_colored_diff_from_args() -> None:
    start = AgentToolStartEvent(
        tool_name="patch_file",
        args={
            "path": "calc.py",
            "old_content": "x = 1\n",
            "new_content": "x = 2\n",
        },
        tool_call_id="c1",
    )
    done = AgentToolDoneEvent(
        tool_name="patch_file", result="Replaced 1", duration_ms=9, tool_call_id="c1"
    )
    output = _render(render_tool_done_line(done, DARK, args=start.args))
    assert "-x = 1" in output
    assert "+x = 2" in output
    assert "@@" in output
    assert "✓" in output


def test_patch_card_diff_lines_carry_a_background() -> None:
    """T-7: +/− lines render with a subtle background, not just a prefix color."""
    import io

    done = AgentToolDoneEvent(
        tool_name="patch_file", result="Replaced 1", duration_ms=9
    )
    card = render_tool_done_line(
        done,
        DARK,
        args={"path": "f.py", "old_content": "a\nb\n", "new_content": "a\nc\n"},
    )
    buf = io.StringIO()
    # force_terminal + truecolor: capture() would strip the SGR we're checking.
    console = Console(
        file=buf, force_terminal=True, color_system="truecolor", width=120
    )
    console.print(card)
    output = buf.getvalue()
    # Truecolor background introducer ("48;2;r;g;b" — or "48;5;n" in
    # 256-color mode): the + and - lines must carry one (the DARK tier
    # sets diff_add_bg/diff_del_bg).
    assert "48;2" in output or "48;5" in output
    raw_plus = next(
        line for line in output.splitlines() if "+c" in line or "+a" in line
    )
    raw_minus = next(line for line in output.splitlines() if "-b" in line)
    assert ("48;2" in raw_plus) or ("48;5" in raw_plus)
    assert ("48;2" in raw_minus) or ("48;5" in raw_minus)


def test_patch_card_diff_background_respects_the_theme() -> None:
    """T-7: tiers without diff backgrounds (ansi) stay prefix-color only."""
    import io

    from phoson_cli.theme import ANSI

    done = AgentToolDoneEvent(
        tool_name="patch_file", result="Replaced 1", duration_ms=9
    )
    card = render_tool_done_line(
        done,
        ANSI,
        args={"path": "f.py", "old_content": "a\nb\n", "new_content": "a\nc\n"},
    )
    buf = io.StringIO()
    console = Console(
        file=buf, force_terminal=True, color_system="truecolor", width=120
    )
    console.print(card)
    output = buf.getvalue()
    # The ANSI tier sets diff_add_bg/diff_del_bg="" → no background SGR.
    assert "48;2" not in output and "48;5" not in output
    # Prefix color still distinguishes +/- (bright_red/bright_green).
    assert "-b" in output and "+c" in output


def test_patch_card_truncates_long_diffs_with_a_notice() -> None:
    old = "\n".join(f"line {i}" for i in range(40)) + "\n"
    new = "\n".join(f"line {i}!" for i in range(40)) + "\n"
    done = AgentToolDoneEvent(tool_name="patch_file", result="ok", duration_ms=5)
    output = _render(
        render_tool_done_line(
            done, DARK, args={"path": "big.py", "old_content": old, "new_content": new}
        )
    )
    assert "more diff lines" in output
    assert "line 39!" not in output  # beyond the truncation point


def test_patch_card_error_shows_error_not_diff() -> None:
    done = AgentToolDoneEvent(
        tool_name="patch_file",
        result="",
        error="old_content not found in f.py",
        duration_ms=3,
    )
    output = _render(render_tool_done_line(done, DARK))
    assert "✗" in output
    assert "old_content not found" in output
    assert "+@" not in output


def test_write_card_summarizes_created_file_with_lines_and_size() -> None:
    done = AgentToolDoneEvent(tool_name="write_file", result="Created", duration_ms=2)
    output = _render(
        render_tool_done_line(
            done, DARK, args={"path": "src/new.py", "content": "a\nb\n"}
        )
    )
    assert "created src/new.py" in output
    assert "2 lines" in output
    assert "B" in output  # byte size shown


def test_write_card_says_updated_on_overwrite() -> None:
    """T-7: an overwrite must not be claimed as a creation."""
    done = AgentToolDoneEvent(
        tool_name="write_file", result="Updated: src/app.py (12 bytes)", duration_ms=2
    )
    output = _render(
        render_tool_done_line(
            done, DARK, args={"path": "src/app.py", "content": "x\ny\n"}
        )
    )
    assert "updated src/app.py" in output
    assert "created src/app.py" not in output


def test_tool_icons_differ_per_family() -> None:
    """T-7: one glyph for everything read as one action."""
    from phoson_cli.formatting import tool_icon

    assert tool_icon("read_file") != tool_icon("bash")
    assert tool_icon("read_file") != tool_icon("write_file")
    assert tool_icon("write_file") != tool_icon("patch_file")
    assert tool_icon("web_fetch") != tool_icon("web_search")
    assert tool_icon("unknown_tool") == "⚙"  # fallback stays the gear


def test_tool_card_path_is_osc8_link() -> None:
    """T-7: file paths in card headers are real OSC 8 file:// links."""
    import io

    from rich.console import Console

    event = AgentToolStartEvent(tool_name="read_file", args={"path": "src/app.py"})
    buf = io.StringIO()
    # force_terminal like the app's production render: Rich only emits the
    # OSC 8 escape when it believes a real terminal is on the other end.
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=120,
        highlight=False,
    )
    console.print(render_tool_start_line(event, DARK))
    out = buf.getvalue()
    assert "file://" in out
    assert "src/app.py" in out
    # The URI resolves relative paths against the cwd.
    import os

    assert os.path.abspath("src/app.py").replace("\\", "/") in out


def test_write_card_error_has_no_summary_body() -> None:
    done = AgentToolDoneEvent(
        tool_name="write_file", result="", error="Permission denied", duration_ms=1
    )
    output = _render(
        render_tool_done_line(done, DARK, args={"path": "/etc/x", "content": "y"})
    )
    assert "created /etc/x" not in output
    assert "Permission denied" in output


def test_bash_card_previews_output_lines() -> None:
    done = AgentToolDoneEvent(
        tool_name="bash",
        result="\n".join(f"out {i}" for i in range(10)),
        duration_ms=50,
    )
    output = _render(render_tool_done_line(done, DARK, args={"command": "ls"}))
    assert "out 0" in output
    assert "more lines" in output
    assert "out 9" not in output  # beyond the preview cap


def test_done_card_without_args_still_renders() -> None:
    """Done events without remembered args degrade to verb + outcome."""
    done = AgentToolDoneEvent(tool_name="bash", result="hi", duration_ms=7)
    output = _render(render_tool_done_line(done, DARK))
    assert "running command" in output
    assert "✓" in output
    assert "7ms" in output


# ─── error hints (C4 formatter side) ────────────────────────────────────────


def test_error_hint_known_codes() -> None:
    assert error_hint("auth") is not None
    assert error_hint("rate_limit") is not None
    assert error_hint("max_iterations") is not None


def test_error_hint_unknown_code_returns_none() -> None:
    assert error_hint("mystery") is None
    assert error_hint(None) is None
