"""Tests for AGENTS.md filesystem memory (IMPROVEMENTS.md A3)."""

from pathlib import Path

import pytest

from phoson_cli.agents_md import (
    DEFAULT_MAX_TOKENS,
    load_agents_md,
    collect_agents_md_files,
)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A fake repository root with a nested working directory."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src" / "pkg").mkdir(parents=True)
    return root


def test_returns_empty_when_no_files_exist(tmp_path) -> None:
    assert load_agents_md(cwd=tmp_path, home_file=tmp_path / "none.md") == ""
    assert collect_agents_md_files(cwd=tmp_path, home_file=tmp_path / "none.md") == []


def test_global_home_file_is_loaded(tmp_path) -> None:
    home = tmp_path / "home-agents.md"
    home.write_text("always answer politely", encoding="utf-8")

    files = collect_agents_md_files(cwd=tmp_path / "elsewhere", home_file=home)
    assert [p for p, _ in files] == [home]
    assert "politely" in load_agents_md(cwd=tmp_path / "x", home_file=home)


def test_repo_root_and_cwd_hierarchy(repo) -> None:
    (repo / "AGENTS.md").write_text("root rule: use ruff", encoding="utf-8")
    workdir = repo / "src" / "pkg"
    (workdir / "AGENTS.md").write_text("pkg rule: no prints", encoding="utf-8")

    files = collect_agents_md_files(cwd=workdir, home_file=repo / "none")
    assert [p.name for p, _ in files] == ["AGENTS.md", "AGENTS.md"]
    # Root comes first, cwd last (closest instructions read as more specific).
    combined = load_agents_md(cwd=workdir, home_file=repo / "none")
    assert combined.index("root rule") < combined.index("pkg rule")


def test_claude_md_used_as_fallback_when_no_agents_md(repo) -> None:
    (repo / "CLAUDE.md").write_text("claude-style instructions", encoding="utf-8")
    files = collect_agents_md_files(cwd=repo, home_file=repo / "none")
    assert [p.name for p, _ in files] == ["CLAUDE.md"]


def test_agents_md_wins_over_claude_md_in_same_dir(repo) -> None:
    (repo / "AGENTS.md").write_text("agents wins", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("claude loses", encoding="utf-8")
    files = collect_agents_md_files(cwd=repo, home_file=repo / "none")
    assert [p.name for p, _ in files] == ["AGENTS.md"]
    assert "claude loses" not in load_agents_md(cwd=repo, home_file=repo / "none")


def test_at_imports_are_expanded(repo) -> None:
    (repo / "style.md").write_text("imported style guide", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "main file\n@style.md\nafter import", encoding="utf-8"
    )

    combined = load_agents_md(cwd=repo, home_file=repo / "none")
    assert "main file" in combined
    assert "imported style guide" in combined
    assert "@style.md" not in combined


def test_import_cycles_do_not_hang(repo) -> None:
    (repo / "a.md").write_text("A\n@b.md", encoding="utf-8")
    (repo / "b.md").write_text("B\n@a.md", encoding="utf-8")
    (repo / "AGENTS.md").write_text("start\n@a.md", encoding="utf-8")

    combined = load_agents_md(cwd=repo, home_file=repo / "none")
    assert "start" in combined
    assert "A" in combined
    assert "B" in combined


def test_missing_import_becomes_a_note_not_a_crash(repo) -> None:
    (repo / "AGENTS.md").write_text(
        "before\n@does-not-exist.md\nafter", encoding="utf-8"
    )
    combined = load_agents_md(cwd=repo, home_file=repo / "none")
    assert "[import not found: does-not-exist.md]" in combined


def test_budget_truncates_with_visible_marker(tmp_path) -> None:
    big = tmp_path / "big.md"
    big.write_text("word " * 5000, encoding="utf-8")  # ~25k chars ≈ 6k tokens

    combined = load_agents_md(max_tokens=100, cwd=tmp_path, home_file=big)
    assert len(combined) < 100 * 4 + 200  # budget + marker slack
    assert "truncated" in combined


def test_default_budget_is_2000_tokens() -> None:
    assert DEFAULT_MAX_TOKENS == 2000


# ── System prompt integration ─────────────────────────────────────────────────


def test_system_prompt_includes_agents_md_content(repo, monkeypatch) -> None:
    from phoson_cli.session_utils import build_system_prompt

    class _FakeTool:
        name = "bash"

    (repo / "AGENTS.md").write_text("distinctive-memory-content-42", encoding="utf-8")
    monkeypatch.chdir(repo)

    prompt = build_system_prompt(tools=[_FakeTool()])
    assert "distinctive-memory-content-42" in prompt
    assert "# Project memory (AGENTS.md)" in prompt


def test_system_prompt_has_no_memory_block_without_files(tmp_path, monkeypatch) -> None:
    from phoson_cli import session_utils

    class _FakeTool:
        name = "bash"

    monkeypatch.chdir(tmp_path)
    prompt = session_utils.build_system_prompt(tools=[_FakeTool()])
    assert "Project memory" not in prompt


def test_system_prompt_rereads_files_every_turn(repo, monkeypatch) -> None:
    """Cache-busting: an edit to AGENTS.md shows up on the next build."""
    from phoson_cli.session_utils import build_system_prompt

    class _FakeTool:
        name = "bash"

    agents = repo / "AGENTS.md"
    agents.write_text("first version", encoding="utf-8")
    monkeypatch.chdir(repo)

    first = build_system_prompt(tools=[_FakeTool()])
    assert "first version" in first

    agents.write_text("second version", encoding="utf-8")
    second = build_system_prompt(tools=[_FakeTool()])
    assert "second version" in second
