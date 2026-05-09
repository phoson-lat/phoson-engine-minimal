"""
Module for managing agent context.
"""

from typing import Any
from dataclasses import field, dataclass


@dataclass
class AgentContext:
    """Context container for agent execution.

    The context is intentionally untyped (``dict[str, Any]``) so callers can
    inject arbitrary state into tools (sessions, DB connections, user IDs,
    feature flags, ...). For type safety in domain code, prefer subclassing
    AgentContext with explicit fields, or wrap typed objects inside
    ``extra``::

        @dataclass
        class MyContext(AgentContext):
            user_id: str = ""
            db: Database | None = None

    Tools declared with ``@tool(inject=[...])`` will receive matching keys
    from ``extra`` and from public attributes on the context object.
    """

    extra: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """Gets a value from the context or returns the default value."""
        return self.extra.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Sets a value in the context.

        Convenience method so callers do not have to reach into ``extra``
        directly.
        """
        self.extra[key] = value

    def __getitem__(self, key: str) -> Any:
        """Gets a value from the context using indexing."""
        return self.extra[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """Sets a value in the context using indexing."""
        self.extra[key] = value

    def __contains__(self, key: str) -> bool:
        """Checks if a key exists in the context."""
        return key in self.extra
