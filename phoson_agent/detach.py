"""Sending a running tool call to the background (host-driven).

A host that wants "move this to the background" (e.g. a Ctrl+B key) puts a
:class:`ToolDetacher` in ``AgentContext.extra[DETACHER_KEY]``. For tools
that opt in with ``metadata[DETACHABLE_KEY] = True`` (``@tool(detachable=
True)``), the tool runner then runs the handler in its own asyncio task and
waits for *either* the handler to finish *or* the detacher to resolve the
future returned by :meth:`ToolDetacher.track`. In the second case the
future's text becomes the tool result the model sees right away, and the
handler task keeps running — the host owns it from then on.

Inside a detachable handler, :func:`detach_requested` returns an
:class:`asyncio.Event` that is set when the call is detached, so a handler
with its own short timeout (``bash``) can switch to a longer one instead of
killing work the user chose to keep.

The engine itself never detaches anything; without a detacher in the
context, detachable tools run exactly like any other tool.
"""

import asyncio
import contextvars
from typing import Any, Protocol

#: ``AgentTool.metadata`` key: the tool may be sent to the background.
DETACHABLE_KEY = "detachable"
#: ``AgentContext.extra`` key under which a host installs its detacher.
DETACHER_KEY = "tool_detacher"

_DETACH_EVENT: contextvars.ContextVar[asyncio.Event | None] = contextvars.ContextVar(
    "phoson_detach_event", default=None
)


class ToolDetacher(Protocol):
    """Host side of detaching: decides when and owns what was detached."""

    def track(self, call: Any, task: asyncio.Task) -> "asyncio.Future[str]":
        """Register a running call; resolve the future to detach it."""
        ...

    def untrack(self, call: Any) -> None:
        """The call finished (or was cancelled) in the foreground."""
        ...


def detach_requested() -> asyncio.Event | None:
    """Event set when the *current* tool call is sent to the background.

    ``None`` outside a detachable call run by a host that supports it.
    """
    return _DETACH_EVENT.get()


def set_detach_event(event: asyncio.Event | None) -> contextvars.Token:
    return _DETACH_EVENT.set(event)


__all__ = [
    "DETACHABLE_KEY",
    "DETACHER_KEY",
    "ToolDetacher",
    "detach_requested",
    "set_detach_event",
]
