"""
Módulo para gestionar el contexto del agente.
"""
from typing import Any
from dataclasses import field, dataclass


@dataclass
class AgentContext:
    """
    Contenedor de contexto para la ejecución del agente.
    """
    extra: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """Obtiene un valor del contexto o retorna el valor por defecto."""
        return self.extra.get(key, default)

    def __getitem__(self, key: str) -> Any:
        """Obtiene un valor del contexto mediante indexación."""
        return self.extra[key]

    def __contains__(self, key: str) -> bool:
        """Verifica si una clave existe en el contexto."""
        return key in self.extra
