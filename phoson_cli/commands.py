"""
Command handler for the Phoson CLI.

Defines and implements the slash-commands available in the REPL.
"""

from typing import TYPE_CHECKING, Any, Final
from dataclasses import dataclass

from .config import save_config
from .installer import run_install_wizard
from .model_picker import pick_model
from .model_selector import list_available_models
from .provider_picker import pick_provider

if TYPE_CHECKING:
    from .repl import PhosonRepl

COMMANDS: Final[set[str]] = {
    "/exit",
    "/quit",
    "/clear",
    "/new",
    "/model",
    "/provider",
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
    "/setup",
    "/mcp",
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
                models = await list_available_models(self.repl.config)
                if not models:
                    r.print_info("No models available.")
                    return True
                result = await pick_model(
                    models=models,
                    current_model=self.repl.current_model,
                )
                if result.cancelled or not result.model_id:
                    r.print_info("Cancelled.")
                    return True
                self.repl.set_model(result.model_id)
                save_config(self.repl.config)
                r.print_info(f"Model → {self.repl.current_model}  ·  saved")
                return True
            if cmd.args == "list":
                models = await list_available_models(self.repl.config)
                if not models:
                    r.print_info("No models available.")
                    return True
                r.print_info("Available models:")
                for option in models:
                    marker = "*" if option.id == self.repl.current_model else " "
                    suffix = f" [{option.provider}]" if option.provider else ""
                    r.print_info(f" {marker} {option.id}{suffix}")
                return True
            self.repl.set_model(cmd.args)
            save_config(self.repl.config)
            r.print_info(f"Model → {self.repl.current_model}  ·  saved")
            return True

        if cmd.name == "/provider":
            providers = self._available_providers()
            if not providers:
                r.print_info("No providers configured. Run /setup first.")
                return True
            if cmd.args == "list":
                r.print_info("Available providers:")
                for provider in providers:
                    marker = "*" if provider == self.repl.config.provider else " "
                    r.print_info(f" {marker} {provider}")
                return True

            target_provider = cmd.args or None
            if not target_provider:
                result = await pick_provider(
                    providers=providers,
                    current_provider=self.repl.config.provider,
                )
                if result.cancelled or not result.provider:
                    r.print_info("Cancelled.")
                    return True
                target_provider = result.provider

            if target_provider not in providers:
                r.print_error(f"Provider not configured: {target_provider}")
                return True

            try:
                self.repl.set_provider(target_provider)
            except ValueError as e:
                r.print_error(str(e))
                return True

            models = await list_available_models(self.repl.config)
            if not models:
                save_config(self.repl.config)
                r.print_info(f"Provider → {self.repl.config.provider}  ·  saved")
                r.print_info("No models available for the selected provider.")
                return True

            model_result = await pick_model(
                models=models,
                current_model=self.repl.current_model,
            )
            if model_result.cancelled or not model_result.model_id:
                save_config(self.repl.config)
                r.print_info(f"Provider → {self.repl.config.provider}  ·  saved")
                r.print_info("Model selection cancelled; kept current model.")
                return True

            self.repl.set_model(model_result.model_id)
            save_config(self.repl.config)
            r.print_info(
                "Provider → "
                f"{self.repl.config.provider}  ·  "
                f"Model → {self.repl.current_model}  ·  saved"
            )
            return True

        if cmd.name == "/subagent-model":
            if not cmd.args:
                models = await list_available_models(self.repl.config)
                if not models:
                    r.print_info("No models available.")
                    return True
                result = await pick_model(
                    models=models,
                    current_model=self.repl.subagent_model,
                )
                if result.cancelled or not result.model_id:
                    r.print_info("Cancelled.")
                    return True
                self.repl.subagent_model = result.model_id
                self.repl.config.subagent_model = result.model_id
                self.repl.engine.context.extra["default_model"] = result.model_id
                save_config(self.repl.config)
                r.print_info(f"Sub-agent model → {result.model_id}  ·  saved")
                return True
            if cmd.args == "list":
                models = await list_available_models(self.repl.config)
                if not models:
                    r.print_info("No models available.")
                    return True
                r.print_info("Available sub-agent models:")
                for option in models:
                    marker = "*" if option.id == self.repl.subagent_model else " "
                    suffix = f" [{option.provider}]" if option.provider else ""
                    r.print_info(f" {marker} {option.id}{suffix}")
                return True
            self.repl.subagent_model = cmd.args
            self.repl.config.subagent_model = cmd.args
            self.repl.engine.context.extra["default_model"] = cmd.args
            save_config(self.repl.config)
            r.print_info(f"Sub-agent model → {cmd.args}  ·  saved")
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

        if cmd.name == "/setup":
            self.repl.config = await run_install_wizard(self.repl.config)
            self.repl.set_model(self.repl.config.model)
            r.print_info("Setup completed.")
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

        if cmd.name == "/mcp":
            return await self._handle_mcp(cmd)

        r.print_error(f"Unknown command: {cmd.name}")
        return True

    def _available_providers(self) -> list[str]:
        config = self.repl.config
        providers: list[str] = []
        if config.openrouter_api_key:
            providers.append("openrouter")
        if config.openai_api_key:
            providers.append("openai")
        if config.anthropic_api_key:
            providers.append("anthropic")
        if config.ollama_base_url:
            providers.append("ollama")
        if config.provider not in providers:
            providers.append(config.provider)
        return providers

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

    async def _handle_mcp(self, cmd: Command) -> bool:
        """Handle /mcp command for Model Context Protocol management."""
        r = self.repl.renderer

        if cmd.name != "/mcp":
            return True

        # /mcp status - show current MCP status
        if cmd.args == "status" or not cmd.args:
            status = "enabled" if self.repl.config.enable_mcp else "disabled"
            r.print_info(f"MCP: {status}")
            if self.repl.config.enable_mcp:
                r.print_info(f"Config file: {self.repl.config.mcp_config_file}")
                
                # Show loaded MCP tools
                mcp_tools = [t for t in self.repl.engine.tools if t.name.startswith("mcp_")]
                if mcp_tools:
                    r.print_info(f"Loaded {len(mcp_tools)} MCP tool(s):")
                    for tool in mcp_tools:
                        r.print_info(f"  • {tool.name}")
                else:
                    r.print_info("No MCP tools loaded (check config file)")
            else:
                r.print_info("Use '/mcp enable' to activate MCP support")
            return True

        # /mcp enable - enable MCP
        if cmd.args == "enable":
            if self.repl.config.enable_mcp:
                r.print_info("MCP is already enabled")
                return True
            
            self.repl.config.enable_mcp = True
            save_config(self.repl.config)
            
            # Rebuild engine with MCP plugin
            self.repl.set_model(self.repl.current_model)
            
            r.print_info("MCP enabled  ·  saved")
            r.print_info(f"Config file: {self.repl.config.mcp_config_file}")
            r.print_info("Restart or run '/mcp status' to see loaded tools")
            return True

        # /mcp disable - disable MCP
        if cmd.args == "disable":
            if not self.repl.config.enable_mcp:
                r.print_info("MCP is already disabled")
                return True
            
            self.repl.config.enable_mcp = False
            save_config(self.repl.config)
            
            # Rebuild engine without MCP plugin
            self.repl.set_model(self.repl.current_model)
            
            r.print_info("MCP disabled  ·  saved")
            return True

        # /mcp config <path> - set config file path
        if cmd.args.startswith("config "):
            from pathlib import Path
            config_path = cmd.args[7:].strip()
            self.repl.config.mcp_config_file = Path(config_path)
            save_config(self.repl.config)
            
            # Rebuild engine if MCP is enabled
            if self.repl.config.enable_mcp:
                self.repl.set_model(self.repl.current_model)
            
            r.print_info(f"MCP config file → {config_path}  ·  saved")
            return True

        # /mcp help - show help
        if cmd.args == "help":
            r.print_info("MCP (Model Context Protocol) commands:")
            r.print_info("  /mcp status          Show MCP status and loaded tools")
            r.print_info("  /mcp enable          Enable MCP support")
            r.print_info("  /mcp disable         Disable MCP support")
            r.print_info("  /mcp config <path>   Set MCP config file path")
            r.print_info("  /mcp help            Show this help")
            r.print_info("")
            r.print_info("Example phoson-mcp.json:")
            r.print_info('  {')
            r.print_info('    "mcpServers": {')
            r.print_info('      "filesystem": {')
            r.print_info('        "command": "npx",')
            r.print_info('        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]')
            r.print_info('      }')
            r.print_info('    }')
            r.print_info('  }')
            return True

        r.print_error(f"Unknown /mcp command: {cmd.args}")
        r.print_info("Use '/mcp help' for available commands")
        return True
