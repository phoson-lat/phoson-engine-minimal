"""Agent execution context.

The :class:`AgentContext` is a small dependency-injection bag passed to
every tool handler. It deliberately straddles two needs:

  * Quick prototyping with arbitrary key/value state via ``extra``.
  * First-class type safety for production code via subclassing with
    explicit dataclass fields.

For the static-typing-friendly path, see :class:`AgentContextProtocol`
and the example subclass in the module docstring of
:class:`AgentContext`.

Tools declared with ``@tool(inject=[...])`` receive matching values
from both ``extra`` and from public attributes on the context object.
The lookup order is: public attributes first, then ``extra`` overrides.
"""

from typing import Any, Protocol, runtime_checkable
from dataclasses import field, dataclass


@runtime_checkable
class AgentContextProtocol(Protocol):
    """Structural type that any agent context must satisfy.

    Implementations need to expose ``extra`` (for ad-hoc state) plus
    ``get``/``set`` for ergonomic lookups. Public attributes on the
    object itself are also visible to ``@tool(inject=[...])``.
    """

    extra: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


@dataclass
class AgentContext:
    """Default context container for agent execution.

    The ``extra`` dict is intentionally untyped (``dict[str, Any]``) so
    callers can inject arbitrary state into tools (sessions, DB
    connections, user IDs, feature flags, ...). For type safety in
    domain code, subclass :class:`AgentContext` with explicit fields
    or wrap typed objects inside ``extra``.

    Example — typed subclass::

        @dataclass
        class MyContext(AgentContext):
            user_id: str = ""
            db: Database | None = None

        ctx = MyContext(user_id="abel", db=db)
        engine = AgentEngine(chat=chat, tools=tools, context=ctx)

    Tools declared with ``@tool(inject=["user_id", "db"])`` receive
    those attributes as keyword arguments. Public attributes on the
    context object take precedence over keys in ``extra`` of the
    same name.
    """

    extra: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from ``extra``, returning ``default`` if absent."""
        return self.extra.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a key in ``extra``.

        Convenience method so callers do not have to reach into ``extra``
        directly.
        """
        self.extra[key] = value

    def __getitem__(self, key: str) -> Any:
        """Get a value from ``extra`` using indexing."""
        return self.extra[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """Set a value in ``extra`` using indexing."""
        self.extra[key] = value

    def __contains__(self, key: str) -> bool:
        """Check if a key exists in ``extra``."""
        return key in self.extra
