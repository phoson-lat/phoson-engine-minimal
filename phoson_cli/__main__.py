"""Entry point for the Phoson CLI application.

Argument parsing is centralized in :func:`parse_args` — a pure function
over an argv list returning a :class:`CliOptions` dataclass — so every
flag is unit-testable without spawning a process (IMPROVEMENTS.md D5).
Manual parsing is deliberate: typer/click would add a dependency against
the "minimal" philosophy.
"""

import os
import sys
import shutil
import asyncio
import subprocess
from pathlib import Path
from dataclasses import dataclass

from phoson_cli import warnings_hook
from phoson_cli.repl import PhosonRepl
from phoson_cli.config import (
    PhosonConfig,
    PhosonConfigError,
    PhosonKeyBindingsError,
    build_chat,
    load_config,
    save_config,
    has_configured_provider,
)
from phoson_cli.updater import get_current_version, perform_self_update
from phoson_cli.installer import run_install_wizard
from phoson_cli.fullscreen.app import PhosonApp

_USAGE = """\
phoson-cli [options] [task]

Interactive agent CLI. The default interactive front end is the
full-screen TUI; use --classic for the line-by-line REPL.

One-shot mode (no REPL, no session — for scripts and CI):
  phoson-cli "fix the failing tests"      # positional task
  phoson-cli -p "summarize this repo"     # --print flag
  echo "explain the CI failure" | phoson-cli   # piped stdin

Options:
  -p, --print          Print the final answer and exit (one-shot mode)
  --version            Show the version and exit
  --model <id>         Override the model for this run
  --provider <id>      Override the provider for this run
  --theme <tier>       Override the theme: dark, light, ansi, no-color
  --max-turns <n>      Override max_iterations for this run
  --classic            Use the classic line-by-line REPL
  --no-fullscreen      Alias for --classic
  --setup              Run the setup wizard
  --self-update        Check for and install CLI updates
  --uninstall          Uninstall phoson-cli
  -h, --help           Show this help and exit
"""


@dataclass
class CliOptions:
    """Parsed CLI arguments (IMPROVEMENTS.md D5)."""

    version: bool = False
    self_update: bool = False
    uninstall: bool = False
    setup: bool = False
    classic: bool = False
    print_mode: bool = False
    model: str | None = None
    provider: str | None = None
    theme: str | None = None
    max_turns: int | None = None
    task: str | None = None


def _fail(message: str) -> None:
    """Print a usage error and exit 2 (argparse-compatible behavior)."""
    print(f"phoson-cli: {message}", file=sys.stderr)
    sys.exit(2)


def _take_value(argv: list[str], i: int, flag: str) -> str:
    """Return the value following ``flag`` at position ``i`` (or fail)."""
    if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
        _fail(f"option {flag} requires a value")
    return argv[i + 1]


def _parse_max_turns(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        _fail(f"option --max-turns expects a positive integer, got {value!r}")
    if n <= 0:
        _fail(f"option --max-turns expects a positive integer, got {value!r}")
    return n


def parse_args(argv: list[str]) -> CliOptions:
    """Parse *argv* (without the program name) into :class:`CliOptions`.

    Pure with respect to argv: the only side effect is reading piped
    stdin (``echo "task" | phoson-cli``), which is inherent to one-shot
    mode. Unknown flags, missing values and bad numbers exit 2 with a
    message (argparse-compatible behavior).
    """
    options = CliOptions()
    task_parts: list[str] = []

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in {"-h", "--help"}:
            print(_USAGE)
            sys.exit(0)
        elif arg == "--version":
            options.version = True
        elif arg == "--self-update":
            options.self_update = True
        elif arg == "--uninstall":
            options.uninstall = True
        elif arg in {"--install", "--setup"}:
            options.setup = True
        elif arg in {"--classic", "--no-fullscreen"}:
            options.classic = True
        elif arg in {"-p", "--print"}:
            options.print_mode = True
        elif arg in {"--model", "--provider", "--theme", "--max-turns"}:
            value = _take_value(argv, i, arg)
            i += 1
            if arg == "--model":
                options.model = value
            elif arg == "--provider":
                options.provider = value
            elif arg == "--theme":
                options.theme = value
            else:
                options.max_turns = _parse_max_turns(value)
        elif arg.startswith("-") and arg != "-":
            _fail(f"unknown option: {arg}")
        else:
            task_parts.append(arg)
        i += 1

    task = " ".join(task_parts) if task_parts else None
    if task is None:
        if not sys.stdin.isatty():
            task = _read_stdin_task() or None
            if options.print_mode and task is None:
                print("Error: -p/--print received empty stdin.", file=sys.stderr)
                sys.exit(1)
        elif options.print_mode:
            print(
                "Error: -p/--print needs a task argument or piped stdin.",
                file=sys.stderr,
            )
            sys.exit(1)

    options.task = task
    return options


def _apply_overrides(config: PhosonConfig, options: CliOptions) -> None:
    """Apply the one-off CLI overrides on top of the loaded config (D5).

    Precedence: flag > config.toml > env > default. The config object is
    mutated in place (it is a dataclass, not persisted back to disk).
    """
    if options.provider:
        config.provider = options.provider
    if options.model:
        config.model = options.model
    if options.theme:
        config.theme = options.theme
    if options.max_turns is not None:
        config.max_iterations = options.max_turns


def _should_use_classic(options: CliOptions) -> bool:
    """Whether the interactive session runs the classic REPL (D2/D5).

    Explicit ``--classic``/``--no-fullscreen`` always wins. Otherwise the
    classic front end is selected as a degraded mode when the terminal
    cannot do full-screen (``TERM`` unset or ``dumb``) — the full-screen
    ``Application`` needs a real TTY with cursor/alternate-screen
    capabilities. The auto-detection only applies to genuinely
    interactive terminals (``sys.stdin.isatty()``); piped/redirected
    stdin is one-shot mode, which is resolved before front-end selection.
    """
    if options.classic:
        return True
    return sys.stdin.isatty() and os.environ.get("TERM", "") in {"", "dumb"}


def _maybe_offer_theme_suggestion(config: PhosonConfig, options: CliOptions) -> None:
    """First-run light/dark theme suggestion (IMPROVEMENTS.md E4).

    Only when the user has never set a theme (no ``PHOSON_THEME`` env, no
    ``theme`` in config.toml) and no ``--theme`` flag was passed for this
    run. Detection is a bounded ~150 ms OSC 11 probe after the
    COLORFGBG check; any terminal that cannot be classified simply skips
    the question. The answer is persisted, so this fires at most once.
    Runs before the front end is built, so the new theme applies
    immediately (banner included).
    """
    if options.theme is not None:
        return
    from phoson_cli.theme import suggest_theme
    from phoson_cli.config import has_persisted_theme
    from phoson_cli.terminal_theme import detect_terminal_theme

    if has_persisted_theme():
        return
    suggested = suggest_theme(
        detected_light=detect_terminal_theme(),
        has_persisted=False,
    )
    if suggested is None:
        return
    print(
        f"\nYour terminal looks like a {suggested} background.",
        file=sys.stderr,
    )
    try:
        answer = (
            input("Save the " + suggested + " theme as your default? [Y/n] ")
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return
    if answer in {"n", "no"}:
        return
    config.theme = suggested
    save_config(config, only_fields={"theme"})
    print(f"Theme saved → {suggested}", file=sys.stderr)


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
        # Note: no ``on_subagent_progress`` callback here — one-shot mode
        # has no live panel to feed (E2), and the final per-task metrics
        # still arrive in the tool output the model receives.

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
    """Run the Phoson CLI: interactive REPL, one-shot task, or setup.

    Installs the I-112 warnings hook for the whole run: internal soft-fail
    warnings (context-window / model-listing fallbacks, invalid config values)
    surface once, as a styled notice — never as a raw Python ``UserWarning``
    with file + line on stderr. ``restore()`` runs in a ``finally`` so even the
    ``sys.exit`` paths unwind it (``SystemExit`` fires ``finally``).
    """
    restore = warnings_hook.install()
    try:
        _run_cli()
    finally:
        restore()


def _run_cli() -> None:
    """The actual CLI body (pre-I-112 ``main``) — see :func:`main` wrapper."""
    options = parse_args(sys.argv[1:])

    if options.version:
        print(f"phoson-cli {get_current_version()}")
        return

    if options.self_update:
        self_update()
        return

    if options.uninstall:
        uninstall()
        return

    if options.setup:
        try:
            config = load_config()
        except PhosonConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        asyncio.run(run_install_wizard(config))
        return

    try:
        config = load_config()
    except PhosonConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _apply_overrides(config, options)

    # One-shot mode: phoson-cli "task" | -p "task" | piped stdin.
    # Skips the interactive wizard — missing credentials surface as the
    # friendly pre-check error below, which is what scripts need.
    if options.task is not None:
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
        sys.exit(asyncio.run(_run_oneshot(config, options.task)))

    config_path = Path.home() / ".phoson" / "config.toml"

    if not config_path.exists() and not has_configured_provider(config):
        print("No API keys configured. Running setup wizard...")
        asyncio.run(run_install_wizard(config))
        # Reload config after setup and re-apply the CLI overrides on top.
        config = load_config()
        _apply_overrides(config, options)

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

    # First-run theme suggestion (E4) — before either front end is built,
    # so a confirmed theme colors the banner on this very startup.
    _maybe_offer_theme_suggestion(config, options)

    if _should_use_classic(options):
        if not options.classic:
            print(
                "Full-screen UI unavailable (TERM=dumb) — using the classic REPL.",
                file=sys.stderr,
            )
        repl = PhosonRepl(config)
        # I-112: point the warnings hook's printer at the themed renderer so
        # notices match the front end's style (live theme; /theme re-points it).
        # getattr keeps fakes without a renderer (tests) on the plain default.
        renderer = getattr(repl, "renderer", None)
        if renderer is not None:
            warnings_hook.notice_printer = renderer.print_warn
        try:
            asyncio.run(repl.run())
        finally:
            warnings_hook.reset_notice_printer()
        return

    try:
        app = PhosonApp(config)
    except PhosonKeyBindingsError as exc:
        # A [keys] section that survived load-time validation but still
        # collides (e.g. two actions remapped onto one sequence): fail
        # with the same friendly message as every other config error.
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(app.run_async())


if __name__ == "__main__":
    main()
