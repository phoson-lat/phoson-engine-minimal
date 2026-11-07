"""PyInstaller runtime hook for the phoson-cli bundle (see phoson_cli.spec).

Runs before any application code. Points tiktoken at the BPE ranks bundled
in ``<bundle>/tiktoken_cache`` so token counting works offline and without a
first-use download; a user-provided ``TIKTOKEN_CACHE_DIR`` still wins.
"""

import os
import sys

_bundle = getattr(sys, "_MEIPASS", None)
if _bundle:
    _cache = os.path.join(_bundle, "tiktoken_cache")
    if os.path.isdir(_cache):
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", _cache)
