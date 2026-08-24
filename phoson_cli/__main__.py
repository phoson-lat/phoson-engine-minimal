"""Entry point for the Phoson CLI application."""

import sys
import shutil
import asyncio
import subprocess
from pathlib import Path

from phoson_cli.config import (
    PhosonConfig,
    build_chat,
    load_config,
    has_configured_provider,
)
from phoson_cli.updater import perform_self_update
from phoson_cli.installer import run_install_wizard
from phoson_cli.fullscreen.app import PhosonApp


def self_update() -> None:
    """Upgrade phoson-cli to the latest version (with confirmation)."""
    summary = asyncio.run(perform_self_update(assume_yes=False))
    print(summary)
    if "Update failed" in summary:
        sys.exit(1)


def uninstall() -> None:
    """Remove phoson-cli and optionally config."""
    print("Uninstalling phoson-cli...")

    result = subprocess.run(
        ["uv", "tool", "uninstall", "phoson-engine-minimal"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        print("Package uninstalled.")
    else:
        print(f"Failed to uninstall package: {result.stderr}", file=sys.stderr)

    config_dir = Path.home() / ".phoson"
    if config_dir.exists():
        response = input("Remove ~/.phoson config directory? [y/N] ")
        if response.lower() in {"y", "yes"}:
            shutil.rmtree(config_dir)
            print("Config directory removed.")


def _read_stdin_task() -> str:
    """Read a task from stdin; OSError (e.g. closed/captured stdin) → empty."""
    try:
        return sys.stdin.read().strip()
    except OSError:
        return ""


def _parse_oneshot_task(args: list[str]) -> str | None:
    """Return the one-shot task, or None for interactive REPL mode.

    Accepted forms:
      phoson-cli "task"            (positional)
      phoson-cli -p "task"         (--print)
      echo "task" | phoson-cli     (piped stdin, with or without -p)
    """
    print_mode = any(a in {"-p", "--print"} for a in args)
    positional = [a for a in args if not a.startswith("-")]

    if positional:
        return " ".join(positional)

    piped = not sys.stdin.isatty()
    if print_mode and piped:
        task = _read_stdin_task()
        if not task:
            print("Error: -p/--print received empty stdin.", file=sys.stderr)
            sys.exit(1)
        return task
    if print_mode:
        print(
            "Error: -p/--print needs a task argument or piped stdin.",
            file=sys.stderr,
        )
        sys.exit(1)
    if piped:
        task = _read_stdin_task()
        if task:
            return task
    return None


async def _run_oneshot(config: PhosonConfig, task: str) -> int:
    """Run a single agent task and print the final content to stdout.

    No REPL, no session persistence — intended for scripts and CI.
    Returns 0 on success, 1 on error.
    """
    from phoson_agent import AgentEngine
    from phoson_cli.repl import close_plugins, build_mcp_plugins, build_system_prompt
    from phoson_cli.tools import build_tools, build_tools_dict
    from phoson_llm.schemas import Message, ModelConfig

    chat = build_chat(config)
    engine: AgentEngine | None = None
    try:
        tools = build_tools()
        engine = AgentEngine(
            chat=chat,
            tools=tools,
            plugins=build_mcp_plugins(config),
            max_iterations=config.max_iterations,
        )
        # Same sub-agent runtime context as the interactive REPL.
        engine.context.extra["safe_mode"] = config.safe_mode
        engine.context.extra["available_tools"] = build_tools_dict()
        engine.context.extra["default_model"] = config.subagent_model or config.model
        engine.context.extra["main_model"] = config.model
        engine.context.extra["max_iterations"] = config.max_iterations
        engine.context.extra["subagent_max_parallel"] = config.subagent_max_parallel
        engine.context.extra["subagent_timeout_seconds"] = (
            config.subagent_timeout_seconds
        )
        engine.context.extra["chat"] = chat

        result = await engine.run(
            [Message(role="user", content=task)],
            ModelConfig(
                model=config.model,
                system=build_system_prompt(engine.tools),
            ),
        )
        print(result.final_content)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            await close_plugins(list(getattr(engine, "_loaded_plugins", [])))
        aclose = getattr(chat, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    """Run the Phoson CLI: interactive REPL, one-shot task, or setup."""
    args = sys.argv[1:]

    if "--self-update" in args:
        self_update()
        return

    if "--uninstall" in args:
        uninstall()
        return

    if any(arg in {"--install", "--setup"} for arg in args):
        config = load_config()
        asyncio.run(run_install_wizard(config))
        return

    # One-shot mode: phoson-cli "task" | -p "task" | piped stdin.
    # Skips the interactive wizard — missing credentials surface as the
    # friendly pre-check error below, which is what scripts need.
    oneshot_task = _parse_oneshot_task(args)
    if oneshot_task is not None:
        config = load_config()
        try:
            build_chat(config)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print(
                "Set the provider's API key in ~/.phoson/config.toml "
                "or run: phoson-cli --setup",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.exit(asyncio.run(_run_oneshot(config, oneshot_task)))

    config = load_config()

    config_path = Path.home() / ".phoson" / "config.toml"

    if not config_path.exists() and not has_configured_provider(config):
        print("No API keys configured. Running setup wizard...")
        asyncio.run(run_install_wizard(config))
        # Reload config after setup
        config = load_config()

    # Fail fast with a friendly message instead of a traceback when the
    # active provider has no usable credential (e.g. stale config.toml).
    try:
        build_chat(config)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(
            "Set the provider's API key in ~/.phoson/config.toml "
            "or run: phoson-cli --setup",
            file=sys.stderr,
        )
        sys.exit(1)

    app = PhosonApp(config)
    asyncio.run(app.run_async())


if __name__ == "__main__":
    main()
