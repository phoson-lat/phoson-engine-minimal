"""Tests for phoson_cli.tools.search — native grep + glob (F-21b / #181).

Both tools have two code paths — ``rg`` (ripgrep on PATH) and a pure
Python fallback — that must agree. The tests therefore:

- exercise the *public* functions (``_grep``/``_glob``), which pick the
  backend automatically (rg present in the CI/dev environment);
- assert explicit **parity** between ``_rg_*`` and ``_py_*`` backends on
  a shared scratch tree;
- pin the output contract (``path:line: content``, caps, gitignore and
  noise-dir behavior) that the model sees either way.
"""

import re
from pathlib import Path

import pytest

from phoson_cli.tools import build_tools, build_tools_dict
from phoson_cli.tools.search import (
    _glob,
    _grep,
    _py_glob,
    _py_grep,
    _rg_glob,
    _rg_grep,
    _glob_match,
    _rg_available,
    _parse_gitignore,
)

RG_PRESENT = _rg_available()


# ── fixture: a small tree exercising every rule ─────────────────────────────


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "build").mkdir()
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "docs" / "api").mkdir(parents=True)

    (tmp_path / "src" / "main.py").write_text(
        "def alpha():\n    return 1\n\nALPHA constant\n", encoding="utf-8"
    )
    (tmp_path / "src" / "pkg" / "util.py").write_text(
        "ALPHA = 42\nbeta line\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        "# Alpha project\nsee ALPHA docs\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "api" / "spec.md").write_text("ALPHA spec\n", encoding="utf-8")

    # gitignore: .env, build/ dir, *.log with a negation, docs/api/**
    (tmp_path / ".env").write_text("ALPHA secret\n", encoding="utf-8")
    (tmp_path / "build" / "out.txt").write_text("ALPHA build\n", encoding="utf-8")
    (tmp_path / "run.log").write_text("ALPHA log\n", encoding="utf-8")
    (tmp_path / "keep.log").write_text("ALPHA keep\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(
        "# comments and blanks are fine\n"
        "\n"
        ".env\n"
        "build/\n"
        "*.log\n"
        "!keep.log\n"
        "docs/api/**\n",
        encoding="utf-8",
    )

    # noise dirs + hidden files (skipped by default, like rg without --hidden)
    (tmp_path / "node_modules" / "dep" / "index.js").write_text(
        "ALPHA dep\n", encoding="utf-8"
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("ALPHA git\n", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("ALPHA hidden\n", encoding="utf-8")

    # binary file: NUL byte → skipped by both backends
    (tmp_path / "blob.bin").write_bytes(b"ALPHA binary \x00\x01\n")

    return tmp_path


# ── grep: basic behavior (public function, auto backend) ────────────────────


class TestGrep:
    def test_finds_matches_with_path_line_content(self, tree: Path):
        out = _grep("ALPHA", path=str(tree))
        assert "src/main.py:4: ALPHA constant" in out
        assert "src/pkg/util.py:1: ALPHA = 42" in out
        assert "README.md:2: see ALPHA docs" in out

    def test_no_matches_message(self, tree: Path):
        out = _grep("zzz_no_such_token", path=str(tree))
        assert "No matches found" in out

    def test_missing_path(self, tmp_path: Path):
        out = _grep("x", path=str(tmp_path / "nope"))
        assert "Path not found" in out

    def test_regex_and_case_insensitive(self, tree: Path):
        lower = _grep("^ALPHA", path=str(tree))
        assert "src/main.py:4" in lower
        # unanchored + case-insensitive also finds "alpha" mid-line (superset)
        ci = _grep("alpha", path=str(tree), case_insensitive=True)
        assert "src/main.py:1" in ci
        assert "src/main.py:4: ALPHA constant" in ci
        assert "src/pkg/util.py:2: beta line" not in ci

    def test_glob_filters_files(self, tree: Path):
        out = _grep("ALPHA", path=str(tree), glob="*.py")
        assert "src/main.py:4" in out
        assert "README.md" not in out
        assert "docs/api" not in out

    def test_context_lines(self, tree: Path):
        out = _grep("ALPHA", path=str(tree), context=1, glob="src/pkg/*.py")
        lines = out.splitlines()
        assert "src/pkg/util.py:1: ALPHA = 42" in lines
        # context=1 includes the line before/after the match
        assert "src/pkg/util.py:2: beta line" in lines

    def test_max_results_stops_with_note(self, tree: Path):
        out = _grep("ALPHA", path=str(tree), max_results=2)
        assert "stopped at 2 matching lines" in out
        match_lines = [line for line in out.splitlines() if re.match(r".+:\d+: ", line)]
        assert len(match_lines) <= 3  # 2 matches (+ possible context line)

    def test_gitignore_respected(self, tree: Path):
        out = _grep("ALPHA", path=str(tree))
        assert ".env" not in out
        assert "build/out.txt" not in out
        assert "run.log" not in out
        assert "docs/api/spec.md" not in out

    def test_gitignore_negation_keeps_file(self, tree: Path):
        out = _grep("ALPHA", path=str(tree))
        assert "keep.log:1: ALPHA keep" in out

    def test_noise_dirs_and_hidden_and_binary_skipped(self, tree: Path):
        out = _grep("ALPHA", path=str(tree))
        assert "node_modules" not in out
        assert ".git" not in out
        assert ".hidden.txt" not in out
        assert "blob.bin" not in out

    def test_single_file_path(self, tree: Path):
        out = _grep("ALPHA", path=str(tree / "README.md"))
        assert "README.md:2: see ALPHA docs" in out

    def test_invalid_regex_is_actionable(self, tree: Path):
        out = _grep("(", path=str(tree))
        assert "invalid search pattern" in out.lower()

    def test_max_results_is_clamped_to_hard_cap(self, tree: Path):
        # A bogus huge value must not be forwarded to the backends.
        out = _grep("ALPHA", path=str(tree), max_results=10**9)
        assert "1000000000" not in out


# ── grep: backend parity (rg vs Python) ─────────────────────────────────────


class TestGrepParity:
    ARGS = dict(glob="*.py", case_insensitive=False, max_results=50, context=0)

    def test_same_output_on_shared_tree(self, tree: Path):
        rg = _rg_grep(tree, "ALPHA", **self.ARGS)
        py = _py_grep(tree, "ALPHA", **self.ARGS)
        assert rg == py

    def test_context_output_parity(self, tree: Path):
        rg = _rg_grep(
            tree, "ALPHA", glob=None, case_insensitive=False, max_results=50, context=1
        )
        py = _py_grep(
            tree, "ALPHA", glob=None, case_insensitive=False, max_results=50, context=1
        )
        assert rg == py

    def test_case_insensitive_parity(self, tree: Path):
        rg = _rg_grep(
            tree, "alpha", glob=None, case_insensitive=True, max_results=50, context=0
        )
        py = _py_grep(
            tree, "alpha", glob=None, case_insensitive=True, max_results=50, context=0
        )
        assert rg == py

    def test_empty_result_parity(self, tree: Path):
        assert _rg_grep(tree, "zzz_none", **self.ARGS) == _py_grep(
            tree, "zzz_none", **self.ARGS
        )


# ── glob: basic behavior ────────────────────────────────────────────────────


class TestGlob:
    def test_basename_at_any_depth(self, tree: Path):
        out = _glob("*.py", path=str(tree))
        assert "src/main.py" in out
        assert "src/pkg/util.py" in out
        assert "README.md" not in out

    def test_double_star_spans_segments(self, tree: Path):
        out = _glob("src/**/*.py", path=str(tree))
        assert "src/main.py" in out
        assert "src/pkg/util.py" in out
        out_all = _glob("**/*.py", path=str(tree))
        assert "src/pkg/util.py" in out_all

    def test_dir_trailing_double_star_matches_subtree(self, tree: Path):
        out = _glob("src/**", path=str(tree))
        assert "src/main.py" in out
        assert "src/pkg/util.py" in out

    def test_gitignore_and_noise(self, tree: Path):
        # --files semantics: hidden *files* are listed, hidden *dirs* and
        # gitignored dirs are not.
        out = _glob("**/*.txt", path=str(tree))
        assert ".hidden.txt" in out
        assert "node_modules" not in out
        # An ignored directory is pruned even when addressed directly.
        out_build = _glob("build/**", path=str(tree))
        assert "build/out.txt" not in out_build

    def test_most_recently_modified_first(self, tmp_path: Path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("a", encoding="utf-8")
        b.write_text("b", encoding="utf-8")
        import os
        import time

        old = time.time() - 100
        os.utime(a, (old, old))
        out = _glob("*.txt", path=str(tmp_path))
        lines = out.splitlines()
        assert lines[0] == "b.txt"
        assert lines[1] == "a.txt"

    def test_no_match_message(self, tree: Path):
        out = _glob("*.zig", path=str(tree))
        assert "No files matched" in out

    def test_missing_path(self, tmp_path: Path):
        assert "Path not found" in _glob("*.py", path=str(tmp_path / "nope"))

    def test_file_path_rejected(self, tree: Path):
        assert "Not a directory" in _glob("*.py", path=str(tree / "README.md"))


class TestGlobParity:
    def test_same_output_on_shared_tree(self, tree: Path):
        assert _rg_glob(tree, "**/*.py") == _py_glob(tree, "**/*.py")

    def test_basename_pattern_parity(self, tree: Path):
        assert _rg_glob(tree, "*.md") == _py_glob(tree, "*.md")

    def test_no_match_parity(self, tree: Path):
        assert _rg_glob(tree, "*.zig") == _py_glob(tree, "*.zig")


# ── .gitignore parser unit tests ────────────────────────────────────────────


class TestGitignoreParser:
    def test_basenames_match_at_any_depth(self):
        rules = _parse_gitignore("*.pyc\n")
        assert _match(rules, "a.pyc")
        assert _match(rules, "deep/dir/x.pyc")

    def test_slash_anchors_to_file_dir(self):
        rules = _parse_gitignore("/out.txt\n")
        assert _match(rules, "out.txt")
        assert not _match(rules, "sub/out.txt")

    def test_star_does_not_cross_slash(self):
        rules = _parse_gitignore("a/*\n")
        assert _match(rules, "a/x")
        assert not _match(rules, "a/b/x")

    def test_double_star_spans(self):
        rules = _parse_gitignore("docs/**\n")
        assert _match(rules, "docs/a/b.md")
        assert not _match(rules, "other/a/b.md")

    def test_trailing_slash_is_directory_only(self):
        rules = _parse_gitignore("build/\n")
        assert _match(rules, "build", is_dir=True)
        assert not _match(rules, "build", is_dir=False)
        assert _match(rules, "build/out.txt")

    def test_last_rule_in_file_wins(self):
        rules = _parse_gitignore("*.log\n!keep.log\n")
        assert _match(rules, "run.log")
        assert not _match(rules, "keep.log")

    def test_comments_and_quotes(self):
        rules = _parse_gitignore("# a comment\n\n'quoted.txt'\n")
        assert _match(rules, "quoted.txt")
        assert not _match(rules, "quoted.txt.bak")

    def test_negation_can_reinclude_deeper(self, tmp_path: Path):
        # Root ignores *.log; a nested .gitignore re-includes one file.
        (tmp_path / "sub").mkdir()
        (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (tmp_path / "sub" / ".gitignore").write_text(
            "!important.log\n", encoding="utf-8"
        )
        from phoson_cli.tools.search import _is_ignored, _gitignore_levels

        levels = _gitignore_levels(tmp_path)
        assert _is_ignored("run.log", levels)
        assert _is_ignored("sub/other.log", levels)
        assert not _is_ignored("sub/important.log", levels)


def _match(rules, rel: str, *, is_dir: bool = False) -> bool:
    from phoson_cli.tools.search import _is_ignored

    return _is_ignored(rel, [("", rules)], is_dir=is_dir)


# ── glob_match (shared with rg dialect) ─────────────────────────────────────


class TestGlobMatch:
    @pytest.mark.parametrize(
        ("rel", "pattern", "expected"),
        [
            ("a/b.txt", "*.txt", True),  # basename at any depth
            ("a/b.txt", "b.txt", True),
            ("a/b.txt", "a/*.txt", True),
            ("a/b/c.txt", "a/*.txt", False),  # * never crosses /
            ("a/b.txt", "**/*.txt", True),
            ("a/b/c.txt", "a/**/*.txt", True),
            ("a/b/c.txt", "**/c.txt", True),
            ("sub/x/y", "sub/**", True),
            ("a.txt", "a.txt", True),
            ("x/y/b.txt", "a/*.txt", False),
        ],
    )
    def test_cases(self, rel: str, pattern: str, expected: bool):
        assert _glob_match(rel, pattern) is expected


# ── registry / permissions / prompt wiring ──────────────────────────────────


class TestWiring:
    def test_tools_registered_in_base_registry(self):
        names = [t.name for t in build_tools()]
        assert "grep" in names and "glob" in names
        # Search tools sit with the file tools, before the web pair.
        assert names.index("grep") < names.index("web_search")

    def test_tools_are_registered_objects(self):
        registry = build_tools_dict()
        assert callable(registry["grep"].handler)
        assert callable(registry["glob"].handler)
        # schema is valid: the args the docs promise exist
        assert set(registry["grep"].parameters["properties"]) == {
            "pattern",
            "path",
            "glob",
            "case_insensitive",
            "max_results",
            "context",
        }
        assert set(registry["glob"].parameters["properties"]) == {"pattern", "path"}

    def test_match_args_declares_path_for_both(self):
        from phoson_cli.permissions_store import MATCH_ARGS

        assert MATCH_ARGS["grep"] == "path"
        assert MATCH_ARGS["glob"] == "path"

    def test_prompt_advertises_native_search_when_tools_present(self):
        from phoson_cli.session_utils import _tool_usage_block

        block = _tool_usage_block({"bash", "grep", "glob", "read_file"})
        assert "grep tool to search file contents" in block
        # The old "no native search" hint must be gone in this case.
        assert "There is no native search" not in block

    def test_prompt_falls_back_to_bash_hint_without_search_tools(self):
        from phoson_cli.session_utils import _tool_usage_block

        block = _tool_usage_block({"bash", "read_file"})
        assert "There is no native search/glob tool" in block
        assert "grep tool to search" not in block
