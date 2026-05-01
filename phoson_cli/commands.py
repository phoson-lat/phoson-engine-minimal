from typing import TYPE_CHECKING, Final
from dataclasses import dataclass

if TYPE_CHECKING:
    from .repl import PhosonRepl

COMMANDS: Final[set[str]] = {
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
    # ── New diagnostic commands ──────────────────────────────────────────────
    "/env",
    "/cost",
    "/tokens",
    "/steps",
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

        if cmd.name == "/help":
            r.print_help(COMMANDS)
            return True

        if cmd.name == "/sessions":
            sessions = await self.repl.storage.list_meta()
            if not sessions:
                r.print_info("No saved sessions.")
                return True
            r.print_sessions_table(sessions)
            return True

        if cmd.name == "/env":
            r.print_info(
                f"provider={self.repl.config.provider} model={self.repl.current_model} cwd={self.repl.config.sessions_dir}"
            )
            return True

        if cmd.name == "/cost":
            r.print_info(
                f"cost=${self.repl.session_metrics.total_cost_usd:.5f} credits={self.repl.session_metrics.total_credits:.5f}"
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
