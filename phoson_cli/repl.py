import asyncio
from pathlib import Path
from dataclasses import field, dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.history import FileHistory
from prompt_toolkit.document import Document
from prompt_toolkit.completion import Completer, Completion

from phoson_agent import (
    RunStep,
    AgentEngine,
    AgentDoneEvent,
    AgentErrorEvent,
)
from phoson_llm.schemas import Message, ModelConfig
from phoson_agent.sessions import JsonlStorage, ConversationTree

from .tools import build_tools, build_tools_dict
from .config import PhosonConfig, build_chat
from .commands import COMMANDS, CommandHandler, parse_command
from .renderer import Renderer


@dataclass
class SessionMetrics:
    """Accumulated metrics for the current session."""

    total_cost_usd: float = 0.0
    total_credits: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_cache_read_tokens: int = 0
    step_count: int = 0
    last_model: str = ""
    steps: list[RunStep] = field(default_factory=list)

    # For phoson_weight calculation
    phoson_weight: float = 1.0

    def add_run_step(self, step: RunStep) -> None:
        """Add a run step and update totals."""
        self.steps.append(step)
        self.step_count += 1

        if step.usage:
            self.total_input_tokens += step.usage.input_tokens
            self.total_output_tokens += step.usage.output_tokens
            if step.usage.cache_write_tokens:
                self.total_cache_write_tokens += step.usage.cache_write_tokens
            if step.usage.cache_read_tokens:
                self.total_cache_read_tokens += step.usage.cache_read_tokens

        self.total_cost_usd += step.cost_usd
        self.total_credits += step.credits
        if step.model:
            self.last_model = step.model

    def reset(self) -> None:
        """Reset all metrics for a new session."""
        self.total_cost_usd = 0.0
        self.total_credits = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_write_tokens = 0
        self.total_cache_read_tokens = 0
        self.step_count = 0
        self.last_model = ""
        self.steps.clear()

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def avg_cost_per_message(self) -> float:
        if self.step_count == 0:
            return 0.0
        return self.total_cost_usd / self.step_count

    def load_from_meta(self, meta: dict) -> None:
        """Load metrics from session metadata dict."""
        self.total_cost_usd = meta.get("total_cost_usd", 0.0)
        self.total_credits = meta.get("total_credits", 0.0)
        self.total_input_tokens = meta.get("total_input_tokens", 0)
        self.total_output_tokens = meta.get("total_output_tokens", 0)
        self.total_cache_write_tokens = meta.get("total_cache_write_tokens", 0)
        self.total_cache_read_tokens = meta.get("total_cache_read_tokens", 0)
        self.step_count = meta.get("step_count", 0)
        self.last_model = meta.get("last_model", "")

    def to_meta(self) -> dict:
        """Convert to metadata dict for storage."""
        return {
            "total_cost_usd": self.total_cost_usd,
            "total_credits": self.total_credits,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "step_count": self.step_count,
            "last_model": self.last_model,
        }


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

# ── Command descriptions shown in the meta column ─────────────────────────────
_CMD_META: dict[str, str] = {
    "/exit": "quit phoson_cli",
    "/quit": "quit phoson_cli",
    "/new": "start a new session",
    "/clear": "alias for /new",
    "/model": "show, list, or switch model",
    "/subagent-model": "show or set the LLM model for sub-agents",
    "/env": "show environment variables",
    "/cost": "show session cost breakdown",
    "/tokens": "show token usage stats",
    "/steps": "show execution steps",
    "/tree": "show conversation tree",
    "/sessions": "list & load saved sessions",
    "/branch": "branch from current node",
    "/label": "label current node",
    "/help": "show command reference",
}

_SYSTEM_PROMPT_TEMPLATE = (
    "You are Phos, a terminal coding agent, created by the Phoson.lat team. "
    "You are running in working directory: {cwd}. "
    "Available tools: read_file, write_file, list_dir, bash, web_search, subagents. "
    "Be concise, accurate, and use tools when needed."
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


# Load the phos ASCII art from the package file at import time
_PHOS_ART = (
    (Path(__file__).parent.parent / "phoson_llm" / "phos-ascii.txt")
    .read_text(encoding="utf-8")
    .rstrip("\n")
)


class PhosonRepl:
    def __init__(self, config: PhosonConfig) -> None:
        self.config = config
        self.storage = JsonlStorage(base_path=config.sessions_dir)
        self.tree = ConversationTree.new()
        self.current_node_id: str | None = None
        self.renderer = Renderer()
        self.current_model = config.model
        self.current_task: asyncio.Task | None = None
        self.session_metrics = SessionMetrics()

        # Build tools and registry for sub-agents
        self.tools = build_tools()
        self.tools_dict = build_tools_dict()

        # Create chat instance for sub-agents
        self.chat = build_chat(config)

        self.engine = AgentEngine(
            chat=self.chat,
            tools=self.tools,
            max_iterations=config.max_iterations,
        )

        # Sub-agent model: explicit override or fallback to main model
        self.subagent_model: str = config.subagent_model or config.model

        # Inject context for sub-agents
        self.engine.context.extra["safe_mode"] = config.safe_mode
        self.engine.context.extra["available_tools"] = self.tools_dict
        self.engine.context.extra["default_model"] = self.subagent_model

        self.engine.context.extra["max_iterations"] = config.max_iterations
        self.engine.context.extra["chat"] = self.chat

        self.renderer.set_session(self.tree.session_id)

    async def run(self) -> None:
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

    async def _run_agent(self, user_input: str) -> None:
        user_node = self.tree.append(
            parent_id=self.current_node_id,
            message=Message(role="user", content=user_input),
        )
        self.current_node_id = user_node.id

        self.renderer.print_user_turn(user_input)

        path = self.tree.get_path(self.current_node_id)
        base_count = len(path)
        config = ModelConfig(
            model=self.current_model,
            system=_SYSTEM_PROMPT_TEMPLATE.format(cwd=Path.cwd()),
        )

        done_event: AgentDoneEvent | None = None
        had_error = False

        async def consume() -> None:
            nonlocal done_event, had_error
            async for event in self.engine.stream(path, config):
                self.renderer.on_event(event)
                if isinstance(event, AgentDoneEvent):
                    done_event = event
                elif isinstance(event, AgentErrorEvent):
                    had_error = True

        self.current_task = asyncio.create_task(consume())
        try:
            await self.current_task
        except asyncio.CancelledError:
            partial = self.engine.get_partial_history()
            new_messages = partial[base_count:]
            if new_messages:
                created = self.tree.append_many(self.current_node_id, new_messages)
                self.current_node_id = created[-1].id
            await self.storage.save(self.tree)
            await self.storage.save_meta(
                self.tree.session_id, self.session_metrics.to_meta()
            )
            self.renderer.flush_line()
            self.renderer.print_warn("Partial progress saved.")
        finally:
            self.current_task = None

        if done_event and not had_error:
            new_messages = done_event.result.history[base_count:]
            if new_messages:
                created = self.tree.append_many(self.current_node_id, new_messages)
                self.current_node_id = created[-1].id
            # Update session metrics from run steps
            for step in done_event.result.steps:
                self.session_metrics.add_run_step(step)
            # Save both tree and metadata
            await self.storage.save(self.tree)
            await self.storage.save_meta(
                self.tree.session_id, self.session_metrics.to_meta()
            )

    # ── Session / model management ─────────────────────────────────────────��──

    def new_session(self) -> None:
        self.tree = ConversationTree.new()
        self.current_node_id = None
        self.renderer.set_session(self.tree.session_id)
        self.session_metrics.reset()

    def branch_session(self) -> None:
        if self.current_node_id is None:
            return
        self.current_node_id = self.tree.branch(self.current_node_id)

    def set_model(self, model: str) -> None:
        self.current_model = model
        self.config.model = model

        # Rebuild chat and tools for new model
        self.chat = build_chat(self.config)
        self.tools = build_tools()
        self.tools_dict = build_tools_dict()

        self.engine = AgentEngine(
            chat=self.chat,
            tools=self.tools,
            max_iterations=self.config.max_iterations,
        )

        # Re-inject context for sub-agents
        self.subagent_model = self.config.subagent_model or model
        self.engine.context.extra["safe_mode"] = self.config.safe_mode
        self.engine.context.extra["available_tools"] = self.tools_dict
        self.engine.context.extra["default_model"] = self.subagent_model

        self.engine.context.extra["max_iterations"] = self.config.max_iterations
        self.engine.context.extra["chat"] = self.chat

    def label_current_node(self, text: str) -> None:
        if self.current_node_id is None:
            return
        self.tree.label(self.current_node_id, text)

    def find_latest_node_id(self) -> str | None:
        if not self.tree.nodes:
            return None
        latest = max(self.tree.nodes.values(), key=lambda n: n.created_at)
        return latest.id

    # ── Tree rendering ────────────────────────────────────────────────────────

    def render_tree_ascii(self) -> str:
        if not self.tree.nodes:
            return "(empty session)"

        children: dict[str | None, list[str]] = {}
        for node in self.tree.nodes.values():
            children.setdefault(node.parent_id, []).append(node.id)
            children.setdefault(node.id, [])
        for child_ids in children.values():
            child_ids.sort(key=lambda nid: self.tree.nodes[nid].created_at)

        def render_node(node_id: str, prefix: str, is_last: bool) -> list[str]:
            node = self.tree.nodes[node_id]
            marker = "○" if node_id == self.current_node_id else "●"
            preview = _message_preview(node.message.content)
            tail = "  ← current" if node_id == self.current_node_id else ""
            branch = "└─ " if is_last else "├─ "
            lines = [f"{prefix}{branch}{marker} {node.id}  {preview}{tail}"]
            next_prefix = prefix + ("   " if is_last else "│  ")
            kids = children.get(node_id, [])
            for i, child_id in enumerate(kids):
                lines.extend(render_node(child_id, next_prefix, i == len(kids) - 1))
            return lines

        roots = children.get(None, [])
        lines: list[str] = []
        for i, root_id in enumerate(roots):
            root = self.tree.nodes[root_id]
            marker = "○" if root_id == self.current_node_id else "●"
            preview = _message_preview(root.message.content)
            tail = "  ← current" if root_id == self.current_node_id else ""
            lines.append(f"{marker} {root.id}  {preview}{tail}")
            kids = children.get(root_id, [])
            for j, child_id in enumerate(kids):
                lines.extend(render_node(child_id, "", j == len(kids) - 1))
            if i < len(roots) - 1:
                lines.append("")
        return "\n".join(lines)

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _prompt_fragments(self) -> list[tuple[str, str]]:
        """Return prompt_toolkit (style, text) fragments for the input prompt."""
        short_model = self.current_model.split("/")[-1][:22]
        short_node = (self.current_node_id or "new")[:8]
        return [
            ("class:prompt.prefix", "phoson"),
            ("class:prompt.bracket", " ["),
            ("class:prompt.model", short_model),
            ("class:prompt.sep", "·"),
            ("class:prompt.node", short_node),
            ("class:prompt.bracket", "]"),
            ("class:prompt.arrow", " › "),
            ("", ""),
        ]

    # ── Banner ────────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        from rich.rule import Rule
        from rich.text import Text
        from rich.columns import Columns

        c = self.renderer.console
        c.print()

        # Build art column (purple)
        art = Text(_PHOS_ART, style="medium_purple1 bold")

        # Build wordmark column — lines aligned to logo height
        art_lines = _PHOS_ART.splitlines()
        mid = len(art_lines) // 2
        word_lines: list[str] = [""] * len(art_lines)
        word_lines[mid - 1] = "phoson"
        word_lines[mid] = "terminal agent"
        wordmark = Text("\n".join(word_lines))
        wordmark.highlight_words(["phoson"], style="bold medium_purple1")
        wordmark.highlight_words(["terminal agent"], style="grey50")

        c.print(Columns([art, wordmark], padding=(0, 4)))
        c.print()

        short_model = self.current_model.split("/")[-1]
        c.print(
            Text(
                f"  provider {self.config.provider}  ·  model {short_model}"
                f"  ·  session {self.tree.session_id[:8]}",
                style="grey50",
            )
        )
        c.print(Rule(style="plum4"))
        c.print(
            Text(
                "  /help for commands  ·  /sessions to resume work"
                "  ·  Ctrl+C interrupt  ·  Ctrl+D exit",
                style="grey42",
            )
        )
        c.print()


# ── Helpers ────────────────────────────────────────────────────────────────────


def _message_preview(content: object, max_len: int = 56) -> str:
    text = content if isinstance(content, str) else str(content)
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
