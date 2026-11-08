"""Security defaults: shell classification gaps, soft intent defaults, shell
tools that must not sidestep the bash gate, trace redaction, private offload."""

import os
import sys
import stat
from pathlib import Path

import pytest

from phoson_agent.intents import SAFE_DEFAULT_INTENT_LEVELS, infer_intents
from phoson_agent.permissions import (
    ToolHints,
    PermissionPolicy,
    pattern_allows,
    is_simple_shell_command,
)


def _bash(command: str) -> set[str]:
    return set(infer_intents("bash", {"command": command}))


# ─── Shell classification ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", {"filesystem_read"}),
        ("git diff HEAD~1", {"filesystem_read"}),
        ("git branch", {"filesystem_read"}),
        ("git branch -D old", {"filesystem_write"}),
        ("git commit -m 'x'", {"filesystem_write"}),
        ("git push origin main", {"network_outbound", "filesystem_write"}),
        ("git clean -fd", {"filesystem_delete"}),
        ("echo hi", {"filesystem_read"}),
        ("pwd", {"filesystem_read"}),
        ("ls 2>&1", {"filesystem_read"}),
        ("echo x > /dev/null", {"filesystem_read"}),
        ("env", {"filesystem_read_sensitive"}),
        ("printenv OPENAI_API_KEY", {"filesystem_read_sensitive"}),
        ("sort -o out.txt in.txt", {"filesystem_write"}),
        ("find . -fprint list.txt", {"filesystem_write"}),
        ("awk '{print $1}' f", {"filesystem_read"}),
        ("awk 'BEGIN{system(\"id\")}'", {"lang_exec"}),
        ("sed s/foo/bare/g f", {"filesystem_read"}),
        ("sed -n '1e id' f", {"lang_exec", "filesystem_write"}),
        ("pytest -q", {"process_spawn"}),
    ],
)
def test_shell_classification(command: str, expected: set[str]) -> None:
    assert _bash(command) == expected


def test_redirect_is_a_write_and_outside_workspace_is_flagged() -> None:
    intents = _bash("cat notes.txt > ~/.zshrc")
    assert {"filesystem_write", "filesystem_write_outside_workspace"} <= intents
    inside = _bash("echo hi > out.txt")
    assert "filesystem_write" in inside
    assert "filesystem_write_outside_workspace" not in inside


def test_bash_reads_of_credential_stores_are_sensitive() -> None:
    assert "filesystem_read_sensitive" in _bash("cat ~/.ssh/id_rsa")
    assert "filesystem_read_sensitive" in _bash("grep token ~/.phoson/config.toml")
    assert "filesystem_read_sensitive" in _bash("wc -l < ~/.aws/credentials")
    assert "filesystem_read_sensitive" not in _bash("cat README.md")


def test_redirect_lines_are_never_simple_for_patterns() -> None:
    assert is_simple_shell_command("cat a")
    assert not is_simple_shell_command("cat a > b")
    assert not is_simple_shell_command("cat < a")
    assert is_simple_shell_command("git commit -m 'a > b'")
    assert not pattern_allows("bash", "cat *", "cat a > ~/.zshrc")


# ─── Soft defaults ───────────────────────────────────────────────────────────


def _safe(**kwargs) -> PermissionPolicy:
    return PermissionPolicy(
        default_intent_levels=dict(SAFE_DEFAULT_INTENT_LEVELS), **kwargs
    )


def _level(policy: PermissionPolicy, command: str) -> tuple[str, str]:
    level, source, _ = policy.evaluate("bash", command, {"command": command})
    return level, source


def test_library_policy_keeps_allow_by_default() -> None:
    assert _level(PermissionPolicy(), "rm -rf build")[0] == "allow"


def test_safe_defaults_ask_for_programs_code_and_deletes_only() -> None:
    policy = _safe()
    assert _level(policy, "rm -rf build") == ("ask", "default_intent")
    assert _level(policy, "pytest -q")[0] == "ask"
    assert _level(policy, "python -c 'print(1)'")[0] == "ask"
    assert _level(policy, "git status") == ("allow", "default")
    assert _level(policy, "echo hi > out.txt")[0] == "allow"
    assert policy.evaluate("web_fetch", None, {"url": "https://x"})[0] == "allow"


def test_user_choices_override_the_soft_defaults() -> None:
    assert _level(_safe(levels={"*": "allow"}), "rm -rf build") == ("allow", "mode")
    assert _level(_safe(levels={"bash": "allow"}), "pytest")[0] == "allow"
    relaxed = _safe(intent_levels={"process_spawn": "allow"})
    assert _level(relaxed, "pytest")[0] == "allow"
    # The protective path intents still win over auto mode.
    assert _level(_safe(levels={"*": "allow"}), "cat ~/.ssh/id_rsa")[0] == "ask"


def test_soft_defaults_tighten_a_permissive_hint() -> None:
    read_only = ToolHints(annotated=True, read_only=True, destructive=False)
    policy = _safe(hints={"run_bg_job": read_only})
    level, source, _ = policy.evaluate("run_bg_job", None, {"command": "make"})
    assert (level, source) == ("ask", "default_intent")


# ─── Shell-running plugin tools follow the bash gate ─────────────────────────


def test_bg_job_and_command_monitor_are_classified_like_bash() -> None:
    assert set(infer_intents("run_bg_job", {"command": "rm -rf x"})) == {
        "filesystem_delete",
        "process_spawn",
    }
    monitor = {"kind": "command", "spec": {"command": "curl http://x"}}
    assert "network_outbound" in infer_intents("register_monitor", monitor)
    assert infer_intents("register_monitor", {"kind": "file", "spec": {}}) == ()


@pytest.mark.parametrize("tool", ["run_bg_job", "register_monitor"])
def test_shell_tools_inherit_the_bash_level(tool: str) -> None:
    args = (
        {"command": "ls"}
        if tool == "run_bg_job"
        else {"kind": "command", "spec": {"command": "ls"}}
    )
    policy = PermissionPolicy(levels={"bash": "deny"})
    assert policy.evaluate(tool, None, args)[0] == "deny"
    own = PermissionPolicy(levels={"bash": "deny", tool: "allow"})
    assert own.evaluate(tool, None, args)[0] == "allow"


# ─── CLI wiring ──────────────────────────────────────────────────────────────


def test_cli_policy_has_safe_defaults_with_and_without_a_file(tmp_path: Path) -> None:
    from phoson_cli.permissions_store import load_policy, save_policy

    missing = load_policy(tmp_path / "none.json")
    assert missing.default_intent_levels == SAFE_DEFAULT_INTENT_LEVELS

    path = tmp_path / "permissions.json"
    path.write_text('{"levels": {"bash": "allow"}}')
    loaded = load_policy(path)
    assert loaded.default_intent_levels == SAFE_DEFAULT_INTENT_LEVELS
    assert _level(loaded, "rm -rf x")[0] == "allow"  # the user's rule wins

    save_policy(loaded, path)
    assert "default_intent" not in path.read_text()  # never persisted


def test_auto_flag_is_parsed() -> None:
    from phoson_cli.__main__ import parse_args

    assert parse_args(["-p", "task", "--auto"]).auto is True
    assert parse_args(["-p", "task"]).auto is False


def test_trace_redacts_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from phoson_cli.trace_middleware import redact, _secret_values

    monkeypatch.setenv("ACME_API_KEY", "acme-secret-value-123")
    secrets = _secret_values()
    out = redact(
        {
            "command": "curl -H 'x: acme-secret-value-123' https://x",
            "nested": ["token ghp_" + "a" * 36, "sk-" + "b" * 40],
        },
        secrets,
    )
    text = str(out)
    assert "acme-secret-value-123" not in text
    assert "ghp_" not in text and "sk-bbb" not in text
    assert text.count("[REDACTED]") == 3


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_offloaded_output_is_private(tmp_path: Path) -> None:
    from phoson_agent.plugins.offload import offload_output

    previous = os.umask(0o022)
    out_dir = tmp_path / "compacted"
    try:
        stub = offload_output(
            "x" * 5000,
            tool_name="bash",
            tool_call_id="t1",
            max_chars=100,
            head_chars=10,
            tail_chars=10,
            output_dir=out_dir,
            error=False,
        )
    finally:
        os.umask(previous)
    assert "x" * 5000 not in stub
    (path,) = list(out_dir.iterdir())
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
