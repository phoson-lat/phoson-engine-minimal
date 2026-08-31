"""Decoupling tests: the in-tree plugins must not depend on the CLI (I-126).

The design contract for ``phoson_plugin_monitor`` / ``phoson_plugin_mcp`` is
that they depend ONLY on stdlib + the ``phoson_agent`` public contract, so an
embedded host (e.g. Phoson-Core) can use them without importing ``phoson_cli``
or dragging in its TUI stack (``prompt_toolkit``/``rich``).

The dynamic "does not drag the TUI stack" checks run in a *clean subprocess*
because the main test process has already loaded ``prompt_toolkit`` (via the
controller/repl tests) and would give a false positive.
"""

import sys
import subprocess
from pathlib import Path

import pytest

PLUGIN_PACKAGES = ["phoson_plugin_monitor", "phoson_plugin_mcp"]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _clean_subprocess_import_drag(prompt_filter: str, import_expr: str) -> str:
    """Import ``import_expr`` in a fresh interpreter; return leaked UI modules."""
    code = (
        "import sys, importlib\n"
        f"importlib.import_module({import_expr!r})\n"
        f"leaked = [m for m in sys.modules if {prompt_filter}]\n"
        "print(','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    return result.stdout.strip()


class TestPluginPackageIsUiFree:
    @pytest.mark.parametrize("pkg", PLUGIN_PACKAGES)
    def test_importing_a_plugin_does_not_load_prompt_toolkit(self, pkg) -> None:
        """Importing either plugin must not load the TUI stack (prompt_toolkit)."""
        leaked = _clean_subprocess_import_drag(
            "m == 'prompt_toolkit' or m.startswith('prompt_toolkit.')", pkg
        )
        assert leaked == "", f"{pkg} dragged in the TUI stack: {leaked.split(',')[:3]}"


class TestNoReverseCouplingToCli:
    def test_plugins_never_reference_phoson_cli(self) -> None:
        """Static guarantee: no plugin module imports/references phoson_cli."""
        import importlib

        for pkg in PLUGIN_PACKAGES:
            module = importlib.import_module(pkg)
            root = Path(module.__file__).parent
            for source in root.rglob("*.py"):
                text = source.read_text(encoding="utf-8")
                assert "phoson_cli" not in text, (
                    f"{pkg}/{source.name} references phoson_cli "
                    "(reverse coupling: plugins must not know about the CLI)"
                )

    def test_cli_config_is_importable_without_the_tui_stack(self) -> None:
        """The example host only needs ``phoson_cli.config``; that import must
        be TUI-free now (PEP 562 lazy ``PhosonRepl`` in the package ``__init__``)."""
        leaked = _clean_subprocess_import_drag(
            "m == 'prompt_toolkit' or m.startswith('prompt_toolkit.')",
            "phoson_cli.config",
        )
        assert leaked == "", (
            "phoson_cli.config dragged in prompt_toolkit; the package __init__ "
            f"must keep the TUI lazy for UI-free hosts (leaked: {leaked})"
        )

    def test_phoson_repl_still_exported_at_package_level(self) -> None:
        """Backwards compat: `from phoson_cli import PhosonRepl` must keep working
        despite the lazy (PEP 562) re-export."""
        from phoson_cli import PhosonRepl

        assert PhosonRepl.__name__ == "PhosonRepl"


class TestMcpFallback:
    """build_mcp_plugins shares the CWD-independent, crash-proof fallback."""

    def _config(self, tmp_path):
        from phoson_cli.config import PhosonConfig

        return PhosonConfig(
            provider="ollama",
            model="m",
            enable_mcp=True,
            mcp_config_file=tmp_path / "mcp.json",
        )

    def test_import_error_falls_back_to_in_tree_path_spec(self, tmp_path, monkeypatch):
        from phoson_cli.session_utils import build_mcp_plugins, _in_tree_plugin_path

        monkeypatch.setitem(sys.modules, "phoson_plugin_mcp", None)
        config = self._config(tmp_path)
        expected = _in_tree_plugin_path("phoson_plugin_mcp")
        specs = build_mcp_plugins(config)
        assert specs == [
            {
                "name": f"path:{expected}",
                "config": {
                    "config_file": str(tmp_path / "mcp.json"),
                    "tool_name_prefix": "mcp",
                },
            }
        ]
        assert Path(specs[0]["name"].removeprefix("path:")).is_absolute()

    def test_import_error_and_missing_file_warns_and_returns_empty(
        self, tmp_path, monkeypatch
    ):
        from phoson_cli.session_utils import build_mcp_plugins

        monkeypatch.setitem(sys.modules, "phoson_plugin_mcp", None)
        monkeypatch.setattr(
            "phoson_cli.session_utils._in_tree_plugin_path",
            lambda package: tmp_path / "gone" / "_plugin.py",
        )
        with pytest.warns(UserWarning, match="MCP disabled"):
            assert build_mcp_plugins(self._config(tmp_path)) == []
