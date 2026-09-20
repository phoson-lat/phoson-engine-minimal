"""Guards for the lazy provider-SDK loading (PEP 562) in ``phoson_llm``.

The runtime checks run in a *clean subprocess*: the main test process has
already imported the vendor SDKs (adapter tests), so an in-process check
would be a false positive. See issue #243.
"""

import ast
import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Vendor SDKs the lazy layout must keep out of a plain ``import phoson_llm``.
VENDOR_SDKS = ("openai", "anthropic")

_SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".ruff_cache",
    ".pytest_cache",
}


def _loaded_sdks(code: str) -> set[str]:
    """Run ``code`` in a fresh interpreter; return the vendor SDKs it loaded."""
    probe = (
        "import sys\n"
        f"{code}\n"
        f"print(','.join(m for m in sys.modules if m in {VENDOR_SDKS!r}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {m for m in result.stdout.strip().split(",") if m}


class TestLazySdkLoading:
    def test_import_phoson_llm_loads_no_sdk(self) -> None:
        assert _loaded_sdks("import phoson_llm") == set()

    def test_import_schemas_submodule_loads_no_sdk(self) -> None:
        assert _loaded_sdks("import phoson_llm.schemas") == set()

    def test_chats_submodule_reachable_without_any_sdk(self) -> None:
        # Finding 4 of #243: ``phoson_llm.chats`` must be reachable without
        # pulling an adapter SDK (the factory binds it with a cheap import).
        assert _loaded_sdks("import phoson_llm\nassert phoson_llm.chats") == set()

    def test_accessing_openai_adapter_loads_only_openai(self) -> None:
        assert _loaded_sdks("import phoson_llm\nphoson_llm.OpenAIChat") == {"openai"}

    def test_accessing_anthropic_adapter_loads_only_anthropic(self) -> None:
        assert _loaded_sdks("import phoson_llm\nphoson_llm.AnthropicChat") == {
            "anthropic"
        }


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def test_no_star_import_of_phoson_llm() -> None:
    """``from phoson_llm import *`` would defeat the lazy layout (#243).

    A star-import walks ``__all__`` and touches ``__getattr__`` for every
    adapter, importing all vendor SDKs at once. This AST check (not a text
    scan, so the docstring warning does not match itself) fails if the
    pattern is reintroduced anywhere in the source.
    """
    offenders: list[str] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "phoson_llm"
                and any(alias.name == "*" for alias in node.names)
            ):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"from phoson_llm import * found in: {offenders}"
