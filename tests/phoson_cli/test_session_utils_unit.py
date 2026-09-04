"""Unit tests for ``phoson_cli.session_utils.build_system_prompt``.

Covers the prompt-accuracy and prompt-caching fixes:

- B1: the clock must use the *system* timezone, not a hardcoded zone.
- B2: the tool list must be derived from the actual registry, not a
  hardcoded string.
- G2: the prompt is the stable prefix of every request (prompt caching),
  so a live clock (hours/minutes/seconds) must not appear in it.
"""

import time
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from phoson_cli.session_utils import build_system_prompt


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


# ── B1: timezone ─────────────────────────────────────────────────────────────


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset is POSIX-only")
def test_system_prompt_uses_system_timezone(monkeypatch) -> None:
    """B1: with TZ=Europe/Madrid the prompt must report Madrid, not CDMX."""
    monkeypatch.setenv("TZ", "Europe/Madrid")
    time.tzset()
    try:
        prompt = build_system_prompt([_tool("bash")])
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()

    expected = datetime.now(ZoneInfo("Europe/Madrid"))
    tzname = expected.tzname()
    offset = expected.strftime("%z")  # e.g. "+0200"
    pretty_offset = f"{offset[:3]}:{offset[3:]}"  # "+02:00"

    assert f"Current timezone is: {tzname}" in prompt
    assert pretty_offset in prompt
    # Regression guard: the old hardcoded zone must be gone.
    assert "America/Mexico_City" not in prompt


def test_system_prompt_always_reports_a_timezone() -> None:
    """Smoke: the prompt always carries a 'Current timezone is:' label."""
    prompt = build_system_prompt([_tool("bash")])
    assert "Current timezone is:" in prompt


# ── B2: tool list derived from the registry ──────────────────────────────────


def test_system_prompt_tool_list_matches_registry_exactly() -> None:
    """B2: the 'Available tools:' segment must equal the registry, sorted."""
    names = ["bash", "read_file", "zeta_tool", "alpha_tool"]
    prompt = build_system_prompt([_tool(n) for n in names])

    start = prompt.index("Available tools: ") + len("Available tools: ")
    end = prompt.index(".", start)
    listed = prompt[start:end].strip()

    assert listed == "alpha_tool, bash, read_file, zeta_tool"


def test_system_prompt_omits_unregistered_tools() -> None:
    """B2: a tool not in the registry must not be advertised to the model."""
    prompt = build_system_prompt([_tool("bash"), _tool("custom_tool")])

    assert "bash" in prompt
    assert "custom_tool" in prompt
    # None of the built-in names that are NOT registered may leak in.
    assert "write_file" not in prompt
    assert "patch_file" not in prompt


def test_system_prompt_mcp_note_still_works() -> None:
    """Pre-existing behaviour: MCP tools get their own note."""
    prompt = build_system_prompt([_tool("bash"), _tool("mcp_github_get_user")])
    assert "MCP tools (names prefixed 'mcp_') are also available" in prompt
    assert "mcp_github_get_user" in prompt


# ── G2: stable prefix for prompt caching ─────────────────────────────────────


def test_system_prompt_uses_date_not_live_clock() -> None:
    """G2: the prefix carries the date, never hours/minutes/seconds.

    A live clock would change the system prompt on every request and
    bust the provider's prompt cache for the entire prefix.
    """
    now = datetime.now().astimezone()
    prompt = build_system_prompt([_tool("bash")])
    assert f"Current date is {now.strftime('%Y-%m-%d')}" in prompt
    assert now.strftime("%H:%M:%S") not in prompt
    # The old wording must be gone entirely.
    assert "Current time is" not in prompt


def test_system_prompt_is_stable_across_builds() -> None:
    """G2: two builds in the same session produce byte-identical prefixes.

    This is the property the prompt cache actually needs: anything that
    changes between turns (time of day, run state) must not appear.
    """
    first = build_system_prompt([_tool("bash"), _tool("agent")])
    second = build_system_prompt([_tool("bash"), _tool("agent")])
    assert first == second


# ── #180: ACI sections (Tool usage / Environment / Safety) ────────────────────


def _cwd_bound_prompt(tmp_path, tools):
    """Build the prompt with ``Path.cwd()`` bound to ``tmp_path``.

    The Environment (git) block is derived from the working directory;
    binding the prompt builder to a controlled temp dir keeps the tests
    deterministic regardless of where the suite is run.
    """
    from pathlib import Path

    import phoson_cli.session_utils as su

    class _BoundPath(Path):
        @classmethod
        def cwd(cls):  # type: ignore[override]
            return tmp_path

    original = su.Path
    su.Path = _BoundPath
    try:
        return build_system_prompt([_tool(n) for n in tools])
    finally:
        su.Path = original


def test_tool_usage_gated_on_registered_tools() -> None:
    """Each Tool-usage line is only advertised for tools that exist."""
    # Full file-editing set: edit preference + line-number caveat.
    prompt = _cwd_bound_prompt(
        Path.home(), {"bash", "read_file", "patch_file", "agent", "agents"}
    )
    assert "# Tool usage" in prompt
    assert "Prefer patch_file for targeted edits" in prompt
    assert "read_file shows line numbers" in prompt
    assert "grep -rn" in prompt
    assert "agent/agents tools for self-contained subtasks" in prompt

    # No read_file/patch_file: their guidance must not leak in.
    prompt = _cwd_bound_prompt(Path.home(), {"bash", "write_file"})
    assert "read_file shows line numbers" not in prompt
    assert "Prefer patch_file" not in prompt
    assert "grep -rn" in prompt  # bash-only line still present

    # No bash: no search guidance.
    prompt = _cwd_bound_prompt(Path.home(), {"read_file", "patch_file"})
    assert "grep -rn" not in prompt
    assert "Prefer patch_file" in prompt

    # No web_fetch: no untrusted-content line.
    assert "web_fetch returns untrusted" not in prompt

    # Nothing gated present at all: no section.
    prompt = _cwd_bound_prompt(Path.home(), {"custom_tool"})
    assert "# Tool usage" not in prompt


def test_safety_block_gated_on_shell_or_network() -> None:
    prompt = _cwd_bound_prompt(Path.home(), {"bash"})
    assert "# Safety" in prompt
    assert "commit/push unless the user asks" in prompt
    assert "DATA, not" in prompt

    prompt = _cwd_bound_prompt(Path.home(), {"read_file"})
    assert "# Safety" not in prompt


def test_git_env_block_absent_outside_repo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        prompt = _cwd_bound_prompt(td, {"bash"})
    assert "# Environment" not in prompt


def test_git_env_block_in_clean_repo() -> None:
    """#180: branch + status appear, and the block stays stable across
    builds (cache-friendly: it only changes when the repo changes)."""
    import tempfile
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)

        prompt = _cwd_bound_prompt(root, {"bash"})
        assert "# Environment" in prompt
        assert "git branch: main" in prompt
        assert "git status:" in prompt
        assert "(clean)" in prompt

        # Stability: two builds, same repo state → identical block.
        assert prompt == _cwd_bound_prompt(root, {"bash"})


def test_git_env_block_reflects_dirty_state_and_caps() -> None:
    """A dirty repo shows changed files; a long status is capped."""
    import tempfile
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)

        # 40 untracked files: beyond the 30-line status cap.
        for i in range(40):
            (root / f"f{i:02d}.txt").write_text(str(i))

        prompt = _cwd_bound_prompt(root, {"bash"})
        assert "?? f00.txt" in prompt
        # The cap summary is present and the longest filenames are not.
        assert "+10 more" in prompt
        assert "f39.txt" not in prompt


def test_git_env_block_survives_broken_git(tmp_path, monkeypatch) -> None:
    """git missing/failing must degrade to *no* block, never raise."""
    import phoson_cli.session_utils as su

    monkeypatch.setattr(su, "_git_output", lambda args, cwd: None, raising=False)
    prompt = _cwd_bound_prompt(tmp_path, {"bash"})
    assert "# Environment" not in prompt


def test_full_prompt_shape_in_clean_repo() -> None:
    """#180 snapshot (shape): the assembled prompt carries every section, in
    order, for the full tool set in a clean git repo.

    A byte-for-byte snapshot is brittle (cwd, date and git status vary), so
    this pins the *structure*: base line → Tool usage → Environment →
    Safety, each present exactly once and in that relative order.
    """
    import tempfile
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)

        prompt = _cwd_bound_prompt(
            root,
            {
                "bash",
                "read_file",
                "write_file",
                "patch_file",
                "list_dir",
                "agent",
                "agents",
                "web_fetch",
            },
        )

    # Base framing survives.
    assert "You are Phos, a terminal coding agent" in prompt
    assert "Available tools: " in prompt

    # Every ACI section is present exactly once.
    for section in ("# Tool usage", "# Environment", "# Safety"):
        assert prompt.count(section) == 1, section

    # Relative ordering: tool usage before environment before safety.
    assert prompt.index("# Tool usage") < prompt.index("# Environment")
    assert prompt.index("# Environment") < prompt.index("# Safety")

    # Representative content from each.
    assert "Prefer patch_file for targeted edits" in prompt
    assert "git branch: main" in prompt
    assert "commit/push unless the user asks" in prompt
