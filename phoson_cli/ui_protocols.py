"""UI-facing protocols for the session runtime.

The :class:`~phoson_cli.controller.SessionController` is deliberately free
of any specific Rich/prompt_toolkit presentation dependency. It talks to
whatever presents the session through the :class:`AgentEventSink`
protocol — today :class:`phoson_cli.renderer.ClassicSink` (wraps the Rich
``Renderer``), soon a full-screen ``prompt_toolkit`` app's own sink.

Keeping this protocol narrow is the point: anything the controller needs
to *show* goes through it, so a new front end is a sink, not a fork.
"""

from typing import Protocol, runtime_checkable

from phoson_agent import AgentEvent
from phoson_llm.schemas import Message


@runtime_checkable
class AgentEventSink(Protocol):
    """Presentation target for a session run.

    All methods are synchronous: they run on the UI's own event loop
    and must not block.
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

    def print_history(self, path: list[Message], tail: int | None = None) -> None:
        """Replay the tail of a loaded session."""

    def notify(self, kind: str, message: str) -> None:
        """Show a status message. ``kind`` is info, warn or error."""

    def on_subagent_progress(self, progress: object | None) -> None:
        """Live sub-agent metrics for the active sub-agent call (E2).

        ``progress`` is the per-call tracker the sub-agent tool created
        (a ``SubagentProgressTracker`` whose ``tasks`` are the live
        per-task metrics) — ``None`` when that call finishes. Front
        ends with a live sub-agent panel render it in real time; front
        ends without one may ignore it.
        """


@runtime_checkable
class ConfirmationService(Protocol):
    """Human-in-the-loop confirmations.

    Tools that need an interactive yes/no (bash in ``safe_mode``) receive
    a service through engine context injection instead of opening a
    prompt themselves: the classic REPL injects a prompt_toolkit-based
    implementation; a full-screen front end can inject a modal; front
    ends that cannot confirm (one-shot / scripts) inject nothing and the
    tool must fail closed.
    """

    async def confirm_bash(self, command: str) -> bool:
        """Ask whether ``command`` may run. False on cancel/EOF."""
        ...


__all__ = ["AgentEventSink", "ConfirmationService"]
