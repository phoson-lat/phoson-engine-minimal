"""Tests for @file mentions (IMPROVEMENTS.md E3).

Three layers, all exercised without a network / TTY:

1. :mod:`phoson_cli.file_mentions` — the pure, UI-independent core:
   mention parsing, path resolution, content-block building, the bounded
   candidate walk, and the size / count guards.
2. ``PathCompleter`` (in :mod:`phoson_cli.commands`, shared by both front
   ends) — the inline ``@`` dropdown: what it offers for a given buffer,
   that it ignores emails / bare handles / slash commands, and that it
   annotates files with a size hint.
3. :meth:`phoson_cli.controller.SessionController._build_user_message` —
   the controller expands mentions into content blocks and reports each one
   through the sink, so both front ends get identical behavior.
"""

from pathlib import Path

import pytest
from prompt_toolkit.document import Document

from phoson_cli.config import PhosonConfig
from phoson_llm.schemas import (
    Message,
    TextBlock,
    AudioBlock,
    ImageBlock,
    VideoBlock,
    DocumentBlock,
)
from phoson_cli.commands import PathCompleter
from phoson_cli.controller import SessionController
from phoson_cli.file_mentions import (
    MAX_MENTIONS_PER_MESSAGE,
    format_file_size,
    expand_file_mentions,
    iter_candidate_paths,
)

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small fake repo rooted at *tmp_path*."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("def f(): pass\n", encoding="utf-8")
    (tmp_path / "src" / "bar.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("assert True\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "img.png").write_bytes(b"\x89PNG" + b"0" * 100)
    # Ignored trees — must never surface in the walk.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x\n", encoding="utf-8")
    return tmp_path


def _complete(completer: PathCompleter, text: str) -> list[tuple[str, int, str]]:
    """Return ``(text, start_position, display_meta)`` for a buffer."""
    doc = Document(text, len(text))
    out = []
    for c in completer.get_completions(doc, None):
        meta = c.display_meta
        if not isinstance(meta, str):
            meta = "".join(t for _, t in meta)
        out.append((c.text, c.start_position, meta))
    return out


# ── 1. iter_candidate_paths (bounded walk) ───────────────────────────────────


def test_walk_lists_files_and_dirs(repo: Path) -> None:
    candidates = set(iter_candidate_paths(repo))
    # Files relative to root, dirs with a trailing slash.
    assert "README.md" in candidates
    assert "src/foo.py" in candidates
    assert "src/bar.py" in candidates
    assert "tests/test_foo.py" in candidates
    # Directories offered so the user can keep navigating.
    assert "src/" in candidates
    assert "tests/" in candidates


def test_walk_skips_dotdirs_and_node_modules(repo: Path) -> None:
    candidates = set(iter_candidate_paths(repo))
    for c in candidates:
        assert not c.startswith(".git")
        assert "node_modules" not in c
        assert "junk.js" not in c


def test_walk_respects_depth_bound(repo: Path) -> None:
    # Build a deeper-than-default tree and confirm the cap holds it back.
    deep = repo / "a"
    node = deep
    for i in range(10):
        node = node / f"d{i}"
        node.mkdir(parents=True)
    (node / "leaf.txt").write_text("x", encoding="utf-8")
    candidates = set(iter_candidate_paths(repo, max_depth=2))
    assert not any("d5" in c for c in candidates)


def test_walk_respects_entry_bound(repo: Path) -> None:
    for i in range(50):
        (repo / f"many_{i}.txt").write_text("x", encoding="utf-8")
    candidates = list(iter_candidate_paths(repo, max_entries=10))
    assert len(candidates) <= 12  # files + a couple of dir entries


# ── 1b. format_file_size ─────────────────────────────────────────────────────


def test_format_file_size_units() -> None:
    assert format_file_size(5) == "5 B"
    assert format_file_size(2048) == "2.0 KB"
    assert format_file_size(5 * 1024 * 1024) == "5.0 MB"


# ── 2. expand_file_mentions: parsing + resolution ────────────────────────────


def test_expands_text_file(repo: Path) -> None:
    r = expand_file_mentions("fix the bug in @src/foo.py please", cwd=repo)
    assert [type(b) for b in r.blocks] == [TextBlock]
    assert r.blocks[0].text.startswith("[File: ")
    assert "def f(): pass" in r.blocks[0].text
    assert len(r.mentions) == 1
    assert r.mentions[0].ok and r.mentions[0].kind == "text"


def test_expands_image_to_native_block(repo: Path) -> None:
    r = expand_file_mentions("look @img.png", cwd=repo)
    assert isinstance(r.blocks[0], ImageBlock)
    assert r.blocks[0].media_type == "image/png"
    assert r.blocks[0].source.startswith("file://")


@pytest.mark.parametrize(
    "suffix,cls",
    [
        (".wav", AudioBlock),
        (".mp4", VideoBlock),
        (".pdf", DocumentBlock),
    ],
)
def test_expands_media_suffixes(repo: Path, suffix: str, cls: type) -> None:
    (repo / f"media{suffix}").write_bytes(b"xx")
    r = expand_file_mentions(f"@media{suffix}", cwd=repo)
    assert isinstance(r.blocks[0], cls)


def test_missing_slash_path_warns(repo: Path) -> None:
    r = expand_file_mentions("@nope/missing.py", cwd=repo)
    assert r.blocks == []
    assert r.mentions[0].ok is False
    assert "file not found" in r.mentions[0].error


def test_bare_unresolved_token_is_silent(repo: Path) -> None:
    """``@user`` / ``@email`` handles in prose are left as text, no warning."""
    r = expand_file_mentions("mail me @john.smith or @team", cwd=repo)
    assert r.blocks == []
    assert r.mentions == []


def test_email_in_sentence_is_not_a_mention(repo: Path) -> None:
    r = expand_file_mentions("contact foo@bar.com today", cwd=repo)
    assert r.mentions == []
    assert r.blocks == []


def test_trailing_period_is_sentence_punctuation(repo: Path) -> None:
    r = expand_file_mentions("see @src/foo.py.", cwd=repo)
    assert r.mentions[0].raw == "@src/foo.py"
    assert r.mentions[0].ok


def test_multiple_mentions_in_order(repo: Path) -> None:
    r = expand_file_mentions("@src/foo.py then @src/bar.py", cwd=repo)
    assert [type(b) for b in r.blocks] == [TextBlock, TextBlock]
    assert "def f(): pass" in r.blocks[0].text
    assert "x = 1" in r.blocks[1].text


def test_duplicate_mention_attached_once(repo: Path) -> None:
    r = expand_file_mentions("@src/foo.py and @src/foo.py again", cwd=repo)
    assert len(r.mentions) == 1
    assert len(r.blocks) == 1


def test_tilde_expands_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "probe.txt").write_text("home", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    r = expand_file_mentions("@~/probe.txt", cwd=tmp_path)
    assert r.mentions[0].ok
    assert r.mentions[0].path == (home / "probe.txt").resolve()


def test_absolute_path_used_as_is(repo: Path) -> None:
    target = repo / "src" / "foo.py"
    r = expand_file_mentions(f"see @{target}", cwd=repo)
    assert r.mentions[0].ok
    assert r.mentions[0].path == target.resolve()


def test_oversized_file_reports_error(repo: Path) -> None:
    from phoson_cli.attachments import MAX_ATTACHMENT_BYTES

    big = repo / "big.txt"
    big.write_bytes(b"\0" * (MAX_ATTACHMENT_BYTES + 1))
    r = expand_file_mentions("@big.txt", cwd=repo)
    assert r.blocks == []
    assert r.mentions[0].ok is False
    assert "too large" in r.mentions[0].error


def test_text_file_larger_than_inline_cap_is_truncated(repo: Path) -> None:
    big = repo / "big.txt"
    big.write_text("A" * 40_000, encoding="utf-8")
    r = expand_file_mentions("@big.txt", cwd=repo)
    assert isinstance(r.blocks[0], TextBlock)
    assert "truncated" in r.blocks[0].text
    assert "head" in r.blocks[0].text and "tail" in r.blocks[0].text
    assert len(r.blocks[0].text) < 40_000


def test_mention_count_cap_truncates(repo: Path) -> None:
    for c in "abcdefghijkl":  # 12 real files
        (repo / f"f_{c}.py").write_text(c, encoding="utf-8")
    text = " ".join(f"@f_{c}.py" for c in "abcdefghijkl")
    r = expand_file_mentions(text, cwd=repo)
    assert r.truncated is True
    assert len(r.mentions) == MAX_MENTIONS_PER_MESSAGE


def test_no_mentions_in_plain_text(repo: Path) -> None:
    r = expand_file_mentions("just a normal message", cwd=repo)
    assert r.blocks == [] and r.mentions == [] and r.truncated is False


def test_expand_accepts_string_cwd(repo: Path) -> None:
    r = expand_file_mentions("@src/foo.py", cwd=str(repo))
    assert r.mentions[0].ok


# ── 3. PathCompleter ─────────────────────────────────────────────────────────


def test_completer_offers_repo_paths(repo: Path) -> None:
    c = PathCompleter(repo)
    results = _complete(c, "@")
    texts = [t for t, _, _ in results]
    assert "src/foo.py" in texts
    assert "README.md" in texts
    assert "src/" in texts  # directories offered for navigation


def test_completer_filters_by_query(repo: Path) -> None:
    c = PathCompleter(repo)
    assert [t for t, _, _ in _complete(c, "@src/fo")] == ["src/foo.py"]
    assert [t for t, _, _ in _complete(c, "@src/ba")] == ["src/bar.py"]


def test_completer_start_position_replaces_query(repo: Path) -> None:
    c = PathCompleter(repo)
    for text, start, _ in _complete(c, "@src/fo"):
        assert text == "src/foo.py"
        assert start == -6  # length of "src/fo"


def test_completer_works_mid_sentence(repo: Path) -> None:
    c = PathCompleter(repo)
    assert [t for t, _, _ in _complete(c, "fix @src/f")] == ["src/foo.py"]


def test_completer_ignores_email_and_bare_handles(repo: Path) -> None:
    c = PathCompleter(repo)
    assert _complete(c, "email a@b.com") == []
    assert _complete(c, "@user") == []  # no such file → nothing offered


def test_completer_ignores_slash_commands(repo: Path) -> None:
    c = PathCompleter(repo)
    assert _complete(c, "/model") == []
    assert _complete(c, "/sessions load ") == []


def test_completer_no_match_returns_empty(repo: Path) -> None:
    c = PathCompleter(repo)
    assert _complete(c, "@src/doesnotexist") == []


def test_completer_shows_size_hint_for_files(repo: Path) -> None:
    c = PathCompleter(repo)
    by_text = {t: meta for t, _, meta in _complete(c, "@")}
    # Files carry a size hint; the dir entry does not.
    assert by_text["src/foo.py"].endswith("B")
    assert by_text["src/"] == ""


def test_completer_walks_lazily_once(repo: Path) -> None:
    c = PathCompleter(repo)
    assert c._candidates == []  # not walked yet
    _complete(c, "@")
    first = list(c._candidates)
    assert first  # walked
    _complete(c, "@src")  # subsequent query uses the cached walk
    assert c._candidates == first


def test_completer_skips_hidden_and_ignored(repo: Path) -> None:
    c = PathCompleter(repo)
    texts = [t for t, _, _ in _complete(c, "@")]
    assert not any("node_modules" in t for t in texts)
    assert not any(t.startswith(".") or t.startswith(".git") for t in texts)


def test_completer_defaults_to_cwd(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.chdir(repo)
    c = PathCompleter()  # no explicit cwd → Path.cwd()
    assert "src/foo.py" in [t for t, _, _ in _complete(c, "@")]


# ── 4. controller wiring: _build_user_message expands mentions ──────────────


class _NotifySink:
    """Minimal sink recording notify() calls (the mention feedback path).

    Only the methods the controller touches during construction and
    ``_build_user_message`` are real; the rest are inert stubs.
    """

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.session_ids: list[str] = []

    def notify(self, kind: str, message: str) -> None:
        self.notifications.append((kind, message))

    def set_session(self, session_id: str) -> None:
        self.session_ids.append(session_id)

    def on_attachments(self, sources: list[str]) -> None:  # noqa: ARG002
        pass

    def on_subagent_progress(self, progress: object | None) -> None:  # noqa: ARG002
        pass


def _make_controller(tmp_path: Path) -> tuple[SessionController, _NotifySink]:
    from unittest.mock import MagicMock, patch

    sink = _NotifySink()
    config = PhosonConfig(provider="ollama", model="m", sessions_dir=tmp_path)
    with patch("phoson_cli.controller.build_chat", return_value=MagicMock()):
        return SessionController(config, sink), sink  # type: ignore[arg-type]


def test_controller_inlines_mentioned_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    controller, _sink = _make_controller(repo)
    msg = controller._build_user_message("fix @src/foo.py")

    assert msg.role == "user"
    blocks = list(msg.content)
    texts = [b for b in blocks if isinstance(b, TextBlock)]
    # The raw user text is preserved (mention stays visible) plus the file.
    assert any("fix @src/foo.py" in b.text for b in texts)
    assert any("def f(): pass" in b.text for b in texts)


def test_controller_attaches_image_mention(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    controller, _sink = _make_controller(repo)
    msg = controller._build_user_message("look @img.png")
    blocks = list(msg.content)
    assert any(isinstance(b, ImageBlock) for b in blocks)


def test_controller_reports_attached_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    controller, sink = _make_controller(repo)
    controller._build_user_message("fix @src/foo.py")
    kinds = [k for k, _ in sink.notifications]
    assert "info" in kinds
    assert any("Attached:" in m for _, m in sink.notifications)


def test_controller_warns_on_missing_mention(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    controller, sink = _make_controller(repo)
    controller._build_user_message("see @nope/missing.py")
    assert any(k == "warn" and "file not found" in m for k, m in sink.notifications)


def test_controller_silent_for_bare_handle(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An @user handle in prose must not produce a spurious warning."""
    monkeypatch.chdir(repo)
    controller, sink = _make_controller(repo)
    controller._build_user_message("ping @team")
    assert sink.notifications == []


def test_controller_plain_text_unchanged(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    controller, sink = _make_controller(repo)
    msg = controller._build_user_message("hello world")
    # No mentions → a single TextBlock with the raw text, no notifications.
    blocks = list(msg.content)
    assert [type(b) for b in blocks] == [TextBlock]
    assert blocks[0].text == "hello world"
    assert sink.notifications == []


def test_controller_combines_attach_and_mention(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    controller, _sink = _make_controller(repo)
    controller.attachments.attach(str(repo / "img.png"))
    msg = controller._build_user_message("see @src/foo.py")
    blocks = list(msg.content)
    # Both the /attach image block and the mentioned file text are present.
    assert any(isinstance(b, ImageBlock) for b in blocks)
    assert any(isinstance(b, TextBlock) and "def f(): pass" in b.text for b in blocks)


def test_controller_message_content_type_union(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    controller, _sink = _make_controller(repo)
    msg = controller._build_user_message("fix @src/foo.py")
    assert isinstance(msg, Message)
    assert msg.content is not None
