from typing import TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from .repl import PhosonRepl

COMMANDS = {
    "/exit",
    "/quit",
    "/clear",
    "/new",
    "/model",
    "/tree",
    "/sessions",
    "/branch",
    "/label",
    "/help",
}


@dataclass
class Command:
    name: str
    args: str


def parse_command(text: str) -> Command | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(maxsplit=1)
    name = parts[0]
    args = parts[1].strip() if len(parts) > 1 else ""
    return Command(name=name, args=args)


class CommandHandler:
    def __init__(self, repl: "PhosonRepl") -> None:
        self.repl = repl

    async def handle(self, cmd: Command) -> bool:
        if cmd.name in {"/exit", "/quit"}:
            return False

        if cmd.name in {"/clear", "/new"}:
            self.repl.new_session()
            self.repl.renderer.console.print("Started new session.")
            return True

        if cmd.name == "/model":
            if cmd.args:
                self.repl.set_model(cmd.args)
                self.repl.renderer.console.print(
                    f"Model set to: {self.repl.current_model}"
                )
            else:
                self.repl.renderer.console.print(
                    f"Current model: {self.repl.current_model}"
                )
            return True

        if cmd.name == "/tree":
            self.repl.renderer.console.print(self.repl.render_tree_ascii())
            return True

        if cmd.name == "/sessions":
            sessions = await self.repl.storage.list_sessions()
            if not sessions:
                self.repl.renderer.console.print("No saved sessions.")
                return True

            self.repl.renderer.console.print("Saved sessions:")
            for i, session in enumerate(sessions, start=1):
                self.repl.renderer.console.print(
                    f"{i}. {session.id} | msgs={session.message_count} | "
                    f"updated={session.updated_at.isoformat()}"
                )
            selection = input(
                "Enter session number to load (blank to cancel): "
            ).strip()
            if not selection:
                return True
            if not selection.isdigit() or not (1 <= int(selection) <= len(sessions)):
                self.repl.renderer.console.print("Invalid selection.")
                return True

            picked = sessions[int(selection) - 1]
            self.repl.tree = await self.repl.storage.load(picked.id)
            self.repl.current_node_id = self.repl.find_latest_node_id()
            self.repl.renderer.set_session(self.repl.tree.session_id)
            self.repl.renderer.console.print(f"Loaded session: {picked.id}")
            return True

        if cmd.name == "/branch":
            self.repl.branch_session()
            self.repl.renderer.console.print("Branched from current node.")
            return True

        if cmd.name == "/label":
            if not cmd.args:
                self.repl.renderer.console.print("Usage: /label <text>")
                return True
            self.repl.label_current_node(cmd.args)
            self.repl.renderer.console.print("Label saved.")
            return True

        if cmd.name == "/help":
            self.repl.renderer.console.print("Commands: " + ", ".join(sorted(COMMANDS)))
            return True

        self.repl.renderer.console.print(f"Unknown command: {cmd.name}")
        return True
