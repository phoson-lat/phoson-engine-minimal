import json

from phoson_plugin_mcp import _plugin
from phoson_cli.__main__ import parse_args, _run_project_mcp_command


def test_parse_mcp_command():
    assert parse_args(["mcp", "project", "review"]).mcp_args == ["project", "review"]


def test_review_approve_revoke_project_mcp(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    config = project / ".phoson" / "mcps.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcpServers": {"sample": {"command": "echo", "args": ["ok"]}}})
    )
    trust = tmp_path / "trust.json"
    monkeypatch.setattr(_plugin, "_project_mcp_trust_path", lambda: trust)
    args = ["project"]
    assert _run_project_mcp_command([*args, "review", "--project", str(project)]) == 0
    assert "sample" in capsys.readouterr().out
    import builtins
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt: "yes")
    assert (
        _run_project_mcp_command(
            [*args, "approve", "sample", "--project", str(project)]
        )
        == 0
    )
    approval_output = capsys.readouterr().out
    assert '"command": "echo"' in approval_output
    assert approval_output.startswith("Project MCP server: sample\n{")
    assert json.loads(trust.read_text().removesuffix("\\n"))[str(project.resolve())][
        "sample"
    ]
    assert (
        _run_project_mcp_command([*args, "revoke", "sample", "--project", str(project)])
        == 0
    )
    assert "Revoked sample" in capsys.readouterr().out


def test_review_and_approval_prompt_redact_command_secrets(tmp_path, monkeypatch, capsys):
    import builtins

    project = tmp_path / "project"
    config = project / ".phoson" / "mcps.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"sample": {
        "command": "runner", "args": ["--api-key", "arg-secret", "--token=inline-secret"],
        "env": {"VISIBLE_SECRET": "env-secret"},
    }}}))
    assert _run_project_mcp_command(["project", "review", "--project", str(project)]) == 0
    review = capsys.readouterr().out
    for secret in ("arg-secret", "inline-secret", "env-secret"):
        assert secret not in review
    assert review.count("[redacted]") >= 3

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt: "no")
    assert _run_project_mcp_command(["project", "approve", "sample", "--project", str(project)]) == 1
    prompt = capsys.readouterr().out
    for secret in ("arg-secret", "inline-secret", "env-secret"):
        assert secret not in prompt


def test_project_mcp_missing_server_fails(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert (
        _run_project_mcp_command(
            ["project", "approve", "missing", "--project", str(project)]
        )
        == 1
    )
