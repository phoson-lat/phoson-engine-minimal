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


# ─── AgentContextProtocol ────────────────────────────────────────────────────


def test_agent_context_satisfies_the_protocol() -> None:
    """Default ``AgentContext`` is structurally compatible with the protocol."""
    from phoson_agent.context import AgentContextProtocol

    ctx = AgentContext()
    assert isinstance(ctx, AgentContextProtocol)


def test_typed_subclass_works_with_inject() -> None:
    """A ``@tool(inject=[...])`` reads typed fields from a subclass.

    This is the pattern documented in :class:`AgentContext`. The runner
    must pull ``user_id`` from the public attribute and ``request_id``
    from ``extra``, demonstrating that both lookup paths coexist.
    """
    from dataclasses import dataclass

    from phoson_agent.tool import tool

    @dataclass
    class MyContext(AgentContext):
        user_id: str = ""

    @tool(inject=["user_id", "request_id"])
    def whoami(*, user_id: str, request_id: str) -> str:
        return f"{user_id}/{request_id}"

    ctx = MyContext(user_id="abel", extra={"request_id": "req-1"})
    result = whoami.handler({}, ctx)
    assert result == "abel/req-1"


def test_typed_subclass_satisfies_the_protocol() -> None:
    from dataclasses import dataclass

    from phoson_agent.context import AgentContextProtocol

    @dataclass
    class MyContext(AgentContext):
        user_id: str = ""

    ctx = MyContext(user_id="abel")
    assert isinstance(ctx, AgentContextProtocol)


def test_set_helper_writes_to_extra() -> None:
    ctx = AgentContext()
    ctx.set("k", 1)
    assert ctx.get("k") == 1
    assert ctx["k"] == 1
