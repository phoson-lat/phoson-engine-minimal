
from phoson_cli.commands import Command, parse_command


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
