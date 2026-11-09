"""Serialization of engine objects to JSON.

The engine uses dataclasses (``AgentEvent``, ``Message``, content blocks,
``RunStep``…), enums, ``Path`` and ``datetime``. This module normalizes them to
plain JSON types. Convention: every dataclass carries a ``type`` field with its
class name (e.g. ``{"type": "AgentTokenEvent", ...}``), which the frontend uses
to discriminate.
"""

import enum
import json
import dataclasses
from typing import Any
from pathlib import Path
from datetime import date, datetime

__all__ = ["to_jsonable", "dump"]


def to_jsonable(obj: Any) -> Any:
    """Recursively convert *obj* into something json.dumps can serialize."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {"type": type(obj).__name__}
        for field in dataclasses.fields(obj):
            out[field.name] = to_jsonable(getattr(obj, field.name, None))
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    # Opaque objects (trackers, clients): the frontend must not depend on them.
    return {"type": type(obj).__name__, "repr": repr(obj)}


def dump(message: dict[str, Any]) -> str:
    """Compact single-line JSON (NDJSON framing)."""
    return json.dumps(message, ensure_ascii=False, default=str, separators=(",", ":"))
