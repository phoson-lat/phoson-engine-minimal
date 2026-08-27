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
