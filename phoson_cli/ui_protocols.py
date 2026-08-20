"""UI-facing protocols for the session runtime.

The :class:`~phoson_cli.controller.SessionController` is deliberately free
of Rich / prompt_toolkit / Textual dependencies. It talks to whatever
presents the session through the :class:`AgentEventSink` protocol:

- Classic REPL: :class:`phoson_cli.renderer.ClassicSink` (wraps the Rich
  ``Renderer``).
- Textual TUI (MIGRATE_CLI_TO_TEXTUAL.md, phase 3+): a widget-based sink.

Keeping this protocol narrow is the point: anything the controller needs
to *show* goes through it, so a second front end is a sink, not a fork.
"""

from typing import Protocol, runtime_checkable

from phoson_agent import AgentEvent
from phoson_llm.schemas import Message


@runtime_checkable
class AgentEventSink(Protocol):
    """Presentation target for a session run.

    All methods are synchronous: they run on the UI's own event loop
    (the prompt_toolkit loop for the classic sink, the Textual message
    loop for the TUI sink) and must not block.
    """

    def on_user_message(self, text: str, message: Message) -> None:
        """Show the user's turn (media blocks included in ``message``)."""

    def on_attachments(self, sources: list[str]) -> None:
        """Show the pending attachments being flushed into a turn."""

    def on_event(self, event: AgentEvent) -> None:
        """One agent event from the run stream (tokens, tools, ...)."""

    def flush_line(self) -> None:
        """Flush any in-progress spinner/line before printing a message."""

    def capture_partial_reasoning(self) -> None:
        """Snapshot the reasoning seen so far (cancel/error paths)."""

    def take_reasoning(self) -> str:
        """Pop the full reasoning of the last run ("" if none)."""
        ...

    def set_session(self, session_id: str) -> None:
        """Update the displayed session id."""

    def print_history(self, path: list[Message], tail: int) -> None:
        """Replay the tail of a loaded session."""

    def notify(self, kind: str, message: str) -> None:
        """Show a status message. ``kind`` is info, warn or error."""


__all__ = ["AgentEventSink"]
