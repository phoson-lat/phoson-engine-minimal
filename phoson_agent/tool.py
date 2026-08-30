"""
Module for the definition of agent tools.
"""

import enum
import typing
import inspect
import warnings
import functools
from types import UnionType
from typing import (
    Any,
    Literal,
    Annotated,
    get_args,
    overload,
    get_origin,
    get_type_hints,
)
from collections.abc import Callable

from phoson_agent.models import AgentTool

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _context_values(context: object | None) -> dict[str, Any]:
    """Extracts relevant values from a context object."""
    if context is None:
        return {}

    if isinstance(context, dict):
        return dict(context)

    values: dict[str, Any] = {}
    if hasattr(context, "__dict__"):
        values.update(vars(context))

    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        values.update(extra)

    values.pop("extra", None)
    return values


def _json_schema_for_type(python_type: Any) -> tuple[dict[str, Any], str | None]:
    """Generates a JSON schema from a Python type."""
    description = None

    if get_origin(python_type) is Annotated:
        args = get_args(python_type)
        python_type = args[0]
        description = next((a for a in args[1:] if isinstance(a, str)), None)

    origin = get_origin(python_type)

    if origin in (list, tuple):
        item_type = get_args(python_type)
        items_schema: dict[str, Any] = {"type": "string"}
        if item_type:
            items_schema, _ = _json_schema_for_type(item_type[0])
        return {"type": "array", "items": items_schema}, description

    if origin is dict:
        return {"type": "object"}, description

    if origin in (UnionType, typing.Union):
        args = [arg for arg in get_args(python_type) if arg is not type(None)]
        if len(args) == 1:
            # Optional[X] (Annotated[X | None, "desc"]): keep the Annotated
            # description when recursing into the single non-None variant.
            schema, _ = _json_schema_for_type(args[0])
            return schema, description
        # Multi-arg union: use anyOf with each variant's schema
        schemas = [_json_schema_for_type(a)[0] for a in args]
        return {"anyOf": schemas}, description

    # Literal[v1, v2, ...] → enum constraint
    if origin is Literal:
        values = list(get_args(python_type))
        # Derive JSON type from the first literal value
        first = values[0]
        json_type = _TYPE_MAP.get(type(first), "string")
        return {"type": json_type, "enum": values}, description

    # Enum subclasses → enum constraint
    if isinstance(python_type, type) and issubclass(python_type, enum.Enum):
        values = [m.value for m in python_type]
        json_type = _TYPE_MAP.get(type(values[0]), "string") if values else "string"
        return {"type": json_type, "enum": values}, description

    json_type = _TYPE_MAP.get(python_type, "string")
    return {"type": json_type}, description


def _build_parameters(fn: Callable[..., Any], exclude: set[str]) -> dict[str, Any]:
    """Builds the JSON parameter schema for a function."""
    hints = get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in exclude:
            continue

        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            continue

        python_type = hints.get(name, str)
        prop, description = _json_schema_for_type(python_type)
        if description:
            prop["description"] = description
        properties[name] = prop

        if param.default is inspect.Parameter.empty:
            required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


@overload
def tool(_fn: Callable[..., Any]) -> AgentTool: ...
@overload
def tool(
    *,
    inject: list[str] | None = None,
) -> Callable[[Callable[..., Any]], AgentTool]: ...
def tool(
    _fn: Callable[..., Any] | None = None,
    *,
    inject: list[str] | None = None,
) -> AgentTool | Callable[[Callable[..., Any]], AgentTool]:
    """
    Decorator to register a function as an agent tool.
    """

    def decorator(fn: Callable[..., Any]) -> AgentTool:
        injected = set(inject or [])

        sig = inspect.signature(fn)
        kw_only = {
            name
            for name, param in sig.parameters.items()
            if param.kind == inspect.Parameter.KEYWORD_ONLY
        }
        excluded = injected | kw_only

        # Keyword-only params not listed in inject= are excluded from the
        # tool schema and won't be forwarded. Warn so users aren't surprised.
        uninjected_kw = kw_only - injected
        if uninjected_kw:
            warnings.warn(
                f"@tool: {fn.__name__!r} has keyword-only parameter(s) "
                f"{sorted(uninjected_kw)} that are not listed in inject= and "
                "will be excluded from the tool schema.",
                UserWarning,
                stacklevel=3,
            )

        parameters = _build_parameters(fn, excluded)
        description = (fn.__doc__ or "").strip()

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_handler(
                args: dict[str, Any],
                context: Any | None = None,
            ) -> Any:
                ctx_data = _context_values(context)
                kwargs = {k: v for k, v in ctx_data.items() if k in excluded}
                return await fn(**args, **kwargs)

            final_handler = async_handler
        else:

            @functools.wraps(fn)
            def sync_handler(
                args: dict[str, Any],
                context: Any | None = None,
            ) -> Any:
                ctx_data = _context_values(context)
                kwargs = {k: v for k, v in ctx_data.items() if k in excluded}
                return fn(**args, **kwargs)

            final_handler = sync_handler

        return AgentTool(
            name=fn.__name__,
            description=description,
            parameters=parameters,
            handler=final_handler,
        )

    if _fn is not None:
        return decorator(_fn)
    return decorator
