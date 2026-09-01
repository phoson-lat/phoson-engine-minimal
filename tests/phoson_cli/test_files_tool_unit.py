"""Unit tests for phoson_cli.tools.files — read, write, patch, list_dir."""

from phoson_cli.tools.files import _list_dir, _read_file, _patch_file, _write_file


class TestReadFile:
    def test_reads_full_content(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")

        result = _read_file(str(f))

        assert result == "line1\nline2\nline3\n"

    def test_returns_error_when_not_found(self, tmp_path):
        result = _read_file(str(tmp_path / "missing.txt"))

        assert "not found" in result.lower()

    def test_truncates_large_files(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_bytes(b"x" * (51 * 1024))

        result = _read_file(str(f))

        assert "truncated" in result

    def test_line_range(self, tmp_path):
        f = tmp_path / "lines.txt"
        f.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")

        result = _read_file(str(f), start_line=2, end_line=4)

        assert result == "b\nc\nd\n"

    def test_start_line_beyond_file(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("only one line\n", encoding="utf-8")

        result = _read_file(str(f), start_line=99)

        assert "beyond" in result


class TestWriteFile:
    def test_creates_file(self, tmp_path):
        path = str(tmp_path / "new.txt")

        result = _write_file(path, "hello")

        assert "Created" in result  # T-7: created vs updated verb
        assert (tmp_path / "new.txt").read_text() == "hello"

    def test_overwrite_says_updated(self, tmp_path):
        path = str(tmp_path / "existing.txt")
        (tmp_path / "existing.txt").write_text("old")

        assert "Updated" in _write_file(path, "new")
        assert "Updated" in _write_file(path, "newer")

    def test_creates_parent_directories(self, tmp_path):
        path = str(tmp_path / "a" / "b" / "c.txt")

        _write_file(path, "deep")

        assert (tmp_path / "a" / "b" / "c.txt").exists()

    def test_overwrites_existing_file(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("old", encoding="utf-8")

        _write_file(str(f), "new")

        assert f.read_text() == "new"

    def test_returns_byte_count(self, tmp_path):
        result = _write_file(str(tmp_path / "f.txt"), "abc")

        assert "3 bytes" in result


class TestPatchFile:
    def test_replaces_first_occurrence(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("foo foo foo", encoding="utf-8")

        result = _patch_file(str(f), "foo", "bar")

        assert f.read_text() == "bar foo foo"
        assert "1 occurrence" in result

    def test_replace_all(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("foo foo foo", encoding="utf-8")

        result = _patch_file(str(f), "foo", "bar", replace_all=True)

        assert f.read_text() == "bar bar bar"
        assert "3 occurrence" in result

    def test_returns_error_when_old_not_found(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("hello world", encoding="utf-8")

        result = _patch_file(str(f), "missing", "replacement")

        assert "not found" in result
        assert f.read_text() == "hello world"

    def test_returns_error_when_file_missing(self, tmp_path):
        result = _patch_file(str(tmp_path / "ghost.py"), "x", "y")

        assert "not found" in result.lower()


class TestListDir:
    def test_lists_files_in_dir(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")

        result = _list_dir(str(tmp_path))

        assert "a.txt" in result
        assert "b.txt" in result

    def test_skips_pycache(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "mod.pyc").write_bytes(b"")

        result = _list_dir(str(tmp_path))

        assert "__pycache__" not in result

    def test_skips_git(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")

        result = _list_dir(str(tmp_path))

        assert ".git" not in result

    def test_returns_error_for_missing_path(self, tmp_path):
        result = _list_dir(str(tmp_path / "nope"))

        assert "not found" in result.lower()

    def test_returns_error_for_file(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")

        result = _list_dir(str(f))

        assert "not a directory" in result.lower()

    def test_max_depth_three(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "hidden.txt").write_text("x")

        result = _list_dir(str(tmp_path))

        assert "hidden.txt" not in result
