# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone phoson-cli binary (issue #93).

Build (from the repo root):
    uv pip install pyinstaller
    PHOSON_VERSION=0.15.0 pyinstaller phoson_cli.spec
    # -> dist/phoson-cli/ (launcher + _internal/), a one-folder bundle

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

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

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
    # Remote-access gateway (`phoson-cli serve`).
    "websockets",
):
    try:
        HIDDEN_IMPORTS.extend(collect_submodules(pkg))
    except Exception:  # noqa: BLE001 — extra simply absent in build env
        pass

# First-party packages are imported dynamically by the plugin loader
# (``importlib.import_module``), which static analysis cannot see.
# First-party packages, including EVERY official plugin: the CLI imports them
# dynamically (official_plugins.py), so the analysis cannot see them.
OFFICIAL_PLUGINS = sorted(p.name for p in ROOT.glob("phoson_plugin_*") if p.is_dir())
for pkg in ("phoson_agent", "phoson_llm", "phoson_cli", "phoson_bridge", *OFFICIAL_PLUGINS):
    HIDDEN_IMPORTS.extend(collect_submodules(pkg))

# tiktoken finds its encodings by scanning the *namespace package*
# ``tiktoken_ext`` with pkgutil.iter_modules. Neither is visible to the
# analysis, so without this every token count failed in the binary with
# "Unknown encoding cl100k_base. Plugins found: []". The module is bundled
# both as code and as a file on disk, which is what iter_modules scans.
HIDDEN_IMPORTS.extend(collect_submodules("tiktoken_ext"))

# ── Data files ────────────────────────────────────────────────────────────────
DATA_FILES = [("phoson_cli/phos-ascii.txt", "phoson_cli")]
DATA_FILES += collect_data_files("tiktoken_ext", include_py_files=True)

# Bundle the BPE ranks the CLI uses, so the binary never downloads them on
# first use (offline machines, corporate proxies). tiktoken caches each file
# under TIKTOKEN_CACHE_DIR; the runtime hook points that variable at this
# directory inside the bundle unless the user set it.
TIKTOKEN_ENCODINGS = ("cl100k_base", "o200k_base")
TIKTOKEN_CACHE = ROOT / "build" / "tiktoken_cache"
TIKTOKEN_CACHE.mkdir(parents=True, exist_ok=True)
os.environ["TIKTOKEN_CACHE_DIR"] = str(TIKTOKEN_CACHE)
import tiktoken  # noqa: E402 - needs TIKTOKEN_CACHE_DIR set first

for _encoding in TIKTOKEN_ENCODINGS:
    tiktoken.get_encoding(_encoding).encode("warm the cache")
_cached = [p for p in TIKTOKEN_CACHE.iterdir() if p.is_file()]
if len(_cached) < len(TIKTOKEN_ENCODINGS):
    raise SystemExit(f"tiktoken cache incomplete: {sorted(p.name for p in _cached)}")
DATA_FILES += [(str(p), "tiktoken_cache") for p in _cached]

# Official plugins ship a declarative phoson-plugin.toml manifest at their
# package root; the loader reads it before importing the plugin.
DATA_FILES += [
    (f"{package}/phoson-plugin.toml", package)
    for package in (
        "phoson_plugin_bgjobs",
        "phoson_plugin_checkpoint",
        "phoson_plugin_computeruse",
        "phoson_plugin_mcp",
        "phoson_plugin_memory",
        "phoson_plugin_monitor",
        "phoson_plugin_otel",
        "phoson_plugin_peers",
        "phoson_plugin_questions",
        "phoson_plugin_ssh",
        "phoson_plugin_stt",
        "phoson_plugin_swarm",
    )
]

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
    runtime_hooks=[str(ROOT / "scripts" / "pyi_rth_phoson.py")],
    # Keep the bundle small: no GUI/plot/test stacks, and tiktoken's
    # BPE files are runtime data we must NOT exclude (see note below).
    excludes=["tkinter", "matplotlib", "PIL", "pytest", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# One-folder ("onedir") bundle: dist/phoson-cli/ holds the launcher plus an
# _internal/ directory with the interpreter, libraries and data. A onefile
# build re-extracts ~65 MB to a temp dir on EVERY launch (measured 1.9-3.9 s
# for `--version` vs ~0.2 s installed) and keeps a second bootloader process
# alive on Unix; onedir is unpacked once, at install time. The release
# workflow archives this folder (.tar.gz / .zip) for the installers.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=DIST_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=DIST_NAME,
)

# ── Cleanup ───────────────────────────────────────────────────────────────────
# Drop the temp version file so it never lands in a git status after a
# local build.
if VERSION_FILE is not None:
    VERSION_FILE.unlink(missing_ok=True)
