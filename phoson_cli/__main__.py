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
  --theme <tier>       Override the theme: system, dark, light, ansi, no-color
  --max-turns <n>      Override max_iterations for this run
  --classic            Use the classic line-by-line REPL
  --no-fullscreen      Alias for --classic
  --setup              Run the setup wizard
  --self-update        Check for and install CLI updates
  --uninstall          Uninstall phoson-cli
  --install-plugin <source>  Install and enable a community plugin (alias)
  -y, --yes            Skip the install confirmation (plugin install only)
  plugin <command>     Manage plugins: install, list, enable, disable,
                       remove, update, doctor
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
    plugin_args: list[str] | None = None
    assume_yes: bool = False


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
        if arg == "plugin":
            plugin_args = argv[i + 1 :]
            if "--yes" in plugin_args or "-y" in plugin_args:
                options.assume_yes = True
                plugin_args = [
                    value for value in plugin_args if value not in {"--yes", "-y"}
                ]
            options.plugin_args = plugin_args
            break
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
        elif arg in {"-y", "--yes"}:
            options.assume_yes = True
        elif arg == "--install-plugin":
            options.plugin_args = ["install", _take_value(argv, i, arg)]
            i += 1
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

    # Plugin management owns stdin itself (notably install confirmation).
    # Do not consume a piped "y" as a one-shot agent task before the
    # subcommand has a chance to read it.
    if options.plugin_args is not None:
        return options

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

    Since T-8 the default tier is ``system`` — it inherits the terminal's
    own colors, so there is nothing to suggest or ask. Kept as a no-op
    for the flag/env plumbing; a persisted ``dark``/``light`` choice from
    before T-8 still applies as-is.
    """
    if options.theme is not None:
        return
    from phoson_cli.config import has_persisted_theme

    if has_persisted_theme():
        return
    # T-8: the system tier resolves light/dark in the terminal itself.
    return


def _run_plugin_command(
    args: list[str], config: PhosonConfig, *, assume_yes: bool = False
) -> None:
    """Run a non-interactive community-plugin management command."""
    from phoson_cli.plugin_manager import (
        PluginManagerError,
        doctor_plugin,
        enable_plugin,
        remove_plugin,
        update_plugin,
        disable_plugin,
        install_plugin,
        configured_plugins,
    )

    if not args:
        _fail(
            "plugin requires one of: install, list, enable, disable, "
            "remove, update, doctor"
        )
    command, *rest = args
    if command == "list" and not rest:
        entries = configured_plugins(config)
        if not entries:
            print("No community plugins configured.")
        for entry in entries:
            print(f"enabled  {entry.name}")
        return
    if command == "install" and len(rest) == 1:
        source = rest[0]
        print(f"Installing plugin from {source!r}. Plugins execute Python code as you.")
        if not assume_yes:
            try:
                answer = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("Cancelled.")
                return
            if answer not in {"y", "yes"}:
                print("Cancelled.")
                return
        try:
            name = install_plugin(source, config)
        except PluginManagerError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return
        print(f"Installed and enabled plugin: {name}")
        return
    if (
        command in {"enable", "disable", "remove", "update", "doctor"}
        and len(rest) == 1
    ):
        plugin_id = rest[0]
        try:
            if command == "enable":
                enable_plugin(plugin_id, config)
                print(f"Enabled plugin: {plugin_id}")
            elif command == "disable":
                disable_plugin(plugin_id, config)
                print(f"Disabled plugin: {plugin_id}")
            elif command == "remove":
                remove_plugin(plugin_id, config)
                print(f"Removed plugin from configuration: {plugin_id}")
            elif command == "update":
                update_plugin(plugin_id, config)
                print(f"Updated plugin: {plugin_id}")
            else:
                plugin = doctor_plugin(plugin_id, config)
                print(f"Plugin OK: {plugin.name} {plugin.version}")
                plugin.cleanup()
        except PluginManagerError as exc:
            print(f"Error: {exc}", file=sys.stderr)
        return
    _fail(f"invalid plugin command: {' '.join(args)}")


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
    Returns 0 on success, 1 on error, 124 when the run hits its
    wall-clock budget (``PHOSON_RUN_BUDGET_SECONDS``; #141).

    The one-shot engine carries the **same middleware chain** as the
    interactive REPL (#174/F-02): Offload → Summarizer → Permission.
    Without it the permissions policy, safe_mode and auto-compaction would
    silently not apply to one-shot runs. Because one-shot is
    non-interactive there is no confirmation callback, so an ``ask``-level
    tool fails closed (refused) rather than hanging.
    """
    import asyncio

    from phoson_agent import AgentEngine
    from phoson_cli.repl import close_plugins, build_plugin_specs, build_system_prompt
    from phoson_cli.theme import load_theme
    from phoson_cli.tools import build_tools, build_tools_dict
    from phoson_llm.schemas import Message, ModelConfig
    from phoson_cli.plugin_ui import NonInteractivePluginUiService
    from phoson_cli.session_utils import (
        build_offload,
        build_summarizer,
        build_middlewares,
    )
    from phoson_cli.permissions_store import build_permission_middleware

    chat = build_chat(config)
    engine: AgentEngine | None = None
    try:
        tools = build_tools()
        # Same middleware chain as the REPL. One-shot has no confirmation
        # service, so the permission gate fails closed for ``ask`` tools.
        offload = build_offload(config)
        summarizer = build_summarizer(config)
        permission = build_permission_middleware(on_ask=None)
        middlewares = build_middlewares(
            config=config,
            offload=offload,
            summarizer=summarizer,
            permission=permission,
        )
        engine = AgentEngine(
            chat=chat,
            tools=tools,
            middlewares=middlewares,
            plugins=build_plugin_specs(config),
            max_iterations=config.max_iterations,
        )
        # Same sub-agent runtime context as the interactive REPL.
        engine.context.extra["safe_mode"] = config.safe_mode
        engine.context.extra["middlewares"] = middlewares
        engine.context.extra["plugin_ui"] = NonInteractivePluginUiService(
            load_theme(config.theme)
        )
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

        # Wall-clock budget for the whole run (#141): one-shot has no Esc,
        # so a hung tool cannot be escaped interactively. ``0`` disables
        # the budget (unlimited). The teardown in ``finally`` closes
        # plugins and the chat client on the budget path too.
        run_task = asyncio.ensure_future(
            engine.run(
                [Message(role="user", content=task)],
                ModelConfig(
                    model=config.model,
                    system=build_system_prompt(engine.tools),
                ),
            )
        )
        budget = config.run_budget_seconds
        if budget and budget > 0:
            try:
                result = await asyncio.wait_for(run_task, timeout=budget)
            except TimeoutError:
                # wait_for has already cancelled the task and reaped it;
                # the teardown in ``finally`` still closes plugins + chat.
                print(
                    f"Error: run exceeded the {budget:g}s wall-clock budget "
                    f"(PHOSON_RUN_BUDGET_SECONDS). "
                    "Set it to 0 to disable the budget.",
                    file=sys.stderr,
                )
                return 124
        else:
            result = await run_task
        # Print an empty string (not ``None``) when there is no content.
        print(result.final_content or "")
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

    if options.plugin_args is not None:
        try:
            config = load_config()
        except PhosonConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        _run_plugin_command(options.plugin_args, config, assume_yes=options.assume_yes)
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
