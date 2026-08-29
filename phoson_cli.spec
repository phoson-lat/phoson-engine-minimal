# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone phoson-cli binary (issue #93).

Build (from the repo root):
    uv pip install pyinstaller
    PHOSON_VERSION=0.15.0 pyinstaller phoson_cli.spec

The spec does two things on top of a bare entry-point build:

1. **Data assets.** ``phos-ascii.txt`` (banner art) is staged into the
   bundle under ``phoson_cli/`` where ``phoson_cli._frozen.asset_path``
   looks.
2. **Version injection.** ``PHOSON_VERSION=X.Y.Z`` (exported by the
   release workflow from the git tag) is written to
   ``phoson_cli/_frozen_version.txt`` in the bundle;
   ``updater.get_current_version()`` →
   :func:`phoson_cli._frozen.frozen_version` reads it, because a bundle
   does not ship the package's ``.dist-info``.

The version arrives via environment, NOT a CLI flag: PyInstaller
parses its own command line (where ``-v/--version`` is its reserved
"show version" flag) *before* the spec runs, so a ``--version=...``
argument would be rejected.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# ── Flags ─────────────────────────────────────────────────────────────────────
VERSION = os.environ.get("PHOSON_VERSION") or None

ROOT = Path(SPECPATH)
DIST_NAME = "phoson-cli"

# ── Hidden imports ────────────────────────────────────────────────────────────
# Provider/plugin SDKs are imported lazily (e.g. ``from google import
# genai`` inside gemini.py), so PyInstaller's static analysis misses
# them. ``collect_submodules`` picks up whatever is installed in the
# build env; a package that is absent simply contributes nothing.
HIDDEN_IMPORTS = []
for pkg in (
    # Core
    "anthropic",
    "openai",
    "httpx",
    "tiktoken",
    "rich",
    "prompt_toolkit",
    # Optional provider extras (I-113 catalog) — included when present
    # so the binary works for those providers out of the box.
    "google.genai",
    "mistralai",
    "boto3",
    # Optional plugin extras.
    "mcp",
    "asyncpg",
    "redis",
    "qdrant_client",
):
    try:
        HIDDEN_IMPORTS.extend(collect_submodules(pkg))
    except Exception:  # noqa: BLE001 — extra simply absent in build env
        pass

# First-party packages are imported dynamically by the plugin loader
# (``importlib.import_module``), which static analysis cannot see.
for pkg in (
    "phoson_agent",
    "phoson_llm",
    "phoson_cli",
    "phoson_plugin_checkpoint",
    "phoson_plugin_mcp",
    "phoson_plugin_memory",
):
    HIDDEN_IMPORTS.extend(collect_submodules(pkg))

# ── Data files ────────────────────────────────────────────────────────────────
DATA_FILES = [("phoson_cli/phos-ascii.txt", "phoson_cli")]

VERSION_FILE = None
if VERSION:
    # Stage the version file in a throwaway dir (outside the package so
    # a local build never dirties the tree); it lands in the bundle as
    # phoson_cli/_frozen_version.txt.
    VERSION_FILE = ROOT / "build" / "_frozen_version.txt"
    VERSION_FILE.parent.mkdir(exist_ok=True)
    VERSION_FILE.write_text(VERSION + "\n", encoding="utf-8")
    DATA_FILES.append((str(VERSION_FILE), "phoson_cli"))

# ── Analysis / build ──────────────────────────────────────────────────────────
a = Analysis(
    ["phoson_cli/__main__.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATA_FILES,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    runtime_hooks=[],
    # Keep the bundle small: no GUI/plot/test stacks, and tiktoken's
    # BPE files are runtime data we must NOT exclude (see note below).
    excludes=["tkinter", "matplotlib", "PIL", "pytest", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=DIST_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

# ── Cleanup ───────────────────────────────────────────────────────────────────
# Drop the temp version file so it never lands in a git status after a
# local build.
if VERSION_FILE is not None:
    VERSION_FILE.unlink(missing_ok=True)
