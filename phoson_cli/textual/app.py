"""The Phoson Textual TUI app (Textual migration, phase 3).

``PhosonTextualApp`` is the second front end over
:class:`phoson_cli.controller.SessionController`:

- :class:`~phoson_cli.textual.sink.TextualSink` routes engine events
  into the conversation widgets (``VerticalScroll`` rows).
- :class:`~phoson_cli.textual.confirmation.TextualConfirmationService`
  answers safe-mode bash prompts with a modal.
- The run lifecycle is one ``asyncio`` task the app owns; ``Ctrl+C``
  cancels the run (the controller persists partial progress — same
  contract as the classic REPL), ``/exit`` or ``Ctrl+Q`` quits.

Slash commands are a TUI-native subset routed through the controller
(``/help /new /tree /undo /label /env /cost /tokens /steps /model
/sessions /exit``); the interactive pickers (``/model``, ``/provider``
without argument) still live in the classic REPL.

Importing this module requires the optional ``tui`` extra — it is only
imported by ``__main__`` when ``--textual`` is requested.
"""

import asyncio
from typing import TYPE_CHECKING
from collections.abc import Iterable, Awaitable

from textual.app import App
from textual.widget import Widget
from textual.widgets import Input, Static
from textual.containers import VerticalScroll

if TYPE_CHECKING:
    from ..config import PhosonConfig

from .sink import TextualSink
from .dialogs import BashConfirmation
from .widgets import ReasoningView, StreamingTurn
from ..controller import SessionController
from .confirmation import TextualConfirmationService


class PhosonTextualApp(App):
    """Textual front end for the Phoson engine."""

    TITLE = "phoson"
    CSS = """
    Screen {
        layout: vertical;
    }
    #conversation {
        height: 1fr;
        padding: 0 1;
        border: round $primary;
    }
    #status {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
        text-style: italic;
    }
    #composer {
        dock: bottom;
        height: 3;
        margin: 0 1 1 1;
    }
    """

    BINDINGS = [
        ("ctrl+t", "toggle_reasoning", "reasoning"),
        ("ctrl+l", "clear_view", "clear view"),
        ("ctrl+c", "interrupt_or_quit", "cancel/quit"),
        ("ctrl+q", "quit_app", "quit"),
    ]

    def __init__(
        self,
        config: "PhosonConfig",
        *,
        controller: SessionController | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._injected_controller = controller
        self._controller: SessionController | None = None
        self._sink: TextualSink | None = None
        self._run_task: asyncio.Task | None = None
        self._current_turn: StreamingTurn | None = None
        self._last_turn: StreamingTurn | None = None
        self._expanded_reasoning: set[str] = set()
        self._pending_confirmation: asyncio.Future[bool] | None = None
        self._shutdown_done = False

    # ── lifecycle ─────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._sink = TextualSink(self)
        self._controller = self._injected_controller or SessionController(
            self._config,
            self._sink,
            confirmation=TextualConfirmationService(self),
        )
        self.update_status_bar()
        self.query_one(Input).focus()

    def compose(self) -> Iterable[Widget]:
        yield VerticalScroll(id="conversation")
        yield Static("", id="status")
        yield Input(
            placeholder=(
                "Ask Phos — /help for commands · Ctrl+T reasoning · "
                "Ctrl+C cancel · Ctrl+Q quit"
            ),
            id="composer",
        )

    def shutdown(self) -> None:
        """Close the chat client and plugins (call after ``run()``).

        Called from ``__main__`` once the app loop has stopped, so the
        (async) controller shutdown runs in its own loop.
        """
        if self._controller is not None and not self._shutdown_done:
            self._shutdown_done = True
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(self._controller.shutdown())
            else:  # pragma: no cover - defensive
                self._fire(self._controller.shutdown())

    async def shutdown_async(self) -> None:
        """Awaited shutdown used from inside the app loop (quit path)."""
        if self._controller is not None and not self._shutdown_done:
            self._shutdown_done = True
            await self._controller.shutdown()

    def _quit(self) -> None:
        """Shutdown the runtime inside the loop, then exit."""
        self._fire(self._shutdown_and_quit())

    async def _shutdown_and_quit(self) -> None:
        await self.shutdown_async()
        self.exit()

    # ── helpers ───────────────────────────────────────────────────

    def _fire(self, awaitable: Awaitable[object]) -> None:
        """Fire-and-forget an awaitable inside the app loop.

        Textual's ``mount``/``scroll_end`` return ``AwaitMount``
        (awaitable, not coroutine), so wrap in a runner task.
        """

        async def _runner() -> None:
            await awaitable

        task = asyncio.ensure_future(_runner())
        task.add_done_callback(self._log_task_error)

    def _log_task_error(self, task: asyncio.Task) -> None:
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                self.log.warning("textual task error: %s", exc)

    def schedule(self, awaitable: Awaitable[object]) -> None:
        """Sink-facing alias of :meth:`_fire`."""
        self._fire(awaitable)

    def scroll_conversation(self) -> None:
        # scroll_end(animate=False) is synchronous — no task needed.
        self.conversation().scroll_end(animate=False)

    def conversation(self) -> VerticalScroll:
        return self.query_one("#conversation", VerticalScroll)

    def current_turn(self) -> StreamingTurn | None:
        return self._current_turn

    def _notify(self, kind: str, message: str) -> None:
        if self._sink is not None:
            self._sink.notify(kind, message)

    def update_status_bar(self) -> None:
        bar = self.query_one("#status", Static)
        parts: list[str] = []
        controller = self._controller
        if controller is not None:
            parts.append(controller.current_model or "?")
            parts.append(str(controller.config.provider))
            session = (controller.current_node_id or "")[:8]
            parts.append(f"session {session or '—'}")
            metrics = controller.session_metrics
            parts.append(f"${metrics.total_cost_usd:,.4f}")
            tokens = metrics.total_input_tokens + metrics.total_output_tokens
            window = getattr(controller, "_context_window", None)
            tokens_label = f"{tokens} tok"
            if window:
                tokens_label += f"/{window}"
            parts.append(tokens_label)
        state = "running…" if self._is_running() else "idle"
        bar.update("  ·  ".join(parts + [state]))

    def _is_running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    # ── input ─────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        if text.startswith("/"):
            self._handle_command(text)
        else:
            self._start_run(text)

    # ── run lifecycle ─────────────────────────────────────────────

    def _start_run(self, text: str) -> None:
        if self._is_running():
            self._notify("warn", "a turn is still running — press Ctrl+C to cancel")
            return
        turn = StreamingTurn()
        self._current_turn = turn
        self.update_status_bar()
        self._run_task = asyncio.ensure_future(self._run(text))
        self._run_task.add_done_callback(self._log_task_error)

    async def _run(self, text: str) -> None:
        controller = self._controller
        turn = self._current_turn
        assert controller is not None and turn is not None
        try:
            # Mount the turn (and its base views) before events arrive.
            await self.conversation().mount(turn)
            await controller.run_turn(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the controller should never raise
            self._notify("error", f"unexpected error: {exc}")
        finally:
            self._last_turn = self._current_turn
            self._current_turn = None
            self._run_task = None
            self.update_status_bar()

    # ── slash commands (TUI subset) ───────────────────────────────

    def _handle_command(self, line: str) -> None:
        parts = line.split(maxsplit=1)
        command = parts[0].lower().lstrip("/")
        arg = parts[1].strip() if len(parts) > 1 else ""
        controller = self._controller
        if controller is None:
            return

        if command in ("exit", "quit"):
            self._quit()
        elif command == "help":
            self._notify(
                "info",
                "commands: /help /new /tree /undo /label <text> /env /cost "
                "/tokens /steps /model [id] /sessions [id] /exit — pickers "
                "live in the classic REPL",
            )
        elif command == "new":
            controller.new_session()
            self._clear_view()
            self.update_status_bar()
            self._notify(
                "info", f"new session {(controller.current_node_id or '')[:8] or '—'}"
            )
        elif command == "tree":
            from .._views import render_tree_ascii

            tree_text = render_tree_ascii(controller.tree, controller.current_node_id)
            self._notify("info", tree_text or "(empty tree)")
        elif command == "undo":
            if self._is_running():
                self._notify("warn", "cancel the running turn first (Ctrl+C)")
            else:
                ok, message = controller.undo_last_turn()
                self._notify("ok" if ok else "warn", message)
        elif command == "label" and arg:
            controller.label_current_node(arg)
            self._notify("info", f"labeled current node as {arg!r}")
        elif command == "env":
            self._notify(
                "info",
                f"provider {controller.config.provider} · model "
                f"{controller.current_model} · session "
                f"{(controller.current_node_id or '')[:8] or '—'}",
            )
        elif command in ("cost", "tokens", "steps"):
            metrics = controller.session_metrics
            if command == "cost":
                self._notify("info", f"session cost ${metrics.total_cost_usd:,.6f}")
            elif command == "tokens":
                window = getattr(controller, "_context_window", None)
                window_text = f" / {window}" if window else ""
                tokens = metrics.total_input_tokens + metrics.total_output_tokens
                self._notify("info", f"session tokens {tokens}{window_text}")
            else:
                self._notify("info", f"session steps {metrics.step_count}")
        elif command == "model":
            if arg:
                controller.set_model(arg)
                self._notify("info", f"model → {arg}")
                self.update_status_bar()
            else:
                self._notify(
                    "info",
                    f"current model: {controller.current_model} — "
                    f"/model <id> switches immediately; the picker lives "
                    "in the classic REPL",
                )
        elif command == "sessions":
            if arg:
                self._fire(self._load_session(arg))
            else:
                self._fire(self._list_sessions())
        else:
            self._notify("error", f"unknown command: {line} (try /help)")

    async def _load_session(self, session_id: str) -> None:
        controller = self._controller
        assert controller is not None
        outcome = await controller.load_session(session_id)
        self._clear_view()
        self.update_status_bar()
        self._notify(
            "ok" if outcome.ok else "error",
            outcome.message
            or (f"session {session_id[:8]} loaded" if outcome.ok else "load failed"),
        )

    async def _list_sessions(self) -> None:
        controller = self._controller
        assert controller is not None
        metas = (await controller.storage.list_meta())[:10]
        if not metas:
            self._notify("info", "no saved sessions")
            return
        for meta in metas:
            self._notify(
                "info",
                f"{meta.id[:8]}  {meta.created_at:%Y-%m-%d %H:%M}  "
                f"${meta.total_cost:,.4f}  {meta.last_model or ''}",
            )

    # ── actions / bindings ────────────────────────────────────────

    def action_interrupt_or_quit(self) -> None:
        if self._is_running():
            controller = self._controller
            if controller is not None:
                controller.cancel_current()
                self._notify("warn", "cancel requested…")
        else:
            self._quit()

    def action_clear_view(self) -> None:
        self._clear_view()
        self._notify("info", "view cleared (session kept)")

    def _clear_view(self) -> None:
        self._fire(self.conversation().remove_children())
        self._current_turn = None
        self._last_turn = None
        self._expanded_reasoning.clear()

    def action_toggle_reasoning(self) -> None:
        turn = self._current_turn or self._last_turn
        if turn is not None and turn.reasoning_view is not None:
            turn.toggle_reasoning()
            return
        # No live block: expand persisted reasoning (once per node).
        controller = self._controller
        if controller is None:
            return
        found = self._latest_persisted_reasoning()
        if found is None:
            self._notify("info", "no reasoning available for the last turn")
            return
        node_id, reasoning = found
        if node_id in self._expanded_reasoning:
            return
        self._expanded_reasoning.add(node_id)
        view = ReasoningView(reasoning)
        view.collapsed = False
        self._fire(self.conversation().mount(view))
        self.scroll_conversation()

    def _latest_persisted_reasoning(self) -> "tuple[str, str] | None":
        """Walk up from the current node; first node with stored reasoning.

        ``tree.get_path`` returns Messages (no metadata), so the lookup
        goes through ``tree.nodes``.
        """
        controller = self._controller
        assert controller is not None
        cursor: str | None = controller.current_node_id
        while cursor is not None:
            node = controller.tree.nodes.get(cursor)
            if node is None:
                return None
            reasoning = (node.metadata or {}).get("reasoning")
            if reasoning:
                return cursor, str(reasoning)
            cursor = node.parent_id
        return None

    def action_quit_app(self) -> None:
        self._quit()

    # ── confirmations ─────────────────────────────────────────────

    async def ask_confirmation(self, prompt: str) -> bool:
        """Show the bash modal and await the answer.

        ``push_screen_wait`` is worker-only in Textual 8.x, so this
        pushes the screen and resolves a future from
        ``on_screen_dismissed``.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()

        def _on_result(result: "object") -> None:
            if not future.done():
                future.set_result(bool(result))

        self._pending_confirmation = future
        self.push_screen(BashConfirmation(prompt), callback=_on_result)
        return await future
