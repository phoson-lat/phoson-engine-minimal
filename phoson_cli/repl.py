import asyncio
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory

from phoson_agent import (
    AgentEngine,
    AgentDoneEvent,
    AgentErrorEvent,
)
from phoson_llm.schemas import Message, ModelConfig
from phoson_agent.sessions import JsonlStorage, ConversationTree

from .tools import build_tools
from .config import PhosonConfig, build_chat
from .commands import CommandHandler, parse_command
from .renderer import Renderer


class PhosonRepl:
    def __init__(self, config: PhosonConfig) -> None:
        self.config = config
        self.storage = JsonlStorage(base_path=config.sessions_dir)
        self.tree = ConversationTree.new()
        self.current_node_id: str | None = None
        self.renderer = Renderer()
        self.current_model = config.model
        self.current_task: asyncio.Task | None = None

        self.engine = AgentEngine(
            chat=build_chat(config),
            tools=build_tools(),
            max_iterations=config.max_iterations,
        )
        self.engine.context.extra["safe_mode"] = config.safe_mode
        self.renderer.set_session(self.tree.session_id)

    async def run(self) -> None:
        self.renderer.console.print("phoson_cli - Terminal Coding Agent")
        self.renderer.console.print("Type /help for commands. Ctrl+D to exit.")

        history_path = Path("~/.phoson/history.txt").expanduser()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        session = PromptSession(history=FileHistory(str(history_path)))
        command_handler = CommandHandler(self)

        while True:
            try:
                prompt = self._build_prompt()
                user_input = await session.prompt_async(prompt)
            except KeyboardInterrupt:
                if self.current_task and not self.current_task.done():
                    self.current_task.cancel()
                    self.renderer.console.print("\nCancelled running task.")
                continue
            except EOFError:
                self.renderer.console.print("\nGoodbye.")
                return

            text = user_input.strip()
            if not text:
                continue

            cmd = parse_command(text)
            if cmd:
                should_continue = await command_handler.handle(cmd)
                if not should_continue:
                    self.renderer.console.print("Goodbye.")
                    return
                continue

            try:
                await self._run_agent(text)
            except KeyboardInterrupt:
                if self.current_task and not self.current_task.done():
                    self.current_task.cancel()
                    self.renderer.console.print("\nCancelled running task.")

    async def _run_agent(self, user_input: str) -> None:
        user_node = self.tree.append(
            parent_id=self.current_node_id,
            message=Message(role="user", content=user_input),
        )
        self.current_node_id = user_node.id

        path = self.tree.get_path(self.current_node_id)
        base_count = len(path)
        config = ModelConfig(model=self.current_model)

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
            self.renderer.console.print("\nSaved partial progress.")
        finally:
            self.renderer.flush_line()
            self.current_task = None

        if done_event and not had_error:
            new_messages = done_event.result.history[base_count:]
            if new_messages:
                created = self.tree.append_many(self.current_node_id, new_messages)
                self.current_node_id = created[-1].id
            await self.storage.save(self.tree)

    def new_session(self) -> None:
        self.tree = ConversationTree.new()
        self.current_node_id = None
        self.renderer.set_session(self.tree.session_id)

    def branch_session(self) -> None:
        if self.current_node_id is None:
            return
        self.current_node_id = self.tree.branch(self.current_node_id)

    def set_model(self, model: str) -> None:
        self.current_model = model
        self.config.model = model
        self.engine = AgentEngine(
            chat=build_chat(self.config),
            tools=build_tools(),
            max_iterations=self.config.max_iterations,
        )
        self.engine.context.extra["safe_mode"] = self.config.safe_mode

    def label_current_node(self, text: str) -> None:
        if self.current_node_id is None:
            return
        self.tree.label(self.current_node_id, text)

    def find_latest_node_id(self) -> str | None:
        if not self.tree.nodes:
            return None
        latest = max(self.tree.nodes.values(), key=lambda n: n.created_at)
        return latest.id

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
            preview = self._message_preview(node.message.content)
            tail = "  [current]" if node_id == self.current_node_id else ""
            branch = "└─ " if is_last else "├─ "
            lines = [f'{prefix}{branch}{marker} {node.id}: "{preview}"{tail}']
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
            preview = self._message_preview(root.message.content)
            tail = "  [current]" if root_id == self.current_node_id else ""
            lines.append(f'{marker} {root.id}: "{preview}"{tail}')
            kids = children.get(root_id, [])
            for j, child_id in enumerate(kids):
                lines.extend(render_node(child_id, "", j == len(kids) - 1))
            if i < len(roots) - 1:
                lines.append("")
        return "\n".join(lines)

    def _build_prompt(self) -> str:
        short_model = self.current_model.split("/")[-1][:24]
        short_node = (self.current_node_id or "root")[:8]
        return f"phoson [{short_model}·{short_node}] › "

    @staticmethod
    def _message_preview(content: object, max_len: int = 52) -> str:
        if isinstance(content, str):
            text = content
        else:
            text = str(content)
        text = " ".join(text.split())
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."
