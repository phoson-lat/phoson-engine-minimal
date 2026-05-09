import asyncio
import logging
from typing import Any
from pathlib import Path

_LOGGER = logging.getLogger("phoson_cli.repl")

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.history import FileHistory
from prompt_toolkit.document import Document
from prompt_toolkit.completion import Completer, Completion

from phoson_agent import (
    AgentEngine,
    AgentDoneEvent,
    AgentErrorEvent,
    Plugin,
)
from phoson_llm.schemas import Message, ModelConfig, ContentBlock
from phoson_agent.sessions import JsonlStorage, ConversationTree
from phoson_agent.plugins.summarizer import SummarizationMiddleware
from phoson_agent.plugins.context_window import ContextWindowResolver

from .tools import build_tools, build_tools_dict
from ._views import print_banner, render_tree_ascii
from ._session import SessionMetrics, SessionState
from .config import PhosonConfig, build_chat
from .commands import COMMANDS, COMMAND_SPECS, CommandHandler, parse_command
from .renderer import Renderer
from .attachments import AttachmentManager


# ── Prompt style ──────────────────────────────────────────────────────────────
# purple accent on prefix/arrow, muted elsewhere; completion menu purple
_PROMPT_STYLE = Style.from_dict(
    {
        # input
        "": "#9a8faa",
        "prompt.prefix": "#b57bee bold",
        "prompt.bracket": "#5a4e6e",
        "prompt.model": "#e0d0ff bold",
        "prompt.sep": "#5a4e6e",
        "prompt.node": "#6b5b8a",
        "prompt.tokens": "#8a7a9a",
        "prompt.arrow": "#b57bee bold",
        # completion dropdown
        "completion-menu": "bg:#1e1530 #9a8faa",
        "completion-menu.completion": "bg:#1e1530 #9a8faa",
        "completion-menu.completion.current": "bg:#3d2b6e #e0d0ff bold",
        "completion-menu.meta": "bg:#150f24 #6b5b8a",
        "completion-menu.meta.current": "bg:#3d2b6e #9b72cf",
        "scrollbar.background": "bg:#150f24",
        "scrollbar.button": "bg:#5a4e6e",
    }
)

# Build a flat ``name -> help`` table from the central COMMAND_SPECS so the
# completer's meta column stays in sync with /help and the dispatch table.
_CMD_META: dict[str, str] = {
    name: spec.help for spec in COMMAND_SPECS for name in spec.names
}

_SYSTEM_PROMPT_TEMPLATE = (
    "You are Phos, a terminal coding agent, created by the Phoson.lat team. "
    "You are running in working directory: {cwd}. "
    "Available tools: read_file, write_file, patch_file, list_dir, bash, "
    "web_search, subagents. Be concise, accurate, and use tools when needed."
)


class _SlashCompleter(Completer):
    """Completes slash commands only when the buffer starts with '/'."""

    def get_completions(self, document: Document, complete_event: object):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        # Only complete the command word itself (no args)
        if " " in text:
            return

        word = text.lower()
        for cmd in sorted(COMMANDS):
            if cmd.startswith(word):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=_CMD_META.get(cmd, ""),
                )


# Banner ASCII art and tree-rendering live in ``_views`` so they can be
# imported, replaced or tested without instantiating the REPL.


class PhosonRepl:
    """Interactive REPL for the Phoson agent platform.

    Handles user input, command execution, agent running, and session management.
    """

    def __init__(self, config: PhosonConfig) -> None:
        """Initialize the REPL with configuration.

        Args:
            config: PhosonConfig containing provider, model, and session settings.
        """
        self.config = config
        self.storage = JsonlStorage(base_path=config.sessions_dir)
        self._session = SessionState.new()
        self.renderer = Renderer()
        self.current_model = config.model
        self.current_task: asyncio.Task | None = None
        self.attachments = AttachmentManager()

        # Sub-agent model: explicit override or fallback to main model.
        self.subagent_model: str = config.subagent_model or config.model

        # Context window resolver + token estimator for prompt display.
        self._cw_resolver = ContextWindowResolver(
            ollama_base_url=config.ollama_base_url or "http://localhost:11434",
            openrouter_api_key=config.openrouter_api_key,
        )
        self._context_window: int = 128_000  # default, resolved on first use
        self._context_tokens: int = 0  # current estimated tokens in context

        # Summarization middleware. The provider/model fields are kept in
        # sync with the active config every time ``_rebuild_engine`` runs.
        self.summarizer = SummarizationMiddleware(
            threshold=0.80,
            min_keep_messages=4,
            provider=config.provider,
            model=config.model,
            ollama_base_url=config.ollama_base_url or "http://localhost:11434",
            openrouter_api_key=config.openrouter_api_key,
        )

        # Build the runtime (chat client, tools, plugins, engine).
        self._rebuild_engine()

        self.renderer.set_session(self._session.tree.session_id)

    # ── Session state properties ──────────────────────────────────────────────

    @property
    def tree(self) -> "ConversationTree":
        """The active conversation tree."""
        return self._session.tree

    @tree.setter
    def tree(self, value: "ConversationTree") -> None:
        self._session.tree = value

    @property
    def current_node_id(self) -> str | None:
        """ID of the most recently active tree node."""
        return self._session.current_node_id

    @current_node_id.setter
    def current_node_id(self, value: str | None) -> None:
        self._session.current_node_id = value

    @property
    def session_metrics(self) -> SessionMetrics:
        """Accumulated metrics for the current session."""
        return self._session.metrics

    # ── Engine (re)construction ───────────────────────────────────────────────

    def _build_mcp_plugins(self) -> list[Plugin | dict[str, Any]]:
        """Resolve the MCP plugin specs for the current configuration.

        Returns an empty list when MCP is disabled. Tries the in-tree
        ``phoson_plugin_mcp`` first; falls back to the path-based loader
        used during local development if the package is not installed.
        """
        if not self.config.enable_mcp:
            return []

        mcp_config = {
            "config_file": str(self.config.mcp_config_file),
            "tool_name_prefix": "mcp",
        }

        try:
            from phoson_plugin_mcp import MCPPlugin

            plugin = MCPPlugin()
            plugin.configure(mcp_config)
            return [plugin]
        except ImportError:
            return [
                {
                    "name": "path:./phoson_plugin_mcp/plugin.py",
                    "config": mcp_config,
                }
            ]

    def _rebuild_engine(self) -> None:
        """(Re)build chat client, tool registry, plugins and the engine.

        Called from ``__init__`` and from every command that mutates
        provider/model/MCP state. The summarizer's provider/model fields
        are also refreshed so token estimation and context-window
        resolution stay accurate.
        """
        self.chat = build_chat(self.config)
        self.tools = build_tools()
        self.tools_dict = build_tools_dict()

        self.summarizer.provider = self.config.provider
        self.summarizer.model = self.config.model

        plugins = self._build_mcp_plugins()

        self.engine = AgentEngine(
            chat=self.chat,
            tools=self.tools,
            middlewares=[self.summarizer],
            plugins=plugins,
            max_iterations=self.config.max_iterations,
        )

        # Inject runtime context for sub-agents.
        self.engine.context.extra["safe_mode"] = self.config.safe_mode
        self.engine.context.extra["available_tools"] = self.tools_dict
        self.engine.context.extra["default_model"] = self.subagent_model
        self.engine.context.extra["max_iterations"] = self.config.max_iterations
        self.engine.context.extra["chat"] = self.chat

    async def run(self) -> None:
        """Run the REPL main loop.

        Displays the banner, initializes the prompt session, and processes
        user input until EOF or /exit command.
        """
        self._print_banner()

        history_path = Path("~/.phoson/history.txt").expanduser()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        session = PromptSession(
            history=FileHistory(str(history_path)),
            style=_PROMPT_STYLE,
            completer=_SlashCompleter(),
            complete_while_typing=True,
            reserve_space_for_menu=6,
        )
        command_handler = CommandHandler(self)

        while True:
            try:
                prompt_fragments = self._prompt_fragments()
                user_input = await session.prompt_async(prompt_fragments)
            except KeyboardInterrupt:
                if self.current_task and not self.current_task.done():
                    self.current_task.cancel()
                    self.renderer.print_warn("Interrupted — run cancelled.")
                continue
            except EOFError:
                self.renderer.print_info("Bye.")
                return

            text = user_input.strip()
            if not text:
                continue

            cmd = parse_command(text)
            if cmd:
                should_continue = await command_handler.handle(cmd)
                if not should_continue:
                    self.renderer.print_info("Bye.")
                    return
                continue

            try:
                await self._run_agent(text)
            except KeyboardInterrupt:
                if self.current_task and not self.current_task.done():
                    self.current_task.cancel()
                    self.renderer.print_warn("Interrupted — run cancelled.")

    # ── Agent execution ───────────────────────────────────────────────────────

    def _build_user_message(self, user_input: str) -> Message:
        """Flush pending attachments and construct the user Message."""
        pending_blocks: list[ContentBlock] = []
        if self.attachments:
            pending_blocks = list(self.attachments.flush())
            for block in pending_blocks:
                self.renderer.print_info(
                    f"  📎 {block.source.split('file://', 1)[-1]}"
                )

        if user_input:
            pending_blocks.insert(0, _text_block(user_input))

        content: str | list[ContentBlock] = (
            pending_blocks if pending_blocks else user_input
        )
        return Message(role="user", content=content)

    def _append_user_turn(
        self, message: Message
    ) -> tuple[str, list[Message]]:
        """Append user message to the tree.

        Returns:
            Tuple of (new_node_id, full conversation path).
        """
        node = self.tree.append(
            parent_id=self.current_node_id,
            message=message,
        )
        self.current_node_id = node.id
        path = self.tree.get_path(self.current_node_id)
        return node.id, path

    async def _consume_stream(
        self, path: list[Message], config: ModelConfig
    ) -> AgentDoneEvent:
        """Run the agent stream loop via renderer.on_event().

        Re-raises ``asyncio.CancelledError`` without catching — the caller
        (``_run_agent``) handles it so that ``base_count`` stays in scope
        for ``_save_partial``.
        """
        done_event: AgentDoneEvent | None = None

        async def consume() -> None:
            nonlocal done_event
            async for event in self.engine.stream(path, config):
                self.renderer.on_event(event)
                if isinstance(event, AgentDoneEvent):
                    done_event = event

        self.current_task = asyncio.create_task(consume())
        await self.current_task
        self.current_task = None

        # consume() always sets done_event before the task completes
        assert done_event is not None
        return done_event

    def _finalize_run(
        self, done_event: AgentDoneEvent, base_count: int
    ) -> None:
        """Append new messages to tree, update metrics, save session."""
        new_messages = done_event.result.history[base_count:]
        if new_messages:
            created = self.tree.append_many(self.current_node_id, new_messages)
            self.current_node_id = created[-1].id

        self._context_tokens = self.summarizer.estimate_tokens(
            done_event.result.history
        )
        for step in done_event.result.steps:
            self.session_metrics.add_run_step(step)

    def _append_partial_history(self, base_count: int) -> None:
        """Slice engine partial history and append new nodes to the tree.

        Updates ``current_node_id`` to the last appended node.
        Called synchronously from the ``CancelledError`` handler in
        ``_run_agent`` before the async saves.
        """
        partial = self.engine.get_partial_history()
        new_messages = partial[base_count:]
        if new_messages:
            created = self.tree.append_many(self.current_node_id, new_messages)
            self.current_node_id = created[-1].id

    async def _run_agent(self, user_input: str) -> None:
        """Execute the agent with user input.

        Args:
            user_input: The user's message text.
        """
        user_message = self._build_user_message(user_input)
        _node_id, path = self._append_user_turn(user_message)
        base_count = len(path)

        self.renderer.print_user_turn(user_input)

        config = ModelConfig(
            model=self.current_model,
            system=_SYSTEM_PROMPT_TEMPLATE.format(cwd=Path.cwd()),
        )

        # Resolve context window and estimate current tokens
        self._context_window = await self._cw_resolver.resolve(
            self.config.provider, self.current_model
        )
        self._context_tokens = self.summarizer.estimate_tokens(path)

        try:
            done_event = await self._consume_stream(path, config)
        except asyncio.CancelledError:
            self.renderer.flush_line()
            self._append_partial_history(base_count)
            await self.storage.save(self.tree)
            await self.storage.save_meta(
                self.tree.session_id, self.session_metrics.to_meta()
            )
            self.renderer.print_warn("Partial progress saved.")
            return
        finally:
            self.current_task = None

        self._finalize_run(done_event, base_count)
        await self.storage.save(self.tree)
        await self.storage.save_meta(
            self.tree.session_id, self.session_metrics.to_meta()
        )

    # ── Session / model management ─────────────────────────────────────────────

    def new_session(self) -> None:
        """Start a fresh session, resetting tree and metrics."""
        self._session.reset()
        self.attachments.clear()
        self.renderer.set_session(self._session.tree.session_id)

    async def load_session(self, session_id: str) -> bool:
        """Load a session from storage and replay its tail. Returns True on success."""
        try:
            self._session.tree = await self.storage.load(session_id)
            self._session.current_node_id = self.find_latest_node_id()
            self._session.metrics = SessionMetrics()
            self.renderer.set_session(self._session.tree.session_id)

            # Load saved metrics using the authoritative SessionMeta field names.
            metas = await self.storage.list_meta()
            for meta in metas:
                if str(meta.id) == session_id:
                    self.session_metrics.total_cost_usd = meta.total_cost
                    self.session_metrics.total_output_tokens = meta.total_tokens
                    self.session_metrics.step_count = meta.step_count
                    self.session_metrics.last_model = meta.last_model or ""
                    break

            # Replay the tail of the session so the user knows where they left off.
            try:
                path = self.tree.get_path(self.current_node_id)
                self.renderer.print_history(path, tail=6)
            except Exception:
                pass  # corrupted node — session still loaded successfully

            return True
        except FileNotFoundError:
            self.renderer.print_error(f"Session {session_id[:8]} not found.")
            return False
        except Exception as e:
            _LOGGER.exception("Failed to load session %s", session_id[:8])
            self.renderer.print_error(f"Failed to load session: {e}")
            return False

    def branch_session(self) -> None:
        """Branch the conversation from the current node."""
        if self.current_node_id is None:
            return
        self.current_node_id = self.tree.branch(self.current_node_id)

    def set_provider(self, provider: str) -> None:
        """Switch to a different provider and rebuild runtime state."""
        self.config.provider = provider
        self.set_model(self.config.model)

    def set_model(self, model: str) -> None:
        """Switch to a different model and rebuild the engine.

        Args:
            model: The new model name to use.
        """
        self.current_model = model
        self.config.model = model
        # Sub-agent model follows the main model unless explicitly overridden.
        self.subagent_model = self.config.subagent_model or model
        self._rebuild_engine()

    def label_current_node(self, text: str) -> None:
        """Label the current node with text."""
        if self.current_node_id is None:
            return
        self.tree.label(self.current_node_id, text)

    def find_latest_node_id(self) -> str | None:
        """Find the most recently created node."""
        if not self.tree.nodes:
            return None
        latest = max(self.tree.nodes.values(), key=lambda n: n.created_at)
        return latest.id

    # ── Tree rendering ────────────────────────────────────────────────────────

    def render_tree_ascii(self) -> str:
        """Render the conversation tree as an ASCII diagram."""
        return render_tree_ascii(self.tree, self.current_node_id)

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _prompt_fragments(self) -> list[tuple[str, str]]:
        """Return prompt_toolkit (style, text) fragments for the input prompt."""
        short_model = self.current_model.split("/")[-1][:22]
        short_node = (self.current_node_id or "new")[:8]
        # Show pending attachments indicator
        attach_indicator = f" 📎{len(self.attachments)}" if self.attachments else ""

        # Token context indicator
        token_part = self._token_indicator()

        return [
            ("class:prompt.prefix", "phoson"),
            ("class:prompt.bracket", " ["),
            ("class:prompt.model", short_model),
            ("class:prompt.sep", "·"),
            ("class:prompt.node", short_node),
            ("class:prompt.sep", attach_indicator),
            ("class:prompt.sep", "·"),
            ("class:prompt.tokens", token_part),
            ("class:prompt.bracket", "]"),
            ("class:prompt.arrow", " › "),
            ("", ""),
        ]

    def _token_indicator(self) -> str:
        """Return a short token usage string like '12.4k/128k'."""
        if self._context_window <= 0:
            return "?"
        used = self._context_tokens
        total = self._context_window

        def _fmt(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.1f}k"
            return str(n)

        return f"{_fmt(used)}/{_fmt(total)}"

    # ── Banner ────────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        """Render the welcome banner."""
        print_banner(
            self.renderer.console,
            provider=self.config.provider,
            model=self.current_model,
            session_id=self.tree.session_id,
        )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _text_block(text: str) -> "ContentBlock":
    """Create a TextBlock inline."""
    from phoson_llm.schemas import TextBlock

    return TextBlock(text=text)
