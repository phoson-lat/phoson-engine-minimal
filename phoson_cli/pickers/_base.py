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
from prompt_toolkit.widgets import Frame
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import Float, HSplit, Window

from phoson_cli.theme import Theme, load_theme, build_picker_style_dict

# ─── Shared style ────────────────────────────────────────────────────────────


def picker_style(
    extra: dict[str, str] | None = None, theme: Theme | None = None
) -> Style:
    """Build a ``Style`` from the active theme's picker palette.

    Args:
        extra: Optional style overrides layered on top of the base dict.
        theme: The active theme; resolved via ``load_theme()`` when None.
    """
    base = build_picker_style_dict(theme or load_theme())
    if not extra:
        return Style.from_dict(base)
    return Style.from_dict({**base, **extra})


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
        on_done: When set, :meth:`done` reports through this callback
            instead of exiting an owned ``Application`` — used when the
            picker is hosted as a Float inside another running
            Application (see :meth:`as_float`) rather than via
            :meth:`run`.
        invalidate: Paired with ``on_done`` — :meth:`refresh` calls this
            instead of the (nonexistent, in Float mode) owned
            ``Application``.
    """

    def __init__(
        self,
        *,
        render: Callable[[], list[tuple[str, str]]],
        style: Style | None = None,
        initial: TResult | None = None,
        on_done: Callable[[TResult], None] | None = None,
        invalidate: Callable[[], None] | None = None,
    ) -> None:
        self._render = render
        self._style = style or picker_style()
        self._initial = initial
        self._on_done = on_done
        self._invalidate = invalidate

        self._kb = KeyBindings()
        self._window = Window(
            # Focusable so a Float host can move focus onto it — otherwise
            # the host's own focused input keeps first claim on keystrokes
            # (the focused control's key bindings take priority over the
            # Application-level ones the picker's kb is merged into).
            content=FormattedTextControl(render, focusable=True),
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
        if self._invalidate is not None:
            self._invalidate()
        elif self._app is not None:
            self._app.invalidate()

    def done(self, result: TResult) -> None:
        """Report ``result``.

        Exits an owned ``Application`` normally, or calls ``on_done``
        when hosted as a Float (see :meth:`as_float`).
        """
        if self._on_done is not None:
            self._on_done(result)
        elif self._app is not None:
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

    def bind_list_nav(
        self,
        *,
        get_len: Callable[[], int],
        get_sel: Callable[[], int],
        set_sel: Callable[[int], None],
        on_enter: HandlerFn,
        on_cancel: HandlerFn,
    ) -> None:
        """Register up/down/enter/escape for a simple linear list.

        Eliminates the boilerplate go_up/go_down pair common to all pickers.
        """

        def go_up() -> None:
            if get_sel() > 0:
                set_sel(get_sel() - 1)
                self.refresh()

        def go_down() -> None:
            if get_sel() < get_len() - 1:
                set_sel(get_sel() + 1)
                self.refresh()

        self.bind_default_nav(
            on_up=go_up, on_down=go_down, on_enter=on_enter, on_cancel=on_cancel
        )

    def bind_paged_nav(
        self,
        *,
        get_len: Callable[[], int],
        get_sel: Callable[[], int],
        set_sel: Callable[[int], None],
        get_page: Callable[[], int],
        set_page: Callable[[int], None],
        page_size: int,
        on_enter: HandlerFn,
        on_cancel: HandlerFn,
        bind_page_keys: bool = True,
    ) -> None:
        """Register up/down/pageup/pagedown/enter/escape for a paged list.

        Eliminates the four-function navigation block duplicated across pickers
        that support pagination. Set ``bind_page_keys=False`` to skip the
        ``pageup``/``pagedown`` bindings (e.g. when registering them manually).
        """

        def go_up() -> None:
            if get_sel() > 0:
                set_sel(get_sel() - 1)
                set_page(get_sel() // page_size)
                self.refresh()

        def go_down() -> None:
            if get_sel() < get_len() - 1:
                set_sel(get_sel() + 1)
                set_page(get_sel() // page_size)
                self.refresh()

        def page_up() -> None:
            if get_page() > 0:
                set_page(get_page() - 1)
                set_sel(get_page() * page_size)
                self.refresh()

        def page_down() -> None:
            total = max(1, (get_len() + page_size - 1) // page_size)
            if get_page() < total - 1:
                set_page(get_page() + 1)
                set_sel(min(get_page() * page_size, get_len() - 1))
                self.refresh()

        self.bind_default_nav(
            on_up=go_up, on_down=go_down, on_enter=on_enter, on_cancel=on_cancel
        )
        if bind_page_keys:
            self.bind("pageup", page_up)
            self.bind("pagedown", page_down)

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

    def as_float(self, *, title: str | None = None) -> Float:
        """Wrap this picker's window in a ``Float`` for a host Application.

        The picker's own key bindings (:attr:`_kb`) are NOT part of the
        returned ``Float`` — a ``Float`` has no independent key-binding
        stack, so the host must merge them into its own ``Application``
        scoped to "this Float is the active one" (e.g. via
        ``ConditionalKeyBindings``/``DynamicKeyBindings``). Construct the
        picker with ``on_done``/``invalidate`` (or set them directly)
        before using this — :meth:`run` is not called in Float mode, so
        :meth:`done`/:meth:`refresh` have nothing else to report to.
        """
        content = Frame(self._window, title=title) if title else self._window
        return Float(content=content, left=2, right=2, top=1, bottom=1)

    def _is_empty(self) -> bool:
        """Subclasses can override to short-circuit run() on empty input."""
        return False
