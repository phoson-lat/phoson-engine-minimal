"""Tests for the Skills system (IMPROVEMENTS.md G5, issue #52)."""

from pathlib import Path

import pytest

from phoson_cli.skills import (
    MAX_SKILLS,
    MAX_DESCRIPTION_CHARS,
    SkillMeta,
    find_skill,
    discover_skills,
    load_skill_body,
    parse_frontmatter,
    render_skill_index,
    skill_search_paths,
    iter_skill_resources,
)

# ─── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path) -> Path:
    """A fake repository root (``.git`` marker), like the agents_md tests."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _write_skill(
    directory: Path,
    name: str,
    description: str = "Use this when testing.",
    body: str = "Step 1. Do the thing.",
) -> Path:
    """Create ``<directory>/<name>/SKILL.md`` with frontmatter and return it."""
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


# ─── Frontmatter parsing ─────────────────────────────────────────────────────


def test_parse_frontmatter_splits_metadata_and_body() -> None:
    front, body = parse_frontmatter(
        "---\nname: pdf-filler\ndescription: Fill PDF forms\n---\n\n# Title\n\nBody."
    )
    assert front == {"name": "pdf-filler", "description": "Fill PDF forms"}
    assert body.startswith("# Title")
    assert "description:" not in body


def test_parse_frontmatter_strips_quotes_and_lowercases_keys() -> None:
    front, _body = parse_frontmatter("---\nName: \"quoted\"\nDESC: 'single'\n---\nx")
    assert front == {"name": "quoted", "desc": "single"}


def test_parse_frontmatter_joins_folded_continuation_lines() -> None:
    """A wrapped YAML description (indented continuation) stays one value."""
    front, _body = parse_frontmatter(
        "---\n"
        "name: architect\n"
        "description: Use when the user asks to design\n"
        "  system architecture or evaluate tradeoffs\n"
        "---\nbody\n"
    )
    assert front["description"] == (
        "Use when the user asks to design system architecture or evaluate tradeoffs"
    )
    assert front["name"] == "architect"


def test_parse_frontmatter_without_fence_is_all_body() -> None:
    front, body = parse_frontmatter("# Just markdown\n\ntext")
    assert front == {}
    assert body == "# Just markdown\n\ntext"


def test_parse_frontmatter_unterminated_fence_is_all_body() -> None:
    """No closing ``---``: we cannot tell where instructions start."""
    front, body = parse_frontmatter("---\nname: broken\nno closing fence\n")
    assert front == {}
    assert "name: broken" in body


# ─── Discovery ───────────────────────────────────────────────────────────────


def test_discovers_project_skill(repo) -> None:
    _write_skill(repo / ".phoson/skills", "reviewer", "Use for code review.")
    skills = discover_skills(cwd=repo, user_dir=repo / "no-home")
    assert [s.name for s in skills] == ["reviewer"]
    assert skills[0].description == "Use for code review."
    assert skills[0].source == ".phoson/skills"


def test_no_skills_returns_empty_list(repo) -> None:
    assert discover_skills(cwd=repo, user_dir=repo / "no-home") == []


def test_discovers_from_nested_working_directory(repo) -> None:
    """Discovery is rooted at the repo root, not the cwd."""
    _write_skill(repo / ".phoson/skills", "rooted")
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    skills = discover_skills(cwd=nested, user_dir=repo / "no-home")
    assert [s.name for s in skills] == ["rooted"]


def test_reads_claude_and_agents_compat_directories(repo) -> None:
    """Repos already set up for other harnesses work unchanged."""
    _write_skill(repo / ".agents/skills", "from-agents")
    _write_skill(repo / ".claude/skills", "from-claude")
    names = [s.name for s in discover_skills(cwd=repo, user_dir=repo / "no-home")]
    assert names == ["from-agents", "from-claude"]


def test_user_global_skills_are_discovered(repo, tmp_path) -> None:
    home = tmp_path / "home-skills"
    _write_skill(home, "global-skill")
    skills = discover_skills(cwd=repo, user_dir=home)
    assert [s.name for s in skills] == ["global-skill"]
    assert skills[0].source == "~/.phoson/skills"


def test_project_skill_shadows_global_with_same_name(repo, tmp_path) -> None:
    home = tmp_path / "home-skills"
    _write_skill(home, "dup", "global version")
    _write_skill(repo / ".phoson/skills", "dup", "project version")

    skills = discover_skills(cwd=repo, user_dir=home)
    assert len(skills) == 1
    assert skills[0].description == "project version"
    assert skills[0].source == ".phoson/skills"


def test_symlinked_mirror_is_not_listed_twice(repo) -> None:
    """``.claude/skills/x -> ../../.agents/skills/x`` is a real-world layout."""
    _write_skill(repo / ".agents/skills", "shared")
    claude_dir = repo / ".claude/skills"
    claude_dir.mkdir(parents=True)
    (claude_dir / "shared").symlink_to(
        repo / ".agents/skills/shared", target_is_directory=True
    )

    skills = discover_skills(cwd=repo, user_dir=repo / "no-home")
    assert [s.name for s in skills] == ["shared"]


def test_directory_without_skill_md_is_ignored(repo) -> None:
    (repo / ".phoson/skills/not-a-skill").mkdir(parents=True)
    (repo / ".phoson/skills/not-a-skill/README.md").write_text("hi", encoding="utf-8")
    _write_skill(repo / ".phoson/skills", "real")
    names = [s.name for s in discover_skills(cwd=repo, user_dir=repo / "no-home")]
    assert names == ["real"]


def test_nested_marketplace_layout_is_discovered(repo) -> None:
    """``skills/<pack>/skills/<name>/SKILL.md`` (how packs are published)."""
    _write_skill(repo / ".agents/skills/engineering-team/skills", "senior-architect")
    names = [s.name for s in discover_skills(cwd=repo, user_dir=repo / "no-home")]
    assert names == ["senior-architect"]


def test_falls_back_to_directory_name_without_frontmatter_name(repo) -> None:
    skill_dir = repo / ".phoson/skills/implicit"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Plain instructions here.", encoding="utf-8")

    skills = discover_skills(cwd=repo, user_dir=repo / "no-home")
    assert [s.name for s in skills] == ["implicit"]
    # First prose line stands in for a missing description.
    assert skills[0].description == "Plain instructions here."


def test_long_description_is_capped(repo) -> None:
    _write_skill(repo / ".phoson/skills", "verbose", "x" * (MAX_DESCRIPTION_CHARS * 2))
    skills = discover_skills(cwd=repo, user_dir=repo / "no-home")
    assert len(skills[0].description) <= MAX_DESCRIPTION_CHARS
    assert skills[0].description.endswith("…")


def test_discovery_is_capped_at_max_skills(repo) -> None:
    directory = repo / ".phoson/skills"
    for i in range(MAX_SKILLS + 5):
        _write_skill(directory, f"skill-{i:03d}")
    skills = discover_skills(cwd=repo, user_dir=repo / "no-home")
    assert len(skills) == MAX_SKILLS


def test_unreadable_skill_is_skipped_not_raised(repo) -> None:
    """Binary/undecodable SKILL.md must not take down discovery."""
    bad = repo / ".phoson/skills/bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_bytes(b"\xff\xfe\x00binary")
    _write_skill(repo / ".phoson/skills", "good")

    names = [s.name for s in discover_skills(cwd=repo, user_dir=repo / "no-home")]
    assert names == ["good"]


def test_search_paths_only_lists_existing_directories(repo, tmp_path) -> None:
    _write_skill(repo / ".phoson/skills", "x")
    paths = skill_search_paths(cwd=repo, user_dir=tmp_path / "missing")
    assert [label for _p, label in paths] == [".phoson/skills"]


def test_discovery_reflects_a_skill_added_later(repo) -> None:
    """Re-run discovery, no caching: new skills work on the next turn."""
    assert discover_skills(cwd=repo, user_dir=repo / "no-home") == []
    _write_skill(repo / ".phoson/skills", "late")
    assert [s.name for s in discover_skills(cwd=repo, user_dir=repo / "no-home")] == [
        "late"
    ]


# ─── Lookup ──────────────────────────────────────────────────────────────────


def _meta(name: str) -> SkillMeta:
    return SkillMeta(
        name=name,
        description="d",
        path=Path(f"/tmp/{name}/SKILL.md"),
        root=Path(f"/tmp/{name}"),
        source=".phoson/skills",
    )


def test_find_skill_exact_case_insensitive_and_prefix() -> None:
    skills = [_meta("senior-architect"), _meta("pdf-filler")]
    assert find_skill("pdf-filler", skills).name == "pdf-filler"
    assert find_skill("PDF-Filler", skills).name == "pdf-filler"
    assert find_skill("senior", skills).name == "senior-architect"


def test_find_skill_ambiguous_prefix_returns_none() -> None:
    skills = [_meta("review-python"), _meta("review-go")]
    assert find_skill("review", skills) is None


def test_find_skill_unknown_returns_none() -> None:
    assert find_skill("nope", [_meta("a")]) is None


# ─── Index rendering (system prompt tier 1) ──────────────────────────────────


def test_index_is_empty_without_skills() -> None:
    assert render_skill_index([]) == ""


def test_index_lists_name_and_description(repo) -> None:
    _write_skill(repo / ".phoson/skills", "reviewer", "Use for code review.")
    skills = discover_skills(cwd=repo, user_dir=repo / "no-home")
    index = render_skill_index(skills)
    assert "# Skills (load on demand)" in index
    assert "- reviewer: Use for code review." in index
    # The index must tell the model *how* to load one.
    assert "`skill`" in index


def test_index_does_not_include_skill_bodies(repo) -> None:
    """Progressive disclosure: the body only arrives via the tool."""
    _write_skill(
        repo / ".phoson/skills",
        "reviewer",
        "Use for code review.",
        body="SECRET-BODY-CONTENT",
    )
    skills = discover_skills(cwd=repo, user_dir=repo / "no-home")
    assert "SECRET-BODY-CONTENT" not in render_skill_index(skills)


def test_index_respects_the_token_budget(repo) -> None:
    directory = repo / ".phoson/skills"
    for i in range(20):
        _write_skill(directory, f"skill-{i:02d}", "d" * 200)
    skills = discover_skills(cwd=repo, user_dir=repo / "no-home")

    index = render_skill_index(skills, max_tokens=50)  # 200 chars
    assert len(index) < 900
    assert "omitted from this index" in index
    # Never truncated mid-entry: every listed line is complete.
    for line in index.splitlines():
        if line.startswith("- "):
            assert ": " in line


# ─── Body loading (tier 2) ───────────────────────────────────────────────────


def test_load_body_returns_instructions_without_frontmatter(repo) -> None:
    _write_skill(repo / ".phoson/skills", "reviewer", body="Check for N+1 queries.")
    skill = discover_skills(cwd=repo, user_dir=repo / "no-home")[0]
    body = load_skill_body(skill)
    assert "Check for N+1 queries." in body
    assert "description:" not in body
    assert "# Skill: reviewer" in body
    # The absolute root lets the model run bundled scripts without guessing.
    assert str(skill.root) in body


def test_load_body_lists_bundled_resources(repo) -> None:
    _write_skill(repo / ".phoson/skills", "architect")
    root = repo / ".phoson/skills/architect"
    (root / "scripts").mkdir()
    (root / "scripts" / "analyze.py").write_text("print(1)", encoding="utf-8")
    (root / "references").mkdir()
    (root / "references" / "patterns.md").write_text("# p", encoding="utf-8")

    skill = discover_skills(cwd=repo, user_dir=repo / "no-home")[0]
    assert iter_skill_resources(skill) == [
        "references/patterns.md",
        "scripts/analyze.py",
    ]
    body = load_skill_body(skill)
    assert "scripts/analyze.py" in body


def test_load_body_truncates_oversized_skill(repo) -> None:
    _write_skill(repo / ".phoson/skills", "huge", body="line\n" * 5000)
    skill = discover_skills(cwd=repo, user_dir=repo / "no-home")[0]
    body = load_skill_body(skill, max_chars=500)
    assert len(body) < 1500
    assert "truncated at 500 characters" in body
    assert "read_file" in body  # pointer to page the rest


def test_load_body_of_deleted_skill_reports_instead_of_raising(repo) -> None:
    _write_skill(repo / ".phoson/skills", "ghost")
    skill = discover_skills(cwd=repo, user_dir=repo / "no-home")[0]
    skill.path.unlink()
    assert "could not be read" in load_skill_body(skill)


# ─── The ``skill`` tool (activation) ─────────────────────────────────────────


def test_skill_tool_returns_the_body(repo, monkeypatch) -> None:
    from phoson_cli.tools.skill import _skill

    _write_skill(repo / ".phoson/skills", "reviewer", body="Check for N+1 queries.")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    result = _skill("reviewer")
    assert "Check for N+1 queries." in result
    assert "# Skill: reviewer" in result


def test_skill_tool_unknown_name_lists_available(repo, monkeypatch) -> None:
    from phoson_cli.tools.skill import _skill

    _write_skill(repo / ".phoson/skills", "reviewer")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    result = _skill("nonexistent")
    assert "Unknown skill" in result
    assert "reviewer" in result


def test_skill_tool_without_any_skill_explains_the_layout(repo, monkeypatch) -> None:
    from phoson_cli.tools.skill import _skill

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    result = _skill("anything")
    assert "No skills are available" in result
    assert "SKILL.md" in result


def test_skill_tool_schema_takes_a_name() -> None:
    from phoson_cli.tools.skill import skill as skill_tool

    assert skill_tool.name == "skill"
    assert skill_tool.parameters["required"] == ["name"]
    assert set(skill_tool.parameters["properties"]) == {"name"}


# ─── Registry wiring ─────────────────────────────────────────────────────────


def test_skill_tool_is_absent_when_no_skills_exist(repo, monkeypatch) -> None:
    """No skills → no schema cost on every request."""
    from phoson_cli.tools import build_tools

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    assert "skill" not in {t.name for t in build_tools()}


def test_skill_tool_joins_the_registry_when_a_skill_exists(repo, monkeypatch) -> None:
    from phoson_cli.tools import build_tools, build_tools_dict

    _write_skill(repo / ".phoson/skills", "reviewer")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    assert "skill" in {t.name for t in build_tools()}
    assert "skill" in build_tools_dict()


def test_include_skill_flag_overrides_auto_detection(repo, monkeypatch) -> None:
    from phoson_cli.tools import build_tools

    monkeypatch.chdir(repo)
    assert "skill" in {t.name for t in build_tools(include_skill=True)}
    assert "skill" not in {t.name for t in build_tools(include_skill=False)}


def test_registry_survives_broken_discovery(monkeypatch) -> None:
    """A failing scan degrades to "no skill tool", never an exception."""
    import phoson_cli.tools as tools_mod

    def _boom(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr("phoson_cli.skills.discover_skills", _boom)
    names = {t.name for t in tools_mod.build_tools()}
    assert "skill" not in names
    assert "read_file" in names  # the rest of the registry is intact


# ─── System prompt integration ──────────────────────────────────────────────


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


def test_system_prompt_includes_the_skill_index(repo, monkeypatch) -> None:
    from phoson_cli.session_utils import build_system_prompt

    _write_skill(repo / ".phoson/skills", "reviewer", "Use for code review.")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    prompt = build_system_prompt(tools=[_FakeTool("bash"), _FakeTool("skill")])
    assert "# Skills (load on demand)" in prompt
    assert "- reviewer: Use for code review." in prompt


def test_system_prompt_has_no_index_without_the_skill_tool(repo, monkeypatch) -> None:
    """Never advertise a tool that is not in the registry."""
    from phoson_cli.session_utils import build_system_prompt

    _write_skill(repo / ".phoson/skills", "reviewer")
    monkeypatch.chdir(repo)

    prompt = build_system_prompt(tools=[_FakeTool("bash")])
    assert "Skills (load on demand)" not in prompt


def test_system_prompt_has_no_index_without_skills(repo, monkeypatch) -> None:
    from phoson_cli.session_utils import build_system_prompt

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    prompt = build_system_prompt(tools=[_FakeTool("bash"), _FakeTool("skill")])
    assert "Skills (load on demand)" not in prompt


def test_system_prompt_is_stable_across_calls(repo, monkeypatch) -> None:
    """Prompt caching (G2): the index must not vary between turns."""
    from phoson_cli.session_utils import build_system_prompt

    _write_skill(repo / ".phoson/skills", "reviewer")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    tools = [_FakeTool("bash"), _FakeTool("skill")]
    assert build_system_prompt(tools=tools) == build_system_prompt(tools=tools)


def test_system_prompt_never_carries_a_skill_body(repo, monkeypatch) -> None:
    """The whole point of G5: bodies stay out of the stable prefix."""
    from phoson_cli.session_utils import build_system_prompt

    _write_skill(
        repo / ".phoson/skills", "reviewer", body="VERY-LONG-BODY-" + "x" * 4000
    )
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    prompt = build_system_prompt(tools=[_FakeTool("bash"), _FakeTool("skill")])
    assert "VERY-LONG-BODY" not in prompt


# ─── /skills command ─────────────────────────────────────────────────────────


class _FakeHost:
    """CommandHost double capturing output (pattern of the C2/B3 tests)."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_warn(self, message: str) -> None:
        self.warns.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def print_help(self, entries) -> None: ...
    def print_renderable(self, renderable) -> None: ...
    async def pick_model(self, models, current_model, **kw): ...
    async def pick_provider(self, providers, current_provider): ...
    async def pick_session(self, sessions, current_id): ...

    async def confirm(self, prompt: str) -> bool:
        return False

    async def run_setup(self) -> None: ...


def _handler(host: "_FakeHost"):
    from types import SimpleNamespace

    from phoson_cli.commands import CommandHandler

    return CommandHandler(SimpleNamespace(), host=host)


@pytest.mark.asyncio
async def test_slash_skills_lists_discovered_skills(repo, monkeypatch) -> None:
    from phoson_cli.commands import Command

    _write_skill(repo / ".phoson/skills", "reviewer", "Use for code review.")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    host = _FakeHost()
    await _handler(host).handle(Command(name="/skills", args=""))

    output = "\n".join(host.infos)
    assert "reviewer" in output
    assert "Use for code review." in output
    assert ".phoson/skills" in output


@pytest.mark.asyncio
async def test_slash_skills_without_skills_shows_where_to_put_them(
    repo, monkeypatch
) -> None:
    from phoson_cli.commands import Command

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    host = _FakeHost()
    await _handler(host).handle(Command(name="/skills", args=""))

    output = "\n".join(host.infos)
    assert "No skills found" in output
    assert "SKILL.md" in output


@pytest.mark.asyncio
async def test_slash_skills_with_name_shows_the_body(repo, monkeypatch) -> None:
    from phoson_cli.commands import Command

    _write_skill(repo / ".phoson/skills", "reviewer", body="Check for N+1 queries.")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    host = _FakeHost()
    await _handler(host).handle(Command(name="/skills", args="reviewer"))

    assert "Check for N+1 queries." in "\n".join(host.infos)


@pytest.mark.asyncio
async def test_slash_skills_unknown_name_errors(repo, monkeypatch) -> None:
    from phoson_cli.commands import Command

    _write_skill(repo / ".phoson/skills", "reviewer")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "phoson_cli.skills.DEFAULT_USER_SKILLS_DIR", repo / "no-home", raising=False
    )

    host = _FakeHost()
    await _handler(host).handle(Command(name="/skills", args="ghost"))

    assert any("Unknown skill" in err for err in host.errors)


def test_slash_skills_is_registered_and_in_help() -> None:
    from phoson_cli.commands import COMMANDS, get_grouped_command_help

    assert "/skills" in COMMANDS
    entries = [
        name for _title, rows in get_grouped_command_help() for name, _help in rows
    ]
    assert "/skills" in entries


def test_skill_names_completer_never_raises(monkeypatch) -> None:
    """The composer must survive a broken skills directory (G5)."""
    from phoson_cli.fullscreen.app import _skill_names

    monkeypatch.setattr(
        "phoson_cli.skills.discover_skills",
        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
    )
    assert _skill_names() == []
