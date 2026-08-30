"""UI-neutral extension contracts for :class:`phoson_agent.Plugin`.

This module is deliberately limited to immutable data objects and protocols.  It
must not import Rich, prompt_toolkit, or :mod:`phoson_cli`: plugins use the
same declarations whether their host is the Phoson CLI, an application embed,
or a non-interactive script.

A host is responsible for validating contributed specs and adapting these
objects to its own widgets.  In particular, a plugin must never return a Rich
renderable or retain a reference to a host UI object.
"""

from typing import Literal, Protocol, runtime_checkable
from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class CliCommandSpec:
    """Metadata for a slash command contributed by a plugin.

    ``handler`` names an async method on the same loaded :class:`Plugin`
    instance.  Keeping a method name rather than a callable makes this object
    declarative, serializable, and independent from a particular CLI host.
    """

    names: tuple[str, ...]
    help: str
    handler: str
    category: str = "Plugins"

    @property
    def primary(self) -> str:
        """Canonical command name (the first item in :attr:`names`)."""
        return self.names[0]


@dataclass(frozen=True)
class CliCommandInvocation:
    """The parsed name and argument text passed to a plugin command handler."""

    name: str
    args: str


@dataclass(frozen=True)
class ToolRenderSpec:
    """Presentation metadata contributed for one plugin-owned agent tool.

    ``detail_handler`` and ``result_handler``, when supplied, name methods on
    the plugin.  Hosts resolve and validate them; they receive only plain tool
    data and return UI-neutral blocks.  No UI toolkit objects cross this API.
    """

    tool_name: str
    verb: str
    icon: str = "⚙"
    detail_handler: str | None = None
    result_handler: str | None = None


@dataclass(frozen=True)
class ThemeExtension:
    """A named CLI theme derived from a built-in base theme.

    A CLI host validates ``tokens`` against its supported core token names.
    ``extra_tokens`` are reserved for presentation authored by this plugin and
    are intentionally never consumed by core renderers.
    """

    name: str
    description: str
    base: Literal["dark", "light", "ansi", "no-color"] = "dark"
    tokens: Mapping[str, str] = field(default_factory=dict)
    extra_tokens: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NoticeBlock:
    """A short status notice emitted by a plugin."""

    id: str
    kind: Literal["info", "warn", "error"]
    message: str


@dataclass(frozen=True)
class KeyValueBlock:
    """A small labelled data card emitted by a plugin."""

    id: str
    title: str
    items: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TodoItem:
    """One item in a :class:`TodoListBlock`.

    TODO items are display/update-only in the first public API.  Interactive
    actions are intentionally deferred until they have a host-independent
    permission and dispatch model.
    """

    id: str
    title: str
    completed: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class TodoListBlock:
    """A plugin-managed TODO list."""

    id: str
    title: str
    items: tuple[TodoItem, ...]


@dataclass(frozen=True)
class ProgressBlock:
    """A progress/status card that a plugin may replace in place."""

    id: str
    label: str
    completed: int | None = None
    total: int | None = None
    detail: str | None = None


type UiBlock = NoticeBlock | KeyValueBlock | TodoListBlock | ProgressBlock


@dataclass(frozen=True)
class Choice:
    """One option presented by :meth:`PluginUiService.select`."""

    id: str
    label: str
    detail: str | None = None


@dataclass(frozen=True)
class FormField:
    """A host-neutral form field requested by a plugin."""

    id: str
    label: str
    kind: Literal["text", "password", "integer"] = "text"
    required: bool = True
    default: str | None = None
    help: str | None = None


@dataclass(frozen=True)
class InteractionResult:
    """Result of a plugin interaction, including safe non-UI degradation."""

    status: Literal["submitted", "cancelled", "unavailable"]
    values: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class PluginUiService(Protocol):
    """A host's UI port exposed to tools and plugin commands.

    ``publish``/``replace``/``remove`` are synchronous by design: they queue
    UI state on the host loop and must not perform blocking work.  Interactive
    methods are async because the user may respond later.  A non-interactive
    host returns ``InteractionResult(status="unavailable")`` and never reads
    stdin implicitly.
    """

    def publish(self, block: UiBlock) -> None: ...

    def replace(self, block_id: str, block: UiBlock) -> None: ...

    def remove(self, block_id: str) -> None: ...

    def set_status(self, key: str, label: str | None) -> None: ...

    async def confirm(
        self, *, title: str, message: str, danger: str | None = None
    ) -> InteractionResult: ...

    async def select(
        self, *, title: str, message: str, choices: Sequence[Choice]
    ) -> InteractionResult: ...

    async def form(
        self, *, title: str, fields: Sequence[FormField]
    ) -> InteractionResult: ...


@runtime_checkable
class CliCommandContext(Protocol):
    """Small stable context passed to a plugin slash-command handler.

    Hosts deliberately do not expose their renderer, widget tree,
    ``SessionController`` or mutable configuration through this protocol.
    """

    @property
    def plugin_name(self) -> str: ...

    @property
    def cwd(self) -> Path: ...

    @property
    def session_id(self) -> str: ...

    @property
    def ui(self) -> PluginUiService: ...

    def notify(self, kind: Literal["info", "warn", "error"], message: str) -> None: ...


__all__ = [
    "Choice",
    "CliCommandContext",
    "CliCommandInvocation",
    "CliCommandSpec",
    "FormField",
    "InteractionResult",
    "KeyValueBlock",
    "NoticeBlock",
    "PluginUiService",
    "ProgressBlock",
    "ThemeExtension",
    "TodoItem",
    "TodoListBlock",
    "ToolRenderSpec",
    "UiBlock",
]
