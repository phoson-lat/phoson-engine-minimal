from pathlib import Path

import pytest

from phoson_cli.memory import (
    project_id,
    read_memory,
    write_memory,
    delete_memory,
    list_memories,
    memory_prompt,
)


def test_global_and_project_isolation_and_prompt(tmp_path: Path):
    home, project, other = tmp_path / "home", tmp_path / "work", tmp_path / "other"
    write_memory("notes", "global note", "global", home=home)
    write_memory("notes", "project note", project=project, home=home)
    write_memory("facts.md", "fact", project=other, home=home)
    assert read_memory("notes", project=project, home=home) == "project note\n"
    assert len(list_memories("project", project=other, home=home)) == 1
    prompt = memory_prompt(project=project, home=home)
    assert "project note" in prompt and "global note" not in prompt
    assert "fact" not in prompt


def test_system_prompt_uses_explicit_project_for_shared_memory(tmp_path: Path, monkeypatch):
    from phoson_cli.session_utils import build_system_prompt

    home = tmp_path / "home"
    project, process_cwd = tmp_path / "workspace", tmp_path / "app"
    write_memory("notes", "workspace-only note", project=project, home=home)
    write_memory("notes", "wrong project note", project=process_cwd, home=home)
    process_cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(process_cwd)

    prompt = build_system_prompt([], project=project)
    assert "workspace-only note" in prompt
    assert "wrong project note" not in prompt


def test_rejects_traversal_and_empty_content(tmp_path: Path):
    with pytest.raises(ValueError):
        write_memory("../secret", "no", home=tmp_path)
    with pytest.raises(ValueError):
        write_memory("empty", "  ", home=tmp_path)


def test_size_limits_atomic_replacement_and_casefolded_names(tmp_path: Path):
    from phoson_cli.memory import MAX_MEMORY_BYTES

    home = tmp_path / "home"
    path = write_memory("Notes", "original", home=home)
    assert read_memory("NOTES.md", home=home) == "original\n"
    with pytest.raises(ValueError):
        write_memory("notes", "x" * MAX_MEMORY_BYTES, home=home)
    assert read_memory("notes", home=home) == "original\n"
    path.write_bytes(b"x" * (MAX_MEMORY_BYTES + 1))
    with pytest.raises(ValueError):
        read_memory("notes", home=home)


def test_prompt_is_bounded_and_ignores_invalid_utf8(tmp_path: Path):
    home = tmp_path / "home"
    write_memory("bad", "ok", "global", home=home).write_bytes(b"\\xff")
    write_memory("large", "a" * 100, "global", home=home)
    assert len(memory_prompt(home=home, max_chars=8)) < 200


def test_delete_and_stable_project_identity(tmp_path: Path):
    p = tmp_path / "project"
    assert project_id(p) == project_id(p)
    write_memory("x", "body", project=p, home=tmp_path)
    delete_memory("x", project=p, home=tmp_path)
    assert list_memories(project=p, home=tmp_path) == []
