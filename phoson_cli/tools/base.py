"""Abstract base class for all Phoson CLI tools."""

from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """
    Interface for all tools in the phoson_cli.

    All tools must implement the `run` method and provide a metadata/schema
    definition if required by the agent engine.
    """

    @abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the tool."""
        pass
