"""Modal Float dialogs for the full-screen front end (issue #187).

Extracted from ``app.py`` to keep ``PhosonApp`` focused on layout, scroll and
lifecycle.  The dialog bodies are unchanged — this is a move, not a rewrite.

``PhosonApp.run_float_*`` remain thin delegates so the public surface used by
``confirmation.py`` and ``command_host.py`` (``self.app.run_float_*``) is
untouched, and ``keys.py`` can still resolve them by name.
"""

import asyncio
from typing import Any
from collections.abc import Callable, Sequence, Coroutine

from prompt_toolkit.widgets import Frame, TextArea
from prompt_toolkit.layout.layout import FocusableElement
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import Float, HSplit, Window
from prompt_toolkit.key_binding.key_bindings import KeyBindings

from phoson_agent import Choice, FormField

from ..pickers import BasePicker


def bash_card_rows(command: str) -> list[tuple[str, str]]:
    """T-6: the permission card's content fragments (testable unit).

    Title + the command in monospace + the three actions. The Float
    wrapper (:meth:`PhosonApp.run_float_bash_card`) renders exactly
    these rows, so the test suite asserts on this function.
    """
    return [
        ("class:title", "  Run bash command?\n\n"),
        ("class:prompt.model", f"  $ {command}\n"),
        ("\n", ""),
        ("class:footer", "  [y] Yes    [a] Always    [n] No / Esc\n"),
    ]


class FloatsController:
    """Owns the modal Float lifecycle (pickers, confirmations, forms).

    References the owning ``PhosonApp`` (``app``) to reach the root
    container, the focus target, and the ``Application``.  The app's float
    state (``_active_float`` / ``_float_kb``) and ``_root_container`` stay on
    the app — ``_build_application`` and ``_build_layout`` read them directly
    — so the controller only mutates them through ``app``.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    @property
    def _root_container(self) -> Any:
        # Lazily resolved (not in ``__init__``): the container is built in
        # ``PhosonApp._build_layout``, which runs *after* this controller is
        # created, so caching it eagerly would capture ``None``.
        return self.app._root_container

    @property
    def _prompt_input(self) -> Any:
        return self.app._prompt_input

    # ── Open / close ───────────────────────────────────────────────────────

    def open_float(
        self, float_: Float, kb: KeyBindings, focus_target: FocusableElement
    ) -> None:
        self._root_container.floats.append(float_)
        self.app._float_kb = kb
        self.app._active_float = float_
        self.app.app.layout.focus(focus_target)
        self.app.app.invalidate()

    def close_float(self, float_: Float) -> None:
        if float_ in self._root_container.floats:
            self._root_container.floats.remove(float_)
        self.app._float_kb = None
        self.app._active_float = None
        self.app.app.layout.focus(self._prompt_input)
        self.app.app.invalidate()

    # ── Dialogs ────────────────────────────────────────────────────────────

    async def run_float_picker(self, picker: BasePicker) -> Any:
        """Show ``picker`` as a modal Float; return its result once resolved."""
        result_future: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_done(result: object) -> None:
            if not result_future.done():
                result_future.set_result(result)

        picker._on_done = on_done
        picker._invalidate = self.app.app.invalidate

        float_ = picker.as_float()
        self.open_float(float_, picker._kb, picker._window)
        try:
            return await result_future
        finally:
            self.close_float(float_)

    async def run_float_confirm(self, prompt: str) -> bool:
        """Show a yes/no Float; return the answer (False on cancel/Ctrl+C).

        Resolving "no" on Ctrl+C (rather than leaving it unhandled) matters
        once this is reused for the bash safe-mode confirmation: cancelling
        the run must not leave the awaiting tool call hanging on a Float
        nobody can answer anymore.
        """
        result_future: asyncio.Future = asyncio.get_running_loop().create_future()

        def resolve(answer: bool) -> None:
            if not result_future.done():
                result_future.set_result(answer)

        kb = KeyBindings()
        kb.add("y")(lambda event: resolve(True))  # noqa: ARG005
        kb.add("Y")(lambda event: resolve(True))  # noqa: ARG005
        kb.add("n")(lambda event: resolve(False))  # noqa: ARG005
        kb.add("N")(lambda event: resolve(False))  # noqa: ARG005
        kb.add("escape")(lambda event: resolve(False))  # noqa: ARG005
        kb.add("c-c")(lambda event: resolve(False))  # noqa: ARG005

        window = Window(
            content=FormattedTextControl(
                lambda: [
                    ("class:title", f"  {prompt}\n\n"),
                    ("class:footer", "  [y] Yes    [n] No / Esc\n"),
                ],
                focusable=True,
            ),
            always_hide_cursor=True,
        )
        float_ = Float(content=Frame(window), left=4, right=4, top=4, bottom=4)

        self.open_float(float_, kb, window)
        try:
            return await result_future
        finally:
            self.close_float(float_)

    async def run_float_bash_card(
        self,
        command: str,
        *,
        on_always: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> bool:
        """T-6: the permission card — command in monospace, 3 actions.

        ``y`` runs the command once; ``a`` runs it and remembers this
        exact command as always-allowed (persisted by the caller through
        ``on_always``); ``n``/Esc denies. Rendered as a proper card
        (title + command body + action footer) instead of a generic
        yes/no modal string.
        """
        result_future: asyncio.Future = asyncio.get_running_loop().create_future()

        def resolve(answer: bool, always: bool = False) -> None:
            if result_future.done():
                return
            if always and on_always is not None:
                try:
                    self.app.app.create_background_task(on_always(command))
                except Exception:
                    # The Application isn't tracking tasks (unit tests):
                    # schedule the grant on the running loop instead.
                    try:
                        asyncio.get_running_loop().create_task(on_always(command))
                    except RuntimeError:  # pragma: no cover - no loop at all
                        pass
            result_future.set_result(answer)

        kb = KeyBindings()
        kb.add("y")(lambda event: resolve(True))  # noqa: ARG005
        kb.add("Y")(lambda event: resolve(True))  # noqa: ARG005
        kb.add("a")(lambda event: resolve(True, always=True))  # noqa: ARG005
        kb.add("A")(lambda event: resolve(True, always=True))  # noqa: ARG005
        kb.add("n")(lambda event: resolve(False))  # noqa: ARG005
        kb.add("N")(lambda event: resolve(False))  # noqa: ARG005
        kb.add("escape")(lambda event: resolve(False))  # noqa: ARG005
        kb.add("c-c")(lambda event: resolve(False))  # noqa: ARG005

        window = Window(
            content=FormattedTextControl(
                lambda: bash_card_rows(command),
                focusable=True,
            ),
            always_hide_cursor=True,
        )
        float_ = Float(content=Frame(window), left=4, right=4, top=4, bottom=4)

        self.open_float(float_, kb, window)
        try:
            return await result_future
        finally:
            self.close_float(float_)

    async def run_float_select(
        self, title: str, message: str, choices: Sequence[Choice]
    ) -> str | None:
        """Show a simple keyboard selector for a plugin interaction."""
        if not choices:
            return None
        result_future: asyncio.Future[str | None] = (
            asyncio.get_running_loop().create_future()
        )
        selected = 0
        kb = KeyBindings()

        def resolve(value: str | None) -> None:
            if not result_future.done():
                result_future.set_result(value)

        def move(delta: int) -> None:
            nonlocal selected
            selected = (selected + delta) % len(choices)
            self.app.app.invalidate()

        kb.add("up")(lambda event: move(-1))  # noqa: ARG005
        kb.add("down")(lambda event: move(1))  # noqa: ARG005
        kb.add("c-p")(lambda event: move(-1))  # noqa: ARG005
        kb.add("c-n")(lambda event: move(1))  # noqa: ARG005
        kb.add("enter")(lambda event: resolve(choices[selected].id))  # noqa: ARG005
        kb.add("escape")(lambda event: resolve(None))  # noqa: ARG005
        kb.add("c-c")(lambda event: resolve(None))  # noqa: ARG005

        def content() -> list[tuple[str, str]]:
            lines = [
                ("class:title", f"  {title}\n"),
                ("class:header", f"  {message}\n"),
            ]
            for index, choice in enumerate(choices):
                marker = "▸" if index == selected else " "
                style = "class:row.selected" if index == selected else "class:row"
                detail = f" — {choice.detail}" if choice.detail else ""
                lines.append((style, f"  {marker} {choice.label}{detail}\n"))
            lines.append(
                ("class:footer", "  ↑/↓ navigate  ·  Enter select  ·  Esc cancel\n")
            )
            return lines

        window = Window(
            content=FormattedTextControl(content, focusable=True),
            always_hide_cursor=True,
        )
        float_ = Float(content=Frame(window), left=4, right=4, top=4, bottom=4)
        self.open_float(float_, kb, window)
        try:
            return await result_future
        finally:
            self.close_float(float_)

    async def run_float_form(
        self, title: str, fields: Sequence[FormField]
    ) -> dict[str, str] | None:
        """Collect a small plugin form in a modal, never exposing widgets to plugins."""
        values: dict[str, TextArea] = {}
        widgets = []
        for field in fields:
            area = TextArea(
                text=field.default or "",
                password=field.kind == "password",
                height=1,
                multiline=False,
            )
            values[field.id] = area
            widgets.extend(
                [
                    Window(
                        content=FormattedTextControl(f"  {field.label}\n"), height=1
                    ),
                    area,
                ]
            )
        result_future: asyncio.Future[dict[str, str] | None] = (
            asyncio.get_running_loop().create_future()
        )
        kb = KeyBindings()

        def resolve() -> None:
            result: dict[str, str] = {}
            for field in fields:
                value = values[field.id].text.strip()
                if field.required and not value:
                    return
                if field.kind == "integer" and value:
                    try:
                        int(value)
                    except ValueError:
                        return
                result[field.id] = value
            if not result_future.done():
                result_future.set_result(result)

        kb.add("enter")(lambda event: resolve())  # noqa: ARG005
        kb.add("escape")(lambda event: result_future.set_result(None))  # noqa: ARG005
        kb.add("c-c")(lambda event: result_future.set_result(None))  # noqa: ARG005
        body = HSplit(widgets)
        float_ = Float(
            content=Frame(body, title=title), left=4, right=4, top=4, bottom=4
        )
        self.open_float(float_, kb, next(iter(values.values()), self._prompt_input))
        try:
            return await result_future
        finally:
            self.close_float(float_)
