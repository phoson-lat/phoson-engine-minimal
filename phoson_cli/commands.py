"""
Command handler for the Phoson CLI.

Defines and implements the slash-commands available in the REPL.
"""

from typing import TYPE_CHECKING, Any, Final
from dataclasses import dataclass

if TYPE_CHECKING:
    from .repl import PhosonRepl

COMMANDS: Final[set[str]] = {
    "/exit",
    "/quit",
    "/clear",
    "/new",
    "/model",
    "/subagent-model",
    "/tree",
    "/sessions",
    "/delete",
    "/branch",
    "/label",
    "/attach",
    "/attachments",
    "/help",
    "/env",
    "/cost",
    "/tokens",
    "/steps",
}


@dataclass
class Command:
    """Represents a parsed slash command."""

    name: str
    args: str


def parse_command(text: str) -> Command | None:
    """Parse a string input into a Command, if it starts with '/'."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(maxsplit=1)
    name = parts[0]
    args = parts[1].strip() if len(parts) > 1 else ""
    return Command(name=name, args=args)


class CommandHandler:
    """Handles execution of CLI commands."""

    def __init__(self, repl: "PhosonRepl") -> None:
        """Initialize handler with a REPL reference.

        Args:
            repl: The PhosonRepl instance to operate on.
        """
        self.repl = repl

    async def handle(self, cmd: Command) -> bool:
        """Handle a command. Return False if the REPL should exit."""
        r = self.repl.renderer

        if cmd.name in {"/exit", "/quit"}:
            return False

        if cmd.name in {"/new", "/clear"}:
            self.repl.new_session()
            r.print_info(f"New session  {self.repl.tree.session_id[:8]}")
            return True

        if cmd.name == "/model":
            if not cmd.args:
                r.print_info(f"Model: {self.repl.current_model}")
                return True
            self.repl.set_model(cmd.args)
            r.print_info(f"Model → {self.repl.current_model}")
            return True

        if cmd.name == "/subagent-model":
            if not cmd.args:
                r.print_info(f"Sub-agent model: {self.repl.subagent_model}")
                return True
            self.repl.subagent_model = cmd.args
            self.repl.config.subagent_model = cmd.args
            self.repl.engine.context.extra["default_model"] = cmd.args
            r.print_info(f"Sub-agent model → {cmd.args}")
            return True

        if cmd.name == "/tree":
            r.print_info(self.repl.render_tree_ascii())
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

        if cmd.name == "/sessions":
            sessions = await self.repl.storage.list_meta()
            if not sessions:
                r.print_info("No saved sessions.")
                return True

            from phoson_cli.session_picker import pick_session

            result = await pick_session(
                sessions=sessions,
                current_id=self.repl.tree.session_id,
                page_size=15,
            )

            if result.cancelled:
                r.print_info("Cancelled.")
                return True

            if result.delete:
                if result.session_id == self.repl.tree.session_id:
                    r.print_error(
                        "Cannot delete the current active session. Use /new first."
                    )
                    return True
                await self.repl.storage.delete(result.session_id)
                r.print_info(
                    f"Session {result.session_id[:8]} deleted."
                    " Run /sessions again to refresh."
                )
                return True

            ok = await self.repl.load_session(result.session_id)
            if ok:
                r.print_info(f"Loaded session  {result.session_id[:8]}")
            return True

        if cmd.name == "/delete":
            if not cmd.args:
                r.print_info("Usage:  /delete <session_id>")
                return True
            session_id = cmd.args.strip()
            if session_id == self.repl.tree.session_id:
                r.print_error(
                    "Cannot delete the current active session. Use /new first."
                )
                return True
            try:
                await self.repl.storage.delete(session_id)
                r.print_info(f"Session {session_id[:8]} deleted.")
            except Exception as e:
                r.print_error(f"Failed to delete session: {e}")
            return True

        if cmd.name == "/env":
            r.print_info(
                f"provider={self.repl.config.provider} "
                f"model={self.repl.current_model} "
                f"subagent_model={self.repl.subagent_model} "
                f"cwd={self.repl.config.sessions_dir}"
            )
            return True

        if cmd.name == "/cost":
            r.print_info(
                f"cost=${self.repl.session_metrics.total_cost_usd:.5f} "
                f"credits={self.repl.session_metrics.total_credits:.5f}"
            )
            return True

        if cmd.name == "/tokens":
            r.print_info(
                "tokens="
                f"{self.repl.session_metrics.total_input_tokens}in/"
                f"{self.repl.session_metrics.total_output_tokens}out"
            )
            return True

        if cmd.name == "/steps":
            r.print_info(f"steps={self.repl.session_metrics.step_count}")
            return True

        r.print_error(f"Unknown command: {cmd.name}")
        return True

    async def _handle_attach(self, cmd: Command, r: Any) -> bool:
        """Handle /attach and /attachments commands."""
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

        try:
            self.repl.attachments.attach(cmd.args)
            r.print_info(f"Attached  {cmd.args}")
        except FileNotFoundError as e:
            r.print_error(str(e))
        except ValueError as e:
            r.print_error(str(e))

        return True
