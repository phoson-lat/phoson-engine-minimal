"""Entry point for the Phoson CLI application.

Argument parsing is centralized in :func:`parse_args` — a pure function
over an argv list returning a :class:`CliOptions` dataclass — so every
flag is unit-testable without spawning a process (IMPROVEMENTS.md D5).
Manual parsing is deliberate: typer/click would add a dependency against
the "minimal" philosophy.
"""

import sys
import shutil
import asyncio
import subprocess
from typing import NoReturn
from pathlib import Path
from dataclasses import dataclass

from phoson_cli import warnings_hook
from phoson_cli.repl import PhosonRepl
from phoson_cli.trace import TraceWriter, trace_enabled
from phoson_cli.config import (
    PhosonConfig,
    PhosonConfigError,
    PhosonKeyBindingsError,
    build_chat,
    load_config,
    has_configured_provider,
)
from phoson_cli.updater import get_current_version, perform_self_update
from phoson_cli.terminal import stream_is_tty, cursor_output_capable
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
  --session <id>       Resume a saved session by id (prefix match works)
  --resume <id>        Alias for --session
  --trace              One-shot only: emit a JSON line per agent event
                       (tool calls, steps, final/error) to stderr
  --classic            Use the classic line-by-line REPL
  --no-fullscreen      Alias for --classic
  --setup              Run the setup wizard
  --self-update        Check for and install CLI updates
  --uninstall          Uninstall phoson-cli
  --install-plugin <source>  Install and enable a community plugin (alias)
  -y, --yes            Skip the install confirmation (plugin install only)
  plugin <command>     Manage plugins: install, list, enable, disable,
                       remove, update, doctor
  bg list              List saved sessions with run status (read-only)
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
    session: str | None = None
    trace: bool = False
    task: str | None = None
    plugin_args: list[str] | None = None
    bg_args: list[str] | None = None
    assume_yes: bool = False


def _fail(message: str) -> NoReturn:
    """Print a usage error and exit 2 (argparse-compatible behavior)."""
    if "--trace" in sys.argv[1:] or trace_enabled():
        TraceWriter().error(message, code="usage_error")
    else:
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

    Stdin is deliberately resolved later, after immediate actions have
    dispatched. Unknown flags, missing values and bad numbers exit 2 with
    a message (argparse-compatible behavior).
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
        if arg == "bg":
            options.bg_args = argv[i + 1 :]
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
        elif arg == "--trace":
            options.trace = True
        elif arg in {
            "--model",
            "--provider",
            "--theme",
            "--max-turns",
            "--session",
            "--resume",
        }:
            value = _take_value(argv, i, arg)
            i += 1
            if arg == "--model":
                options.model = value
            elif arg == "--provider":
                options.provider = value
            elif arg == "--theme":
                options.theme = value.strip().lower()
                if not options.theme:
                    _fail("option --theme requires a non-empty value")
            elif arg in {"--session", "--resume"}:
                options.session = value.strip()
                if not options.session:
                    _fail(f"option {arg} requires a non-empty id")
            else:
                options.max_turns = _parse_max_turns(value)
        elif arg.startswith("-") and arg != "-":
            _fail(f"unknown option: {arg}")
        else:
            task_parts.append(arg)
        i += 1

    options.task = " ".join(task_parts) if task_parts else None
    return options


def _resolve_task(options: CliOptions, trace_writer: TraceWriter | None = None) -> None:
    """Resolve piped input only after non-agent actions have returned."""

    def fail(message: str) -> NoReturn:
        if trace_writer is not None:
            trace_writer.error(message, code="input_error")
        else:
            print(f"Error: {message}", file=sys.stderr)
        sys.exit(1)

    if options.task is not None:
        return
    if stream_is_tty(sys.stdin):
        if options.print_mode:
            fail("-p/--print needs a task argument or piped stdin.")
        return
    try:
        reader = getattr(sys.stdin, "read", None)
        if not callable(reader):
            raise AttributeError("stdin has no readable stream")
        raw = reader()
        if not isinstance(raw, str):
            raise TypeError("stdin did not return text")
        task = raw.strip()
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        fail(f"could not read stdin: {exc}")
    if not task:
        if options.print_mode:
            fail("-p/--print received empty stdin.")
        else:
            fail(
                "stdin was empty; provide a task argument or run from an "
                "interactive terminal."
            )
    options.task = task


def _require_interactive_stdin(action: str) -> None:
    """Reject prompt-driven actions before they can consume redirected stdin."""
    if stream_is_tty(sys.stdin):
        return
    print(
        f"Error: {action} requires an interactive terminal; re-run it from a TTY.",
        file=sys.stderr,
    )
    sys.exit(1)


def _apply_overrides(config: PhosonConfig, options: CliOptions) -> None:
    """Apply the one-off CLI overrides on top of the loaded config (D5).

    Precedence: flag > environment > config.toml > default. The config object is
    mutated in place (it is a dataclass, not persisted back to disk).
    """
    if options.provider:
        config.provider = options.provider
        config._provider_source = "cli"
    if options.model:
        config.model = options.model
    if options.theme:
        config.theme = options.theme
        config.cli_theme = options.theme
    if options.max_turns is not None:
        config.max_iterations = options.max_turns


class _CliThemeError(ValueError):
    pass


async def _resume_session(repl, query: str) -> bool:
    """Resolve *query* (session-id prefix) and load that session.

    Mirrors ``/resume``: prefix match, ambiguity reported. Returns True when a
    session was loaded; on failure prints a friendly error to stderr.
    """
    sessions = await repl.storage.list_meta(cwd=str(Path.cwd()))
    matches = [s for s in sessions if str(s.id).startswith(query)]
    if not matches:
        print(
            f"Error: no session matching {query!r}. "
            "Resume interactively and run /sessions to list them.",
            file=sys.stderr,
        )
        return False
    if len(matches) > 1:
        print(
            f"Error: {len(matches)} sessions match {query!r}; be more specific:",
            file=sys.stderr,
        )
        for session in matches[:10]:
            title = getattr(session, "title", None) or "(untitled)"
            print(f"  {str(session.id)[:8]}  [{title}]", file=sys.stderr)
        return False
    session_id = str(matches[0].id)
    ok = await repl.load_session(session_id)
    if not ok:
        print(f"Error: could not load session {session_id[:8]}.", file=sys.stderr)
        return False
    return True


def _print_resume_hint(repl) -> None:
    """Print the command that resumes this session (interactive exit only).

    Silent when nothing was ever sent (no session was created) — matching the
    lazy-session behaviour — and never shown in one-shot mode, which returns
    before this point.
    """
    if repl is None:
        return
    controller = getattr(repl, "_controller", None)
    if controller is None or not getattr(controller, "session_started", False):
        return
    session_id = (getattr(repl.tree, "session_id", "") or "").strip()
    if not session_id:
        return
    print(f"\nTo resume run: phoson-cli --session {session_id[:8]}")


async def _run_classic(repl, session: str | None) -> bool:
    """Resume *session* (when given) then run the classic REPL, one event loop.

    Loading and running must share a loop: the controller holds asyncio
    primitives that would otherwise bind to a throwaway loop and fail on the
    real one. Returns False when the requested session could not be loaded.
    """
    if session and not await _resume_session(repl, session):
        await repl.shutdown()
        return False
    await repl.run()
    return True


async def _run_fullscreen(app, session: str | None) -> bool:
    """Resume *session* (when given) then run the full-screen app, one loop."""
    if session and not await _resume_session(app.repl, session):
        await app.repl.shutdown()
        return False
    await app.run_async()
    return True


def _prepare_cli_theme(config: PhosonConfig):
    """Classify a CLI theme using built-in and JSON themes only."""
    from phoson_cli.theme import load_theme, default_theme_registry

    registry = default_theme_registry()
    requested = config.cli_theme or ""
    theme = registry.get(requested)
    deferred = theme is None
    if deferred:
        # Keep constructors quiet until the normal runtime plugin load can
        # decide whether the name is plugin-contributed.
        theme = load_theme(None, registry=registry)
    else:
        theme = load_theme(config.theme, registry=registry, cli_value=requested)
    setattr(config, "_startup_theme_registry", registry)
    setattr(config, "_startup_theme", theme)
    setattr(config, "_cli_theme_deferred", deferred)
    return registry


def _validate_runtime_cli_theme(config: PhosonConfig, registry):
    """Validate a deferred CLI theme against one runtime's loaded plugins."""
    from phoson_cli.theme import resolve_runtime_theme

    requested = config.cli_theme
    if requested and registry.get(requested) is None:
        raise _CliThemeError(
            "option --theme expects one of "
            f"{', '.join(registry.valid_names())}, got {requested!r}"
        )
    return resolve_runtime_theme(config, registry)


def _should_use_classic(options: CliOptions) -> bool:
    """Whether the interactive session runs the classic REPL (D2/D5).

    Explicit ``--classic``/``--no-fullscreen`` always wins. Otherwise the
    classic front end is selected as a degraded mode when the terminal
    cannot do full-screen (``TERM`` unset or ``dumb``) — the full-screen
    ``Application`` needs a real TTY with cursor/alternate-screen
    capabilities. Both stdin and stdout must be TTYs; piped stdin is resolved
    as one-shot input before front-end selection, while redirected stdout
    degrades to the line-oriented frontend.
    """
    if options.classic:
        return True
    return not (stream_is_tty(sys.stdin) and cursor_output_capable(sys.stdout))


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
) -> int:
    """Run a non-interactive community-plugin management command."""
    from phoson_cli.plugin_manager import (
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
            status = "enabled" if entry.enabled else "disabled"
            print(f"{status:8} {entry.name}")
        return 0
    if command == "install" and len(rest) == 1:
        source = rest[0]
        print(f"Installing plugin from {source!r}. Plugins execute Python code as you.")
        if not assume_yes:
            if not stream_is_tty(sys.stdin):
                print("Cancelled. Re-run with --yes to install non-interactively.")
                return 0
            try:
                answer = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("Cancelled.")
                return 0
            if answer not in {"y", "yes"}:
                print("Cancelled.")
                return 0
        try:
            name = install_plugin(source, config)
        except Exception as exc:  # noqa: BLE001 - operational CLI boundary
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Installed and enabled plugin: {name}")
        return 0
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
        except Exception as exc:  # noqa: BLE001 - operational CLI boundary
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0
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


async def _run_oneshot(config: PhosonConfig, task: str, trace: bool = False) -> int:
    """Run a single agent task and print the final content to stdout.

    No REPL, no session persistence — intended for scripts and CI.
    Returns 0 on success, 1 on error, 124 when the run hits its
    wall-clock budget (``PHOSON_RUN_BUDGET_SECONDS``; #141).

    When ``trace`` is set (``--trace`` / ``PHOSON_TRACE=1``) a structured
    JSON trace of the run's agent events (tool calls, steps, final/error)
    is written to stderr, so the answer on stdout stays byte-clean for
    callers while the run itself becomes observable (#139).

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
    from phoson_cli.theme import load_theme, build_theme_registry
    from phoson_cli.tools import build_tools, build_tools_dict
    from phoson_cli.trace import TraceMiddleware
    from phoson_llm.schemas import Message, ModelConfig
    from phoson_cli.plugin_ui import NonInteractivePluginUiService
    from phoson_cli.session_utils import (
        build_offload,
        build_summarizer,
        build_middlewares,
        engine_prompt_tools,
    )
    from phoson_cli.guard_classifier import build_permission_classifier
    from phoson_cli.permissions_store import (
        apply_tool_hints,
        build_permission_middleware,
    )

    tracing = trace or trace_enabled()
    trace_writer = TraceWriter() if tracing else None
    previous_notice_printer = warnings_hook.notice_printer
    warnings_hook.notice_printer = lambda message: (
        trace_writer.diagnostic(message, source="warning")
        if trace_writer is not None
        else print(message, file=sys.stderr)
    )

    chat = None
    engine: AgentEngine | None = None
    trace_middleware: TraceMiddleware | None = None
    terminal: tuple[str, str, str, bool] | None = None
    try:
        if config.cli_theme and not hasattr(config, "_startup_theme_registry"):
            _prepare_cli_theme(config)
        chat = build_chat(config)
        tools = build_tools()
        # Same middleware chain as the REPL. One-shot has no confirmation
        # service, so the permission gate fails closed for ``ask`` tools.
        offload = build_offload(config)
        summarizer = build_summarizer(config)
        # Internal summary call must not carry the run's tool schemas
        # (F-11 / #176): route it through the tool-free chat client.
        summarizer.chat = chat
        permission = build_permission_middleware(
            on_ask=None,
            classifier=build_permission_classifier(config, lambda: chat),
            classifier_auto_allow=config.permission_classifier_auto_allow,
            classifier_timeout_s=config.permission_classifier_timeout_s,
        )
        middlewares = build_middlewares(
            config=config,
            offload=offload,
            summarizer=summarizer,
            permission=permission,
        )
        # #139: optional structured run trace for headless one-shot. Appended
        # last (it only implements on_agent_event, so chain order is inert).
        if tracing:
            trace_middleware = TraceMiddleware(writer=trace_writer)
            middlewares.append(trace_middleware)
        engine = AgentEngine(
            chat=chat,
            tools=tools,
            middlewares=middlewares,
            plugins=build_plugin_specs(config),
            max_iterations=config.max_iterations,
            tool_budget_tokens=config.tool_budget_tokens or None,
        )
        # #144 phase 2: fold MCP tool annotations into the permission policy.
        # One-shot has no confirmation callback, so an annotated MCP tool that
        # is not read-only resolves to ask → refused (fail closed).
        apply_tool_hints(permission.policy, engine.tools)
        theme_registry = build_theme_registry(
            list(getattr(engine, "_loaded_plugins", []))
        )
        try:
            active_theme = (
                _validate_runtime_cli_theme(config, theme_registry)
                if config.cli_theme
                else load_theme(config.theme, registry=theme_registry)
            )
        except _CliThemeError as exc:
            if trace_writer is not None:
                terminal = ("error", str(exc), "usage_error", False)
            else:
                print(f"phoson-cli: {exc}", file=sys.stderr)
            return 2
        # #227 phase 3: forward every permission decision to exporters (the
        # OTel plugin, when enabled) now that the plugins are loaded.
        from phoson_cli.session_utils import record_permission_decision

        permission.on_decision = lambda decision: record_permission_decision(
            getattr(engine, "_loaded_plugins", []), decision
        )
        # Same sub-agent runtime context as the interactive REPL.
        engine.context.extra["safe_mode"] = config.safe_mode
        engine.context.extra["middlewares"] = middlewares
        engine.context.extra["plugin_ui"] = NonInteractivePluginUiService(
            active_theme,
            trace_writer=trace_writer,
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
                    system=build_system_prompt(
                        engine_prompt_tools(engine),
                    ),
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
                message = (
                    f"run exceeded the {budget:g}s wall-clock budget "
                    "(PHOSON_RUN_BUDGET_SECONDS). Set it to 0 to disable "
                    "the budget."
                )
                if trace_writer is not None:
                    terminal = ("error", message, "timeout", False)
                else:
                    print(f"Error: {message}", file=sys.stderr)
                return 124
        else:
            result = await run_task
        # Print an empty string (not ``None``) when there is no content.
        sys.stdout.write((result.final_content or "").rstrip("\n") + "\n")
        sys.stdout.flush()
        if trace_writer is not None:
            terminal = ("done", result.final_content or "", "", False)
        return 0
    except asyncio.CancelledError:
        if trace_writer is not None:
            terminal = ("error", "Run cancelled.", "cancelled", False)
        raise
    except Exception as exc:
        if trace_writer is not None:
            event = trace_middleware.terminal_error if trace_middleware else None
            terminal = (
                "error",
                event.message if event is not None else str(exc),
                (event.code or "agent_error") if event is not None else "runtime_error",
                event.retryable if event is not None else False,
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            await close_plugins(list(getattr(engine, "_loaded_plugins", [])))
        aclose = getattr(chat, "aclose", None) if chat is not None else None
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass
        if trace_writer is not None and terminal is not None:
            kind, message, code, retryable = terminal
            if kind == "done":
                trace_writer.done(message)
            else:
                trace_writer.error(message, code=code, retryable=retryable)
        warnings_hook.notice_printer = previous_notice_printer


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
        status = _run_plugin_command(
            options.plugin_args, config, assume_yes=options.assume_yes
        )
        if status:
            sys.exit(status)
        return

    if options.bg_args is not None:
        # #129: `bg` is a read-only subcommand (no daemon, no agent run) —
        # it lists local sessions with their run status.
        from phoson_cli.commands import run_bg_command

        sys.exit(run_bg_command(options.bg_args))

    if options.self_update:
        _require_interactive_stdin("--self-update")
        self_update()
        return

    if options.uninstall:
        _require_interactive_stdin("--uninstall")
        uninstall()
        return

    if options.setup:
        _require_interactive_stdin("--setup")
        try:
            config = load_config()
            asyncio.run(run_install_wizard(config))
        except PhosonConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    trace_writer = TraceWriter() if options.trace or trace_enabled() else None
    _resolve_task(options, trace_writer)

    if trace_writer is not None and options.task is None:
        trace_writer.error(
            "--trace requires a one-shot task argument or piped stdin.",
            code="usage_error",
        )
        sys.exit(2)

    if options.task is not None:
        warnings_hook.notice_printer = lambda message: (
            trace_writer.diagnostic(message, source="warning")
            if trace_writer is not None
            else print(message, file=sys.stderr)
        )

    try:
        config = load_config()
    except PhosonConfigError as exc:
        if trace_writer is not None:
            trace_writer.error(str(exc), code="config_error")
        else:
            print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _apply_overrides(config, options)
    if options.theme:
        _prepare_cli_theme(config)

    # One-shot mode: phoson-cli "task" | -p "task" | piped stdin.
    # Skips the interactive wizard — missing credentials surface as the
    # friendly pre-check error below, which is what scripts need.
    if options.task is not None:
        try:
            build_chat(config)
        except ValueError as exc:
            message = (
                f"{exc}. Set the provider's API key in ~/.phoson/config.toml "
                "or run: phoson-cli --setup"
            )
            if trace_writer is not None:
                trace_writer.error(message, code="configuration_error")
            else:
                print(f"Error: {exc}", file=sys.stderr)
                print(
                    "Set the provider's API key in ~/.phoson/config.toml "
                    "or run: phoson-cli --setup",
                    file=sys.stderr,
                )
            sys.exit(1)
        sys.exit(asyncio.run(_run_oneshot(config, options.task, trace=options.trace)))

    config_path = Path.home() / ".phoson" / "config.toml"

    if not config_path.exists() and not has_configured_provider(config):
        print("No API keys configured. Running setup wizard...")
        try:
            asyncio.run(run_install_wizard(config))
            # Reload config after setup and re-apply the CLI overrides on top.
            config = load_config()
        except PhosonConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        _apply_overrides(config, options)
        if options.theme:
            _prepare_cli_theme(config)

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
                "Full-screen UI unavailable; using the classic REPL. Both stdin "
                "and stdout must be capable TTYs with TERM set.",
                file=sys.stderr,
            )
        repl = PhosonRepl(config)
        if options.theme and getattr(config, "_cli_theme_deferred", False):
            try:
                _validate_runtime_cli_theme(config, repl.theme_registry)
            except _CliThemeError as exc:
                asyncio.run(repl.shutdown())
                _fail(str(exc))
        # I-112: point the warnings hook's printer at the themed renderer so
        # notices match the front end's style (live theme; /theme re-points it).
        # getattr keeps fakes without a renderer (tests) on the plain default.
        renderer = getattr(repl, "renderer", None)
        if renderer is not None:
            warnings_hook.notice_printer = renderer.print_warn
        # Backstop: capture stray stderr (stray prints/logging) to a log
        # file + in-memory tail while the front end runs, so it cannot tear
        # the render. stdout is left alone (it is the paint/result channel).
        from phoson_cli.output_guard import OutputGuard

        guard = OutputGuard()
        guard.install()
        try:
            ran = asyncio.run(_run_classic(repl, options.session))
        finally:
            guard.restore()
            warnings_hook.reset_notice_printer()
        if not ran:
            sys.exit(1)
        _print_resume_hint(repl)
        return

    try:
        app = PhosonApp(config)
    except PhosonKeyBindingsError as exc:
        # A [keys] section that survived load-time validation but still
        # collides (e.g. two actions remapped onto one sequence): fail
        # with the same friendly message as every other config error.
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if options.theme and getattr(config, "_cli_theme_deferred", False):
        try:
            _validate_runtime_cli_theme(config, app.repl.theme_registry)
        except _CliThemeError as exc:
            asyncio.run(app.repl.shutdown())
            _fail(str(exc))
    # Backstop: capture stray stderr (stray prints/logging) to a log file +
    # in-memory tail while the front end runs, so it cannot tear the render.
    # stdout is left alone (it is the prompt_toolkit paint channel).
    from phoson_cli.output_guard import OutputGuard

    guard = OutputGuard()
    guard.install()
    try:
        ran = asyncio.run(_run_fullscreen(app, options.session))
    finally:
        guard.restore()
    if not ran:
        sys.exit(1)
    _print_resume_hint(getattr(app, "repl", None))


if __name__ == "__main__":
    main()
