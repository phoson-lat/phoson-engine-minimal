"""
Module for managing agent context.
"""

from typing import Any
from dataclasses import field, dataclass


@dataclass
class AgentContext:
    """
    Context container for agent execution.
    """

    extra: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """Gets a value from the context or returns the default value."""
        return self.extra.get(key, default)

    def __getitem__(self, key: str) -> Any:
        """Gets a value from the context using indexing."""
        return self.extra[key]

    def __contains__(self, key: str) -> bool:
        """Checks if a key exists in the context."""
        return key in self.extra
