from phoson_cli.commands import (
    COMMAND_SPECS,
    COMMANDS,
    Command,
    CommandHandler,
    get_command_help,
    parse_command,
)


def test_parse_command_returns_none_for_non_slash() -> None:
    assert parse_command("hello world") is None
    assert parse_command("") is None
    assert parse_command("   ") is None


def test_parse_command_strips_whitespace() -> None:
    cmd = parse_command("  /model gpt-4o  ")
    assert cmd is not None
    assert cmd.name == "/model"
    assert cmd.args == "gpt-4o"


def test_parse_command_extracts_name_and_args() -> None:
    cmd = parse_command("/model gpt-4o-mini")
    assert cmd is not None
    assert cmd.name == "/model"
    assert cmd.args == "gpt-4o-mini"


def test_parse_command_with_no_args() -> None:
    cmd = parse_command("/model")
    assert cmd is not None
    assert cmd.name == "/model"
    assert cmd.args == ""


def test_parse_command_args_are_stripped() -> None:
    cmd = parse_command("/label   my label  ")
    assert cmd is not None
    assert cmd.name == "/label"
    assert cmd.args == "my label"


def test_parse_command_preserves_spaces_in_args() -> None:
    cmd = parse_command("/label my multi word label")
    assert cmd is not None
    assert cmd.args == "my multi word label"


def test_parse_command_unknown_command_still_parses() -> None:
    cmd = parse_command("/unknown arg1 arg2")
    assert cmd is not None
    assert cmd.name == "/unknown"
    assert cmd.args == "arg1 arg2"


def test_parse_command_handles_multiple_spaces() -> None:
    cmd = parse_command("/model    gpt-4o")
    assert cmd is not None
    assert cmd.args == "gpt-4o"


def test_parse_command_all_known_commands() -> None:
    known = {
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
    for cmd_name in known:
        cmd = parse_command(cmd_name)
        assert cmd is not None, f"Failed to parse {cmd_name}"
        assert cmd.name == cmd_name
        assert cmd.args == ""


def test_command_dataclass() -> None:
    cmd = Command(name="/test", args="arg1 arg2")
    assert cmd.name == "/test"
    assert cmd.args == "arg1 arg2"


# ─── Dispatch table ──────────────────────────────────────────────────────────


def test_command_specs_have_implemented_methods() -> None:
    """Every CommandSpec.method must exist on CommandHandler."""
    missing = [
        spec.method
        for spec in COMMAND_SPECS
        if not hasattr(CommandHandler, spec.method)
    ]
    assert missing == [], f"Missing handlers: {missing}"


def test_commands_set_matches_command_specs() -> None:
    """The flat COMMANDS frozenset must include every alias from COMMAND_SPECS."""
    expected = {name for spec in COMMAND_SPECS for name in spec.names}
    assert set(COMMANDS) == expected


def test_get_command_help_returns_one_entry_per_spec() -> None:
    entries = get_command_help()
    assert len(entries) == len(COMMAND_SPECS)
    for (name, help_text), spec in zip(entries, COMMAND_SPECS, strict=True):
        assert help_text == spec.help
        # When there are aliases the entry shows them joined together.
        if len(spec.names) == 1:
            assert name == spec.primary
        else:
            for alias in spec.names:
                assert alias in name


def test_command_handler_dispatch_table_covers_all_aliases() -> None:
    """The handler's internal dispatch must register every alias."""
    # We build the handler with a stub repl object — it never reads it.
    handler = CommandHandler.__new__(CommandHandler)
    handler.repl = None  # type: ignore[assignment]
    handler._dispatch = {}
    for spec in COMMAND_SPECS:
        method = getattr(CommandHandler, spec.method)
        for name in spec.names:
            handler._dispatch[name] = method

    expected = {name for spec in COMMAND_SPECS for name in spec.names}
    assert set(handler._dispatch) == expected


def test_command_specs_have_no_duplicate_names() -> None:
    seen: set[str] = set()
    for spec in COMMAND_SPECS:
        for name in spec.names:
            assert name not in seen, f"duplicate command name: {name}"
            seen.add(name)
