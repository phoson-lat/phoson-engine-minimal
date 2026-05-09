"""Unit tests for phoson_agent.tool — decorator, schema generation, context injection."""

import enum
import warnings
from typing import Annotated, Literal

import pytest

from phoson_agent.tool import tool, _json_schema_for_type, _context_values
from phoson_agent.models import AgentTool


# ── _json_schema_for_type ────────────────────────────────────────────────────


class TestJsonSchemaForType:
    def test_primitive_types(self):
        assert _json_schema_for_type(str) == ({"type": "string"}, None)
        assert _json_schema_for_type(int) == ({"type": "integer"}, None)
        assert _json_schema_for_type(float) == ({"type": "number"}, None)
        assert _json_schema_for_type(bool) == ({"type": "boolean"}, None)

    def test_list_with_item_type(self):
        schema, desc = _json_schema_for_type(list[str])
        assert schema == {"type": "array", "items": {"type": "string"}}
        assert desc is None

    def test_list_without_item_type(self):
        schema, _ = _json_schema_for_type(list)
        assert schema["type"] == "array"

    def test_dict(self):
        schema, _ = _json_schema_for_type(dict)
        assert schema == {"type": "object"}

    def test_optional_unwraps_to_inner(self):
        schema, _ = _json_schema_for_type(str | None)
        assert schema == {"type": "string"}

    def test_multi_union_produces_anyof(self):
        schema, _ = _json_schema_for_type(int | str | None)
        assert "anyOf" in schema
        types = [s["type"] for s in schema["anyOf"]]
        assert "integer" in types
        assert "string" in types

    def test_literal_produces_enum(self):
        schema, _ = _json_schema_for_type(Literal["a", "b", "c"])
        assert schema == {"type": "string", "enum": ["a", "b", "c"]}

    def test_int_literal(self):
        schema, _ = _json_schema_for_type(Literal[1, 2, 3])
        assert schema == {"type": "integer", "enum": [1, 2, 3]}

    def test_enum_subclass(self):
        class Color(enum.Enum):
            RED = "red"
            BLUE = "blue"

        schema, _ = _json_schema_for_type(Color)
        assert schema["type"] == "string"
        assert set(schema["enum"]) == {"red", "blue"}

    def test_annotated_extracts_description(self):
        schema, desc = _json_schema_for_type(Annotated[str, "a helpful hint"])
        assert schema == {"type": "string"}
        assert desc == "a helpful hint"

    def test_annotated_literal_with_description(self):
        schema, desc = _json_schema_for_type(Annotated[Literal["x", "y"], "pick one"])
        assert schema == {"type": "string", "enum": ["x", "y"]}
        assert desc == "pick one"


# ── @tool decorator ──────────────────────────────────────────────────────────


class TestToolDecorator:
    def test_returns_agent_tool(self):
        @tool
        def my_tool(x: str) -> str:
            """A simple tool."""
            return x

        assert isinstance(my_tool, AgentTool)
        assert my_tool.name == "my_tool"
        assert my_tool.description == "A simple tool."

    def test_schema_includes_required_params(self):
        @tool
        def greet(name: str, greeting: str) -> str:
            """Greet someone."""
            return f"{greeting}, {name}"

        props = (
            my_tool.parameters["properties"]
            if False
            else greet.parameters["properties"]
        )
        assert "name" in props
        assert "greeting" in props
        assert greet.parameters["required"] == ["name", "greeting"]

    def test_schema_optional_param_not_required(self):
        @tool
        def greet(name: str, greeting: str = "Hello") -> str:
            """Greet."""
            return f"{greeting}, {name}"

        assert "name" in greet.parameters["required"]
        assert "greeting" not in greet.parameters.get("required", [])

    def test_inject_excludes_param_from_schema(self):
        @tool(inject=["ctx"])
        def use_ctx(value: str, *, ctx: str) -> str:
            """Uses context."""
            return value + ctx

        assert "value" in use_ctx.parameters["properties"]
        assert "ctx" not in use_ctx.parameters["properties"]

    def test_kw_only_not_injected_emits_warning(self):
        with pytest.warns(UserWarning, match="keyword-only"):

            @tool
            def my_fn(x: str, *, debug: bool = False) -> str:
                """A tool."""
                return x

        assert "x" in my_fn.parameters["properties"]
        assert "debug" not in my_fn.parameters["properties"]

    def test_annotated_description_in_schema(self):
        @tool
        def search(query: Annotated[str, "search terms"]) -> str:
            """Search."""
            return query

        prop = search.parameters["properties"]["query"]
        assert prop["description"] == "search terms"

    def test_literal_type_generates_enum(self):
        @tool
        def sort(order: Literal["asc", "desc"]) -> str:
            """Sort."""
            return order

        prop = sort.parameters["properties"]["order"]
        assert prop["enum"] == ["asc", "desc"]

    def test_sync_handler_calls_function(self):
        @tool
        def add(a: int, b: int) -> str:
            """Add two numbers."""
            return str(a + b)

        import asyncio

        result = add.handler({"a": 2, "b": 3})
        assert result == "5"

    def test_async_handler_calls_coroutine(self):
        @tool
        async def fetch(url: str) -> str:
            """Fetch URL."""
            return f"fetched:{url}"

        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            fetch.handler({"url": "http://example.com"})
        )
        assert result == "fetched:http://example.com"

    def test_decorator_with_parentheses(self):
        @tool(inject=["db"])
        def query(sql: str, *, db: object) -> str:
            """Query."""
            return sql

        assert isinstance(query, AgentTool)


# ── _context_values ──────────────────────────────────────────────────────────


class TestContextValues:
    def test_none_returns_empty(self):
        assert _context_values(None) == {}

    def test_dict_returns_copy(self):
        ctx = {"a": 1, "b": 2}
        result = _context_values(ctx)
        assert result == {"a": 1, "b": 2}
        assert result is not ctx

    def test_object_with_dict(self):
        class Ctx:
            def __init__(self):
                self.x = 10
                self.y = 20

        result = _context_values(Ctx())
        assert result["x"] == 10
        assert result["y"] == 20

    def test_extra_dict_merged(self):
        class Ctx:
            def __init__(self):
                self.name = "test"
                self.extra = {"token": "abc"}

        result = _context_values(Ctx())
        assert result["name"] == "test"
        assert result["token"] == "abc"
        assert "extra" not in result

    def test_inject_in_handler_provides_context_values(self):
        received = {}

        @tool(inject=["user_id"])
        def whoami(*, user_id: str) -> str:
            """Who am I."""
            received["user_id"] = user_id
            return user_id

        class Ctx:
            def __init__(self):
                self.user_id = "alice"

        import asyncio

        class FakeLoop:
            pass

        whoami.handler({}, Ctx())
        assert received["user_id"] == "alice"
