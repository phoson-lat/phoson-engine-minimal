"""Runtime helpers for the standalone PyInstaller binary (issue #93).

The prebuilt ``phoson-cli`` binary (GitHub Releases) bundles the package
without the source tree. Two consequences need runtime handling:

- **Assets.** Files that live next to the package (``phos-ascii.txt``)
  may be staged by PyInstaller under the temporary bundle dir
  (``sys._MEIPASS``) instead of next to ``__file__``.
  :func:`asset_path` resolves both layouts.
- **Identity.** A bundle does not ship the package's ``.dist-info``, so
  ``importlib.metadata`` cannot report the version from inside the
  binary. The build injects ``_frozen_version.txt`` into the bundle
  (see ``phoson_cli.spec``); :func:`frozen_version` reads it.
  :func:`is_frozen` reports whether the process is a frozen binary
  (``sys.frozen``, set by PyInstaller — tests can plant it to simulate
  the binary layout).
"""

import sys
from pathlib import Path

_VERSION_FILE = "_frozen_version.txt"


def asset_path(name: str) -> Path:
    """Locate a package asset by name (e.g. ``"phos-ascii.txt"``).

    Resolution order:
    1. ``sys._MEIPASS/phoson_cli/<name>`` — PyInstaller onefile staging.
    2. Next to this module — source checkouts and onedir bundles
       (where the package keeps its normal layout).

    Raises:
        FileNotFoundError: If the asset exists in neither location.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "phoson_cli" / name
        if candidate.exists():
            return candidate
    candidate = Path(__file__).parent / name
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"Package asset not found: {name}")


def is_frozen() -> bool:
    """True when running inside a frozen (standalone) binary.

    PyInstaller sets ``sys.frozen`` in the generated executable; tests
    can plant the same attribute (or run ``pyinstaller`` themselves) to
    exercise the frozen code paths.
    """
    return bool(getattr(sys, "frozen", False))


def frozen_version(fallback: str) -> str:
    """Version injected at build time, or ``fallback``.

    The build spec writes ``phoson_cli/_frozen_version.txt`` into the
    bundle with the release version. Absent in source checkouts (and
    in tests that did not plant one) → the fallback — usually the
    ``importlib.metadata`` version or ``"dev"`` — is returned.
    """
    try:
        raw = asset_path(_VERSION_FILE).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return fallback
    return raw or fallback


__all__ = ["asset_path", "frozen_version", "is_frozen"]
