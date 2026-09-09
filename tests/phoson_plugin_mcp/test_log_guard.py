"""Tests for MCP stderr routing + tool-schema sanitizer (issue #148).

Two concerns:

* ``_sanitize_tool_parameters`` coerces a broken ``inputSchema`` into the
  object shape providers validate, so one malformed tool no longer 400s the
  whole request.
* ``_open_errlog`` / ``_close_errlogs`` route each stdio server's stderr to a
  per-server log file instead of the terminal, and close it on teardown.
"""

from pathlib import Path

import pytest

try:
    from phoson_plugin_mcp import MCPPlugin
    from phoson_plugin_mcp._plugin import _sanitize_tool_parameters

    _IMPORT_OK = True
except ImportError as e:  # pragma: no cover - plugin always imports
    MCPPlugin = None
    _sanitize_tool_parameters = None
    _IMPORT_OK = False
    print(f"Warning: could not import MCP plugin: {e}")


needs_plugin = pytest.mark.skipif(not _IMPORT_OK, reason="mcp plugin not importable")


@needs_plugin
class TestSanitizeToolParameters:
    def test_valid_schema_passes_through(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
        }
        assert _sanitize_tool_parameters(schema) == schema

    def test_missing_properties_becomes_empty_object(self):
        out = _sanitize_tool_parameters({"type": "object"})
        assert out["properties"] == {}

    def test_non_object_type_is_coerced(self):
        out = _sanitize_tool_parameters({"type": "array"})
        assert out["type"] == "object"

    def test_non_dict_properties_becomes_empty_object(self):
        out = _sanitize_tool_parameters({"type": "object", "properties": "oops"})
        assert out["properties"] == {}

    def test_dangling_required_entries_are_dropped(self):
        out = _sanitize_tool_parameters(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a", "b"],
            }
        )
        assert out["required"] == ["a"]

    def test_required_without_properties_is_dropped(self):
        # The flagged case: `required` present, `properties` absent. Every
        # required entry is dangling, so the key is dropped (not "required": []).
        out = _sanitize_tool_parameters({"type": "object", "required": ["a"]})
        assert out["properties"] == {}
        assert "required" not in out

    def test_non_list_required_is_dropped(self):
        out = _sanitize_tool_parameters(
            {"type": "object", "properties": {"a": {}}, "required": "a"}
        )
        assert "required" not in out

    def test_dollar_schema_is_stripped(self):
        out = _sanitize_tool_parameters(
            {"$schema": "http://json-schema.org/draft-07", "type": "object"}
        )
        assert "$schema" not in out

    def test_input_schema_is_not_mutated(self):
        schema = {"type": "object", "required": ["a"]}
        snapshot = dict(schema)
        _sanitize_tool_parameters(schema)
        assert schema == snapshot


@needs_plugin
class TestErrlogRouting:
    def _patch_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    def test_open_errlog_creates_file_and_reuses_it(self, tmp_path, monkeypatch):
        self._patch_home(monkeypatch, tmp_path)
        p = MCPPlugin()
        fh1 = p._open_errlog("dokploy-mcp")
        fh2 = p._open_errlog("dokploy-mcp")
        assert fh1 is fh2  # one sink per server, reused
        expected = tmp_path / ".phoson" / "logs" / "mcp" / "dokploy-mcp.log"
        assert Path(fh1.name) == expected
        fh1.write("hello\n")
        fh1.flush()
        p._close_errlogs()
        assert fh1.closed
        assert "hello" in expected.read_text(encoding="utf-8")

    def test_close_errlogs_closes_all_and_clears(self, tmp_path, monkeypatch):
        self._patch_home(monkeypatch, tmp_path)
        p = MCPPlugin()
        a = p._open_errlog("server-a")
        b = p._open_errlog("server-b")
        assert a is not b
        p._close_errlogs()
        assert a.closed and b.closed
        assert p._errlog_files == {}

    def test_server_name_is_sanitized_in_filename(self, tmp_path, monkeypatch):
        self._patch_home(monkeypatch, tmp_path)
        p = MCPPlugin()
        fh = p._open_errlog("weird//name: with spaces")
        try:
            name = Path(fh.name).name
            assert "/" not in name and " " not in name
        finally:
            p._close_errlogs()
