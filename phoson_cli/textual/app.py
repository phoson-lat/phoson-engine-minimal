"""The Phoson Textual TUI app.

``PhosonTextualApp`` is the second front end over
:class:`phoson_cli.controller.SessionController`:

- :class:`~phoson_cli.textual.sink.TextualSink` routes engine events
  into the conversation widgets (``VerticalScroll`` rows).
- :class:`~phoson_cli.textual.confirmation.TextualConfirmationService`
  answers safe-mode bash prompts with a modal.
- Slash commands go through the same :class:`~phoson_cli.commands.CommandHandler`
  as the classic REPL, with a Textual :class:`~phoson_cli.command_host.CommandHost`.
- The run lifecycle is one ``asyncio`` task the app owns; ``Ctrl+C``
  cancels the run (the controller persists partial progress — same
  contract as the classic REPL), ``/exit`` or ``Ctrl+Q`` quits.

Importing this module requires the optional ``tui`` extra — it is only
imported by ``__main__`` when ``--textual`` is requested.
"""

import os
import asyncio
from typing import TYPE_CHECKING, cast
from pathlib import Path
from collections.abc import Iterable, Awaitable

from textual.app import App
from textual.timer import Timer
from textual.events import Key, MouseScrollUp, MouseScrollDown
from textual.widget import Widget
from textual.binding import Binding
from textual.widgets import Footer, Static
from textual.containers import VerticalScroll

if TYPE_CHECKING:
    from ..config import PhosonConfig

from .host import TextualCommandHost, TextualSessionFacade
from .sink import TextualSink
from .dialogs import BashConfirmation
from .widgets import Composer, ReasoningView, StreamingTurn
from ..commands import CommandHandler, parse_command
from ..controller import SessionController
from .confirmation import TextualConfirmationService


class _ConversationScroll(VerticalScroll):
    """Conversation viewport with scroll-intent hooks.

    Textual stops consumed scroll events, so the app cannot observe
    wheel input at the app level; the auto-follow flag is maintained
    right here (wheel up releases the pin, wheel back to the bottom
    re-arms it).
    """

    @property
    def app(self) -> "PhosonTextualApp":
        return cast("PhosonTextualApp", super().app)

    def _on_mouse_scroll_up(self, event: MouseScrollUp) -> None:  # noqa: D102
        super()._on_mouse_scroll_up(event)
        self.app._user_scrolled_up()

    def _on_mouse_scroll_down(self, event: MouseScrollDown) -> None:  # noqa: D102
        super()._on_mouse_scroll_down(event)
        if self.app._conversation_at_bottom():
            self.app._user_scrolled_to_bottom()


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
        height: auto;
        min-height: 3;
        max-height: 12;
        margin: 0 1 1 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+t", "toggle_reasoning", "reasoning"),
        Binding("ctrl+l", "clear_view", "clear view"),
        Binding("ctrl+c", "interrupt_or_quit", "cancel/quit", priority=True),
        Binding("ctrl+q", "quit_app", "quit"),
        Binding("ctrl+enter", "submit", "send"),
        Binding("pageup", "page_up", "page up", priority=True),
        Binding("pagedown", "page_down", "page down", priority=True),
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
        self._command_handler: CommandHandler | None = None
        self._run_task: asyncio.Task | None = None
        self._current_turn: StreamingTurn | None = None
        self._last_turn: StreamingTurn | None = None
        self._expanded_reasoning: set[str] = set()
        self._pending_confirmation: asyncio.Future[bool] | None = None
        self._shutdown_done = False
        # Auto-follow: keep the viewport pinned to the bottom while the
        # answer grows — until the user scrolls up (wheel/PgUp) to read.
        self._follow = True
        self._last_max_scroll = -1
        self._follow_timer: Timer | None = None
        # Optional input/lifecycle debug log — PHOSON_TEXTUAL_DEBUG=1
        # (logs to ~/.phoson/tui-debug.log) or =/path/to/file.
        debug_env = os.environ.get("PHOSON_TEXTUAL_DEBUG", "").strip()
        if debug_env.lower() in {"", "0", "false", "no"}:
            self._debug_path: Path | None = None
        else:
            self._debug_path = (
                Path(debug_env)
                if "/" in debug_env or "\\" in debug_env
                else Path.home() / ".phoson" / "tui-debug.log"
            )

    # ── lifecycle ─────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._sink = TextualSink(self)
        self._controller = self._injected_controller or SessionController(
            self._config,
            self._sink,
            confirmation=TextualConfirmationService(self),
        )
        facade = TextualSessionFacade(self)
        self._command_handler = CommandHandler(
            facade,  # type: ignore[arg-type]
            TextualCommandHost(self),
        )
        self.update_status_bar()
        self._welcome()
        self.focus_composer()
        # The Markdown widget renders its blocks asynchronously, so the
        # turn's height keeps growing for a while after the last token;
        # a cheap change-guarded tick re-arms the pin through that tail.
        self._follow_timer = self.set_interval(0.1, self._tick_follow)
        self._debug_log(
            "mounted",
            model=self._controller.current_model,
            provider=self._config.provider,
            kitty_keys_disabled=os.environ.get("TEXTUAL_DISABLE_KITTY_KEY", ""),
        )

    def on_unmount(self) -> None:
        """Stop the follow tick once the app is gone (no post-mortem ticks)."""
        if self._follow_timer is not None:
            self._follow_timer.stop()

    def _welcome(self) -> None:
        controller = self._controller
        assert controller is not None
        session = (controller.current_node_id or "")[:8] or "—"
        self._fire(
            self.conversation().mount(
                Static(
                    f"[bold]phoson[/]  [dim]·[/]  {controller.current_model}  "
                    f"[dim]·[/]  {controller.config.provider}  [dim]·[/]  "
                    f"session {session}  [dim]·[/]  /help for commands",
                )
            )
        )

    def compose(self) -> Iterable[Widget]:
        yield _ConversationScroll(id="conversation")
        yield Static("", id="status")
        yield Footer()
        yield Composer(
            placeholder=(
                "Ask Phos — Enter send · Shift+Enter newline · /help · "
                "Ctrl+T reasoning · Ctrl+C cancel · Ctrl+Q quit"
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
        self._debug_log("quit")
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

    def _conversation_at_bottom(self, threshold: int = 24) -> bool:
        """Whether the conversation viewport sits at (near) the bottom."""
        conv = self.conversation()
        if conv.max_scroll_y <= 0:
            return True
        return conv.scroll_offset.y >= conv.max_scroll_y - threshold

    def follow_if_pinned(self) -> None:
        """Re-pin the viewport to the bottom after a conversation mutation.

        The pin is armed (``_follow``) and released by explicit user
        scroll-up (wheel or PgUp), re-armed at the bottom or on a new
        message — so reading history mid-stream is never fought. The
        0.1s ``_tick_follow`` interval is the safety net for the async
        Markdown growth that outlives the last token.
        """
        conv = self.conversation()
        if not self._follow:
            return
        if conv.max_scroll_y <= 0:
            return  # no scrollable overflow yet
        conv.scroll_end(animate=False)

    def _tick_follow(self) -> None:
        """Follow the bottom whenever the scrollable height changed.

        A diagnostic tick: it must never raise (an exception here would
        kill the app's message pump, e.g. if it fires during teardown).
        """
        try:
            conv = self.conversation()
            mx = conv.max_scroll_y
            if mx == self._last_max_scroll:
                return
            self._last_max_scroll = mx
            if self._follow and mx > 0:
                conv.scroll_end(animate=False)
        except Exception:  # noqa: BLE001 - diagnostics never break the app
            pass

    def _user_scrolled_up(self) -> None:
        self._follow = False

    def _user_scrolled_to_bottom(self) -> None:
        self._follow = True

    def action_page_up(self) -> None:
        """Scroll the conversation up one page (releases the auto-follow)."""
        self._follow = False
        self.conversation().scroll_page_up(animate=False, force=True)

    def action_page_down(self) -> None:
        """Scroll the conversation down one page (re-arms at the bottom)."""
        self.conversation().scroll_page_down(animate=False, force=True)
        if self._conversation_at_bottom():
            self._follow = True

    def _debug_log(self, event: str, **fields: object) -> None:
        """Append a lifecycle/input line to the debug log (when enabled)."""
        if self._debug_path is None:
            return
        try:
            detail = " ".join(f"{k}={v}" for k, v in fields.items())
            line = f"{event}" + (f" {detail}" if detail else "")
            with self._debug_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass  # diagnostics must never break the app

    def on_key(self, event: Key) -> None:  # noqa: D102
        # Catch-all for keys the focused widget does not stop (arrows,
        # ctrl combos). Printable chars are logged by the composer.
        self._debug_log("app-key", key=event.key, character=event.character)

    def conversation(self) -> VerticalScroll:
        return self.query_one("#conversation", VerticalScroll)

    def current_turn(self) -> StreamingTurn | None:
        return self._current_turn

    def focus_composer(self) -> None:
        self.query_one("#composer", Composer).focus()

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
            window = controller.context_window
            tokens_label = f"{tokens} tok"
            if window:
                tokens_label += f"/{window}"
            parts.append(tokens_label)
            pending = len(controller.attachments)
            if pending:
                parts.append(f"📎{pending}")
        state = "running…" if self._is_running() else "idle"
        bar.update("  ·  ".join(parts + [state]))

    def _is_running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    # ── input ─────────────────────────────────────────────────────

    def action_submit(self) -> None:
        self.submit_composer()

    def submit_composer(self) -> None:
        composer = self.query_one("#composer", Composer)
        text = composer.text.strip()
        if not text:
            return
        composer.text = ""
        if text.startswith("/"):
            self._fire(self._handle_command(text))
        else:
            self._follow = True  # a new message re-arms the auto-follow
            self._start_run(text)

    # ── run lifecycle ─────────────────────────────────────────────

    def _start_run(self, text: str) -> None:
        if self._is_running():
            self._notify("warn", "a turn is still running — press Ctrl+C to cancel")
            return
        self._debug_log("run_start", prompt=text[:60])
        turn = StreamingTurn()
        self._current_turn = turn
        composer = self.query_one("#composer", Composer)
        composer.disabled = True
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
            composer = self.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()
            self.update_status_bar()
            self._debug_log("run_end")

    # ── slash commands ────────────────────────────────────────────

    async def _handle_command(self, line: str) -> None:
        handler = self._command_handler
        if handler is None:
            return
        if self._is_running():
            self._notify("warn", "cancel the running turn first (Ctrl+C)")
            return
        cmd = parse_command(line)
        if cmd is None:
            return
        keep = await handler.handle(cmd)
        self.update_status_bar()
        if not keep:
            self._quit()

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
        if self._is_running():
            self._notify("warn", "cancel the running turn first (Ctrl+C)")
            return
        self._clear_view()
        self._notify("info", "view cleared (session kept)")

    def _clear_view(self) -> None:
        self._fire(self.reset_conversation())

    async def reset_conversation(self) -> None:
        """Drop conversation widgets; keep the session. Awaitable for /new."""
        await self.conversation().remove_children()
        self._current_turn = None
        self._last_turn = None
        self._expanded_reasoning.clear()
        self._follow = True
        self._last_max_scroll = -1

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
        answer = await future
        self._debug_log("confirmation", prompt=prompt[:60], answer=answer)
        self.focus_composer()
        return answer
