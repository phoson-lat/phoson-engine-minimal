import json
import hashlib

from phoson_plugin_mcp._plugin import _project_mcp_is_trusted


def test_project_trust_binds_full_config_and_canonical_root(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    spec = {"command": "node", "args": ["server.js"], "env": {"TOKEN": "a"}}
    digest = hashlib.sha256(
        json.dumps(
            spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    trust = tmp_path / ".phoson" / "trusted-project-mcps.json"
    trust.parent.mkdir()
    trust.write_text(json.dumps({str(root.resolve()): {"srv": digest}}))

    assert _project_mcp_is_trusted(root, "srv", spec)
    assert not _project_mcp_is_trusted(root, "srv", {**spec, "args": ["other.js"]})
    assert not _project_mcp_is_trusted(root, "other", spec)
    assert not _project_mcp_is_trusted(tmp_path, "srv", spec)
