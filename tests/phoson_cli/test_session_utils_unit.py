"""Unit tests for ``phoson_cli.session_utils.build_system_prompt`` (B1).

B1: the clock must use the *system* timezone, not a hardcoded zone.
"""

import time
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from phoson_cli.session_utils import build_system_prompt


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


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
