import inspect
import functools
from typing import Any, Annotated, get_args, get_origin, get_type_hints

from phoson_agent.models import AgentTool

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _context_values(context: Any | None) -> dict[str, Any]:
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


def _get_json_type_and_description(python_type: Any) -> tuple[str, str | None]:
    description = None

    if get_origin(python_type) is Annotated:
        args = get_args(python_type)
        python_type = args[0]
        description = next((a for a in args[1:] if isinstance(a, str)), None)

    json_type = _TYPE_MAP.get(python_type, "string")
    return json_type, description


def _build_parameters(fn: Any, exclude: set[str]) -> dict[str, Any]:
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
        json_type, description = _get_json_type_and_description(python_type)

        prop: dict[str, Any] = {"type": json_type}
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


def tool(_fn: Any = None, *, inject: list[str] | None = None) -> Any:
    def decorator(fn: Any) -> AgentTool:
        injected = set(inject or [])

        sig = inspect.signature(fn)
        kw_only = {
            name
            for name, param in sig.parameters.items()
            if param.kind == inspect.Parameter.KEYWORD_ONLY
        }
        excluded = injected | kw_only

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
