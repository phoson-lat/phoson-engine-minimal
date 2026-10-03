"""Regression guard: importing the full-screen app must not load rich.markdown.

``rich.markdown`` drags in ``markdown_it`` (a sizeable pure-Python parser) and
is only needed when a turn is actually rendered as Markdown. Both
``phoson_cli.formatting`` and ``phoson_cli.renderer`` therefore import it
*lazily*, inside the functions that use it, so a full-screen ``app`` import
(which pulls in ``repl`` → ``renderer``/``formatting``) stays free of it.

The check runs in a *clean subprocess*: the main pytest process has already
imported ``rich.markdown`` (via the rendering tests), so an in-process
``sys.modules`` probe would be a false positive.
"""

import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Modules that must stay out of the full-screen app's import graph.
MARKDOWN_MODULES = ("rich.markdown", "markdown_it")


def _run_clean(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def _loaded_markdown_modules(code: str) -> set[str]:
    """Import ``code`` in a fresh interpreter; return the markdown modules loaded."""
    probe = (
        "import sys\n"
        f"{code}\n"
        f"print(','.join(m for m in sys.modules if m in {MARKDOWN_MODULES!r}))\n"
    )
    result = _run_clean(probe)
    assert result.returncode == 0, result.stderr
    return {m for m in result.stdout.strip().split(",") if m}


class TestFullScreenAppStaysMarkdownFree:
    def test_importing_fullscreen_app_loads_no_markdown(self) -> None:
        assert _loaded_markdown_modules("import phoson_cli.fullscreen.app") == set()

    def test_importing_formatting_and_renderer_loads_no_markdown(self) -> None:
        # The two modules that own the lazy imports must stay clean on their own.
        code = "import phoson_cli.formatting, phoson_cli.renderer"
        assert _loaded_markdown_modules(code) == set()


class TestProbeDetectsMarkdown:
    """Control: the probe is not trivially green — a real render loads it."""

    def test_rendering_markdown_loads_rich_markdown(self) -> None:
        code = (
            "from phoson_cli.formatting import render_streaming_panel\n"
            "from phoson_cli.theme import load_theme\n"
            "render_streaming_panel('**hi**', '', False, load_theme())\n"
        )
        assert _loaded_markdown_modules(code) == {"rich.markdown", "markdown_it"}
