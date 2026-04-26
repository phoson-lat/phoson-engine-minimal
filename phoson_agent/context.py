from typing import Any
from dataclasses import field, dataclass


@dataclass
class AgentContext:
    extra: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.extra[key]

    def __contains__(self, key: str) -> bool:
        return key in self.extra
