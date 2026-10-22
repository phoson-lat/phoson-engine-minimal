"""UI-facing protocols for the session runtime.

The :class:`~phoson_cli.controller.SessionController` is deliberately free
of any specific Rich/prompt_toolkit presentation dependency. It talks to
whatever presents the session through the :class:`AgentEventSink`
protocol — today :class:`phoson_cli.renderer.ClassicSink` (wraps the Rich
``Renderer``), soon a full-screen ``prompt_toolkit`` app's own sink.

Keeping this protocol narrow is the point: anything the controller needs
to *show* goes through it, so a new front end is a sink, not a fork.
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from collections.abc import Callable, Coroutine

from phoson_agent import AgentEvent
from phoson_llm.schemas import Message

if TYPE_CHECKING:
    from phoson_agent import AgentEngine
    from phoson_agent.sessions import JsonlStorage, ConversationTree

    from .theme import Theme, ThemeRegistry
    from .config import PhosonConfig
    from ._session import SessionMetrics
    from .plugin_ui import SinkPluginUiService
    from .controller import CompactPlan
    from .attachments import AttachmentManager
    from .command_catalog import CommandCatalog


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

    def print_history(
        self,
        path: list[Message],
        tail: int | None = None,
        timestamps: "list | None" = None,
    ) -> None:
        """Replay the tail of a loaded session.

        ``timestamps`` (#212) optionally carries each message's local time so
        the replay shows a date/time per message.
        """

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
class TransactionalSessionViewSink(Protocol):
    """Optional capability for front ends whose session view is replaceable."""

    def snapshot_session_view(self) -> object:
        """Capture view state that must survive a failed session switch."""

    def reset_session_view(self) -> None:
        """Clear the visible state before rendering another session."""

    def restore_session_view(self, snapshot: object) -> None:
        """Restore a snapshot after a failed session switch."""


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

    async def confirm_bash_command(
        self,
        command: str,
        *,
        on_always: "Callable[[str], Coroutine[Any, Any, None]] | None" = None,
    ) -> bool:
        """Permission-mode confirmation card (T-6): the command in
        monospace with **Yes / Always / No** actions.

        ``on_always`` is invoked (with the command) when the user picks
        *Always* — the front end persists the allow pattern. Front ends
        without the richer card may not implement this; callers fall
        back to :meth:`confirm_bash`. False on cancel/No.
        """
        ...


class SessionPort(Protocol):
    """The session surface slash commands are written against.

    :class:`~phoson_cli.commands.CommandHandler` and
    :class:`~phoson_cli.commands.PluginCommandContext` depend on this
    protocol only — never on a concrete front end or on controller
    internals. :class:`~phoson_cli.session_frontend.SessionFrontend`
    implements it for both interactive UIs; tests may pass any object with
    the members a command touches.
    """

    #: The active theme (a plain attribute on the front end).
    theme: "Theme"

    @property
    def config(self) -> "PhosonConfig": ...

    @property
    def tree(self) -> "ConversationTree": ...

    @property
    def current_node_id(self) -> str | None: ...

    @property
    def current_model(self) -> str: ...

    @property
    def subagent_model(self) -> str: ...

    @subagent_model.setter
    def subagent_model(self, value: str) -> None: ...

    @property
    def storage(self) -> "JsonlStorage": ...

    @property
    def engine(self) -> "AgentEngine": ...

    @property
    def session_metrics(self) -> "SessionMetrics": ...

    @property
    def attachments(self) -> "AttachmentManager": ...

    @property
    def theme_registry(self) -> "ThemeRegistry": ...

    @property
    def command_catalog(self) -> "CommandCatalog": ...

    @property
    def plugin_ui(self) -> "SinkPluginUiService": ...

    @property
    def sink(self) -> AgentEventSink: ...

    @property
    def context_window(self) -> int: ...

    @property
    def context_tokens(self) -> int: ...

    @property
    def is_running(self) -> bool: ...

    def new_session(self) -> None: ...

    async def load_session(self, session_id: str) -> bool: ...

    async def compact_context(
        self, profile: str | None = None
    ) -> tuple[int, int, bool]: ...

    def plan_compaction(self, profile: str | None = None) -> "CompactPlan": ...

    def set_compact_mode(self, mode: str) -> bool: ...

    async def set_provider(self, provider: str) -> None: ...

    async def set_model(
        self, model: str, provider: str | None = None, *, reuse_engine: bool = False
    ) -> None: ...

    def refresh_permission_policy(self) -> None: ...

    def label_current_node(self, text: str) -> None: ...

    def note_user_title(self) -> None: ...

    def undo_last_turn(self) -> tuple[bool, str]: ...


__all__ = [
    "AgentEventSink",
    "ConfirmationService",
    "SessionPort",
    "TransactionalSessionViewSink",
]
