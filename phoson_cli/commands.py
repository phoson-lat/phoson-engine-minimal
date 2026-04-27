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
    "/attach",
    "/attachments",
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
        r = self.repl.renderer

        if cmd.name in {"/exit", "/quit"}:
            return False

        if cmd.name in {"/clear", "/new"}:
            self.repl.new_session()
            r.print_info(f"New session  {self.repl.tree.session_id[:8]}")
            return True

        if cmd.name == "/model":
            if cmd.args:
                self.repl.set_model(cmd.args)
                r.print_info(f"Model → {self.repl.current_model}")
            else:
                r.print_info(f"Model: {self.repl.current_model}")
            return True

        if cmd.name == "/tree":
            r.console.print(self.repl.render_tree_ascii())
            return True

        if cmd.name == "/sessions":
            sessions = await self.repl.storage.list_sessions()
            if not sessions:
                r.print_info("No saved sessions.")
                return True

            r.print_sessions_table(sessions)
            raw = input("  Load session #  (blank to cancel): ").strip()
            if not raw:
                return True
            if not raw.isdigit() or not (1 <= int(raw) <= len(sessions)):
                r.print_error("Invalid selection.")
                return True

            picked = sessions[int(raw) - 1]
            self.repl.tree = await self.repl.storage.load(picked.id)
            self.repl.current_node_id = self.repl.find_latest_node_id()
            self.repl.renderer.set_session(self.repl.tree.session_id)
            # Replay the conversation path up to the latest node
            history = self.repl.tree.get_path(self.repl.current_node_id)
            r.print_history(history)
            r.print_info(f"Loaded  {picked.id}  ({len(history)} messages)")
            return True

        if cmd.name == "/branch":
            self.repl.branch_session()
            node = (self.repl.current_node_id or "")[:8]
            r.print_info(f"Branched from  {node}")
            return True

        if cmd.name == "/label":
            if not cmd.args:
                r.print_info("Usage:  /label <text>")
                return True
            self.repl.label_current_node(cmd.args)
            r.print_info(f"Labelled  \u201c{cmd.args}\u201d")
            return True

        if cmd.name in {"/attach", "/attachments"}:
            return await self._handle_attach(cmd, r)

        if cmd.name == "/help":
            r.print_help(COMMANDS)
            return True

        r.print_error(f"Unknown command: {cmd.name}")
        return True

    async def _handle_attach(self, cmd: Command, r) -> bool:
        """Maneja /attach y /attachments."""
        if not cmd.args:
            pending = self.repl.attachments.list_pending()
            if not pending:
                r.print_info("No pending attachments. Usage:  /attach <path> [--clear]")
                return True
            r.print_info(f"{len(pending)} attachment(s) pending:")
            for a in pending:
                r.print_info(f"  📎 {a.path}")
            return True

        if cmd.args == "--clear":
            count = len(self.repl.attachments)
            self.repl.attachments.clear()
            r.print_info(f"Cleared {count} attachment(s).")
            return True

        # Es una ruta de archivo
        try:
            self.repl.attachments.attach(cmd.args)
            r.print_info(f"Attached  {cmd.args}")
        except FileNotFoundError as e:
            r.print_error(str(e))
        except ValueError as e:
            r.print_error(str(e))

        return True
