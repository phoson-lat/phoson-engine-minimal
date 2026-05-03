import pytest

from phoson_agent.context import AgentContext


def test_agent_context_default_empty() -> None:
    ctx = AgentContext()
    assert ctx.extra == {}


def test_agent_context_with_extra() -> None:
    ctx = AgentContext(extra={"key": "value"})
    assert ctx.extra == {"key": "value"}


def test_agent_context_get_returns_default() -> None:
    ctx = AgentContext()
    assert ctx.get("missing") is None
    assert ctx.get("missing", "default") == "default"


def test_agent_context_get_returns_value() -> None:
    ctx = AgentContext(extra={"key": "value"})
    assert ctx.get("key") == "value"


def test_agent_context_indexing() -> None:
    ctx = AgentContext(extra={"foo": "bar"})
    assert ctx["foo"] == "bar"


def test_agent_context_indexing_missing_raises() -> None:
    ctx = AgentContext()
    with pytest.raises(KeyError):
        _ = ctx["missing"]


def test_agent_context_contains() -> None:
    ctx = AgentContext(extra={"present": True})
    assert "present" in ctx
    assert "missing" not in ctx


def test_agent_context_can_be_modified() -> None:
    ctx = AgentContext()
    ctx.extra["new_key"] = "new_value"
    assert ctx["new_key"] == "new_value"
