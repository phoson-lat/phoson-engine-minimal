"""Shared infrastructure for full-screen TUI pickers.

The CLI ships three pickers (``model_picker``, ``provider_picker``,
``session_picker``) which historically copy-pasted the same scaffolding:
a ``prompt_toolkit`` ``Application`` with a single ``Window``, a renderer
callback, and a fixed set of key bindings. This module factors that
scaffolding out so each picker only owns:

  * a renderer ``(state) -> formatted_text``,
  * an initial state object,
  * a small set of state transitions triggered by keypresses.

Concrete pickers compose these with :class:`BasePicker`.

The default key set covers ``up``/``down`` for navigation, ``enter`` to
confirm and ``escape`` to cancel. Pickers that need pagination, search,
or extra actions register additional handlers via
``BasePicker.bind(key, handler)``.
"""

from collections.abc import Callable

from prompt_toolkit.styles import Style
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import HSplit, Window

# ─── Shared style ────────────────────────────────────────────────────────────

# All pickers share the same purple-on-dark palette used elsewhere by the
# renderer. Pickers that need extra style classes can extend this dict
# via ``Style.from_dict({**BASE_PICKER_STYLE_DICT, ...})``.
BASE_PICKER_STYLE_DICT: dict[str, str] = {
    "title": "bold #b57bee",
    "header": "#808080",
    "row.selected": "bg:#3d2b6e bold #ffffff",
    "row": "#9a8faa",
    "row.active": "bold #00ff9c",
    "footer": "#5a5a5a",
    "key-hint": "bold #b57bee",
    "search": "bold #e0d0ff",
    "search.label": "#b57bee bold",
    "search.hint": "#6f6780",
    "empty": "#ff9aa2",
}


def picker_style(extra: dict[str, str] | None = None) -> Style:
    """Build a ``Style`` from the shared palette plus optional overrides."""
    if not extra:
        return Style.from_dict(BASE_PICKER_STYLE_DICT)
    return Style.from_dict({**BASE_PICKER_STYLE_DICT, **extra})


# ─── BasePicker ──────────────────────────────────────────────────────────────


HandlerFn = Callable[[], None]


class BasePicker[TResult]:
    """Generic full-screen TUI picker.

    Subclasses (or ad-hoc builders) provide:

      * ``render`` — a no-arg callable returning prompt_toolkit
        formatted-text tuples for the entire frame.
      * ``initial`` — a default :class:`TResult` to return if the picker
        runs against an empty input set (the picker exits immediately
        with this value).

    Key bindings are registered with :meth:`bind`. Each binding receives
    a ``HandlerFn`` (no args) — the handler typically mutates closure
    state captured by the renderer and then calls :meth:`refresh`.

    To finish the picker, handlers call :meth:`done` with the result.

    Args:
        render: Callable returning formatted-text tuples for each frame.
        style: Optional ``Style`` instance; defaults to
            :func:`picker_style`.
        initial: Default result when the input list is empty.
    """

    def __init__(
        self,
        *,
        render: Callable[[], list[tuple[str, str]]],
        style: Style | None = None,
        initial: TResult | None = None,
    ) -> None:
        self._render = render
        self._style = style or picker_style()
        self._initial = initial

        self._kb = KeyBindings()
        self._window = Window(
            content=FormattedTextControl(render),
            always_hide_cursor=True,
        )
        self._app: Application | None = None

    # ── Binding helpers ─────────────────────────────────────────────────

    def bind(self, key: str, handler: HandlerFn) -> None:
        """Bind ``key`` to a no-arg handler."""

        @self._kb.add(key)
        def _(_event: object) -> None:  # noqa: ARG001
            handler()

    def bind_typing(self, handler: Callable[[str], None]) -> None:
        """Bind any printable keystroke to ``handler(typed_string)``.

        The ``model_picker`` uses this for fuzzy search input.
        """

        @self._kb.add("<any>")
        def _(event: object) -> None:
            data = getattr(event, "data", "") or ""
            if data and data.isprintable() and data not in {"\r", "\n"}:
                handler(data)

    def refresh(self) -> None:
        """Force the window to re-render on the next tick."""
        if self._app is not None:
            self._app.invalidate()

    def done(self, result: TResult) -> None:
        """Exit the picker with ``result``."""
        if self._app is not None:
            self._app.exit(result=result)

    # ── Convenience: register the standard navigation set ───────────────

    def bind_default_nav(
        self,
        *,
        on_up: HandlerFn,
        on_down: HandlerFn,
        on_enter: HandlerFn,
        on_cancel: HandlerFn,
    ) -> None:
        """Register the four near-universal bindings in one call."""
        self.bind("up", on_up)
        self.bind("down", on_down)
        self.bind("enter", on_enter)
        self.bind("escape", on_cancel)

    # ── Run ─────────────────────────────────────────────────────────────

    async def run(self) -> TResult:
        """Start the picker and return its result."""
        if self._initial is not None and self._is_empty():  # pragma: no cover
            return self._initial

        self._app = Application(
            layout=Layout(HSplit([self._window])),
            key_bindings=self._kb,
            full_screen=True,
            style=self._style,
            mouse_support=False,
        )
        return await self._app.run_async()

    def _is_empty(self) -> bool:
        """Subclasses can override to short-circuit run() on empty input."""
        return False
