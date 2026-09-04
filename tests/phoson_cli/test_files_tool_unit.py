"""Unit tests for phoson_cli.tools.files — read, write, patch, list_dir.

Covers the ACI contract (F-20/#179, F-21a/F-21b/#180, F-26):

- ``read_file`` renders cat -n (1-based numbers), caps full reads *and*
  ranges, and tells the model the exact next range to request.
- ``patch_file`` refuses an ambiguous anchor (writes nothing) and hints at
  the closest line / a CRLF-LF mismatch when the anchor is absent.
- ``list_dir`` caps entries and skips common noise dirs.
"""

from phoson_cli.tools.files import _list_dir, _read_file, _patch_file, _write_file


class TestReadFile:
    def test_reads_full_content_with_line_numbers(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")

        result = _read_file(str(f))

        # cat -n: 6-wide right-aligned number + tab + line, 1-based.
        assert result == "     1\tline1\n     2\tline2\n     3\tline3"

    def test_returns_error_when_not_found(self, tmp_path):
        result = _read_file(str(tmp_path / "missing.txt"))

        assert "not found" in result.lower()

    def test_truncates_large_files_with_next_range(self, tmp_path):
        # ~7000 short lines: renders to well over the 50KB cap.
        f = tmp_path / "big.txt"
        f.write_text(
            "\n".join(f"line-{i:05d}" for i in range(1, 7001)) + "\n", encoding="utf-8"
        )

        result = _read_file(str(f))

        assert "truncated" in result
        # The note must name the next range to request.
        assert "start_line=" in result
        # First line is still shown, numbered.
        assert result.startswith("     1\tline-00001")

    def test_line_range_is_numbered_from_start(self, tmp_path):
        f = tmp_path / "lines.txt"
        f.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")

        result = _read_file(str(f), start_line=2, end_line=4)

        assert result == "     2\tb\n     3\tc\n     4\td"

    def test_range_beyond_file_is_capped(self, tmp_path):
        # A range larger than the file must still be bounded to the file.
        f = tmp_path / "lines.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")

        result = _read_file(str(f), start_line=1, end_line=999)

        assert result == "     1\ta\n     2\tb\n     3\tc"

    def test_range_cap_applies_to_slices(self, tmp_path):
        # A slice can exceed the cap too; truncation still applies and the
        # next-range hint continues from within the slice.
        f = tmp_path / "big.txt"
        n = 7000
        f.write_text(
            "\n".join(f"line-{i:05d}" for i in range(1, n + 1)) + "\n", encoding="utf-8"
        )

        result = _read_file(str(f), start_line=1, end_line=n)

        assert "truncated" in result
        assert "start_line=" in result
        assert result.startswith("     1\tline-00001")

    def test_start_line_beyond_file(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("only one line\n", encoding="utf-8")

        result = _read_file(str(f), start_line=99)

        assert "beyond" in result

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")

        assert "empty file" in _read_file(str(f))

    def test_non_utf8_is_an_actionable_error(self, tmp_path):
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\x80\xff\xfe binary \x00\x01")

        result = _read_file(str(f))

        assert "not valid UTF-8" in result
        assert "bash" in result  # suggests iconv / treat as binary


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
    def test_replaces_single_occurrence(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("foo once", encoding="utf-8")

        result = _patch_file(str(f), "foo", "bar")

        assert f.read_text() == "bar once"
        assert "1 occurrence" in result

    def test_replace_all(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("foo foo foo", encoding="utf-8")

        result = _patch_file(str(f), "foo", "bar", replace_all=True)

        assert f.read_text() == "bar bar bar"
        assert "3 occurrence" in result

    def test_ambiguous_anchor_fails_and_writes_nothing(self, tmp_path):
        """#179/F-20: more than one match without replace_all is an error."""
        f = tmp_path / "code.py"
        f.write_text("a\nfoo\nb\nfoo\nc\n", encoding="utf-8")
        original = f.read_text()

        result = _patch_file(str(f), "foo", "bar")

        assert "matches 2 times" in result
        # The error names the matching lines so the model can disambiguate.
        assert "2" in result and "4" in result
        assert "replace_all" in result
        # Nothing was written.
        assert f.read_text() == original

    def test_ambiguous_multiline_anchor_lists_lines(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            "def f():\n    return None\n\ndef g():\n    return None\n",
            encoding="utf-8",
        )

        result = _patch_file(str(f), "    return None", "    pass")

        assert "matches 2 times" in result
        assert "2" in result and "5" in result
        assert f.read_text().count("return None") == 2  # untouched

    def test_unique_anchor_in_a_repeat_file_works(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("foo\nfoo\nunique-needle\nfoo\n", encoding="utf-8")

        result = _patch_file(str(f), "unique-needle", "REPLACED")

        assert "1 occurrence" in result
        assert f.read_text() == "foo\nfoo\nREPLACED\nfoo\n"

    def test_returns_error_when_old_not_found(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("hello world", encoding="utf-8")

        result = _patch_file(str(f), "missing", "replacement")

        assert "not found" in result
        assert f.read_text() == "hello world"

    def test_not_found_suggests_closest_line(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def main():\n    x = 1\n    return x\n", encoding="utf-8")

        # The needle differs by one token — closest-line hint should appear.
        result = _patch_file(str(f), "    x = 2", "    x = 3")

        assert "not found" in result
        assert "closest line" in result

    def test_crlf_anchor_matches_and_preserves_line_endings(self, tmp_path):
        """F-20/#179: an LF anchor against a CRLF file matches (endings are
        normalised to the file's) and the patch preserves CRLF elsewhere —
        the old code read universal-newlines and wrote back, turning a CRLF
        file into LF."""
        f = tmp_path / "crlf.txt"
        f.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

        # Anchor is the same words with LF endings.
        result = _patch_file(str(f), "alpha\nbeta", "ALPHA")

        assert "1 occurrence" in result
        # CRLF preserved on the untouched line, and the replaced block
        # reuses the file's own ending.
        assert f.read_bytes() == b"ALPHA\r\ngamma\r\n"

    def test_crlf_file_not_whole_file_rewritten_to_lf(self, tmp_path):
        f = tmp_path / "crlf.txt"
        f.write_bytes(b"one\r\ntwo\r\nthree\r\n")

        _patch_file(str(f), "two", "TWO")

        # Every untouched line keeps its CRLF.
        assert f.read_bytes() == b"one\r\nTWO\r\nthree\r\n"

    def test_returns_error_when_file_missing(self, tmp_path):
        result = _patch_file(str(tmp_path / "ghost.py"), "x", "y")

        assert "not found" in result.lower()

    def test_non_utf8_file_is_not_written(self, tmp_path):
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\xff\xfe\x80 not utf8")
        original = f.read_bytes()

        result = _patch_file(str(f), "anything", "y")

        assert "not valid UTF-8" in result
        assert f.read_bytes() == original


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

    def test_caps_entries(self, tmp_path):
        # 600 files: the listing must stop at the cap and say so.
        from phoson_cli.tools.files import LIST_DIR_MAX_ENTRIES

        for i in range(600):
            (tmp_path / f"f{i:03d}.txt").write_text(str(i))

        result = _list_dir(str(tmp_path))

        assert f"listing stopped at {LIST_DIR_MAX_ENTRIES} entries" in result
        # The first entry is present, a later one beyond the cap is not.
        assert "f000.txt" in result
        assert "f599.txt" not in result

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
