"""Entry point for the Phoson CLI application."""

import os
import sys
import shutil
import asyncio
import subprocess
from pathlib import Path

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import (
    PhosonConfig,
    build_chat,
    load_config,
    has_configured_provider,
)
from phoson_cli.updater import perform_self_update
from phoson_cli.installer import run_install_wizard


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


#: UI mode flags. They never reach the one-shot task parser.
UI_MODE_FLAGS = {"--textual", "--classic"}


def _resolve_ui_mode(args: list[str]) -> tuple[str, list[str]]:
    """Split the UI mode flag out of ``args``.

    Returns ``(mode, remaining_args)`` where mode is ``"textual"`` or
    ``"classic"`` (the default). ``--classic`` is accepted for explicitness
    and future compatibility; the classic REPL is the default.
    """
    mode = "classic"
    rest: list[str] = []
    for arg in args:
        if arg in UI_MODE_FLAGS:
            if arg == "--textual":
                mode = "textual"
        else:
            rest.append(arg)
    return mode, rest


def _textual_available() -> bool:
    """True when the optional ``textual`` dependency is importable."""
    import importlib.util

    try:
        return importlib.util.find_spec("textual") is not None
    except ModuleNotFoundError:
        return False


def _apply_textual_key_env() -> None:
    """Map PHOSON_TEXTUAL_LEGACY_KEYS onto Textual's kitty-key opt-out.

    Some terminal emulators misbehave with the Kitty keyboard protocol;
    the legacy xterm sequences are a solid fallback. Must run before
    the first ``import textual`` (Textual reads the env var at import).
    """
    if os.environ.get("PHOSON_TEXTUAL_LEGACY_KEYS", "").strip() not in ("", "0"):
        os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"


def _workaround_kitty_associated_text() -> None:
    """Disable Kitty's "associated text" key reporting for the TUI.

    Textual 8.2.8's XTermParser does not understand the ``u;<codepoint>``
    suffix that Kitty appends to every key when the associated-text flag
    is enabled, so on Kitty each typed key becomes ``key + ';<digits>'``
    garbage in the composer (``/help`` is untypeable, shortcuts look
    dead). The disambiguate and report-all-keys flags — the ones that
    make the Ctrl combos work — are kept. Must run after ``import
    textual.drivers.linux_driver`` (the driver reads these globals when
    it starts the input thread) and before ``App.run()``.
    """
    try:
        from textual.drivers import linux_driver
    except ModuleNotFoundError:  # pragma: no cover - non-Linux platform
        return
    try:
        linux_driver.KITTY_REPORT_ASSOCIATED_TEXT = 0  # type: ignore[misc]
    except (AttributeError, TypeError):  # pragma: no cover - defensive
        pass  # driver layout changed: leave the flag as Textual sets it


def _start_textual_ui(config: "PhosonConfig") -> bool:
    """Launch the Textual TUI. Returns True if it took over.

    Phase 3 of the Textual migration (MIGRATE_CLI_TO_TEXTUAL.md): the
    TUI is a second front end over the same SessionController. The
    classic REPL remains the default (``--classic`` or no flag).
    """
    if not _textual_available():
        print("Error: the Textual TUI requires the optional 'tui' extra.")
        print(
            "Install it with:  uv sync --extra tui"
            "   (or: pip install 'phoson-engine-minimal[tui]')"
        )
        sys.exit(1)
    _apply_textual_key_env()
    _workaround_kitty_associated_text()
    from phoson_cli.textual import PhosonTextualApp

    app = PhosonTextualApp(config)
    app.run()  # blocks until the user quits
    app.shutdown()  # close the chat client and plugins
    return True


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

    # UI mode selection happens before one-shot parsing so the flags never
    # leak into the task text.
    ui_mode, args = _resolve_ui_mode(args)

    # One-shot mode: phoson-cli "task" | -p "task" | piped stdin.
    # Skips the interactive wizard — missing credentials surface as the
    # friendly pre-check error below, which is what scripts need.
    # (One-shot is always stdout-only; --textual/--classic are ignored.)
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

    if ui_mode == "textual" and _start_textual_ui(config):
        return

    repl = PhosonRepl(config)
    asyncio.run(repl.run())


if __name__ == "__main__":
    main()
