"""Optional engine extras: resolve, install and enable them.

An engine plugin may depend on an **extra** of the ``phoson-engine-minimal``
package (``[stt]``, ``[computeruse]``…). Extras are optional dependencies that
are not installed by default: if they are missing, the engine loads the plugin
but **skips** it (without ``moonshine_voice`` dictation transcribes nothing).

This module lets them be fixed **from the app**:

1. **Resolves** the extra's requirements by reading the installed engine's
   metadata (``importlib.metadata``): there is no duplicated pin list here that
   could go stale; the pins are the engine's own.
2. **Installs** those requirements into ``~/.phoson/extras/<extra>`` — it does
   not touch the engine's environment: the frozen sidecar has no ``pip`` and
   the venv may be shared with other apps.
3. **Enables** the directory on the sidecar's ``sys.path``, so the plugin finds
   the module without restarting the app (and on the next launch, the
   ``server`` bootstrap adds them again).

The installer is ``pip`` (``python -m pip install --target``) or ``uv``
(``uv pip install --target``), whichever is found first with a real
interpreter: the frozen sidecar cannot execute its own ``sys.executable``.

If the machine has neither, the official ``uv`` binary (pinned version, verified
against its sha256) is downloaded to ``~/.phoson/tools/``: ``uv`` brings its own
Python of the sidecar's version if needed, so installation works on a machine
without Python. The app launched from Finder inherits launchd's minimal ``PATH``
(``/usr/bin:/bin:…``), so the usual locations (Homebrew, ``~/.local/bin``…)
are also searched beforehand.
"""

import os
import re
import sys
import shutil
import hashlib
import tarfile
import zipfile
import platform
import tempfile
import importlib
import subprocess
import importlib.util
from typing import Any
from pathlib import Path
from collections.abc import Callable

#: Engine distribution: the one that declares the extras in its metadata.
ENGINE_DIST = "phoson-engine-minimal"

#: Per-platform "sibling" extras: ``[computeruse]`` pulls in the system
#: backend (Quartz on macOS, GDI on Windows, Wayland on Linux).
_PLATFORM_SIBLINGS: dict[str, dict[str, str]] = {
    "computeruse": {"darwin": "computeruse-macos", "win32": "computeruse-windows"},
}

#: Installation cap (an `onnxruntime` download can take minutes).
_INSTALL_TIMEOUT = 900.0

#: `uv` version downloaded when the machine has no installer.
UV_VERSION = "0.13.0"

#: Official `uv` asset per platform → sha256 (published alongside the release).
#: Pinned here, not read from the network: a tampered asset fails verification.
_UV_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("darwin", "arm64"): (
        "uv-aarch64-apple-darwin.tar.gz",
        "a9c1b29002cf3c83f07fa9cd8a887a3be0107d90e23189721221e7257db8e3d6",
    ),
    ("darwin", "x86_64"): (
        "uv-x86_64-apple-darwin.tar.gz",
        "5f44dcbde809b632f47c36fadb241cb4d6f9af71d0c8f172b5d2026d3dde742c",
    ),
    ("linux", "x86_64"): (
        "uv-x86_64-unknown-linux-gnu.tar.gz",
        "1468ebd5a5541121837c5a2817b9972ba6090fa6caa3d142620850a47fb75154",
    ),
    ("linux", "aarch64"): (
        "uv-aarch64-unknown-linux-gnu.tar.gz",
        "3ccfb6af6e242433eb552f7d9676abd5412c6595c497e990d9c8cb7b5bd4d2c3",
    ),
    ("win32", "amd64"): (
        "uv-x86_64-pc-windows-msvc.zip",
        "088962f9e7b7bd9ea740c04c650b2a21c8928c345bd99ac24350dc924dba656c",
    ),
    ("win32", "arm64"): (
        "uv-aarch64-pc-windows-msvc.zip",
        "cb54028b59aa87f11cf6dec293ce000420043a49a8aea522e822a671321f8932",
    ),
}

#: `uv` download cap (~20 MB).
_UV_DOWNLOAD_TIMEOUT = 120.0

#: Phases reported by `install_extra` (the UI translates them).
Progress = Callable[[str], None]

#: `extra == 'x'` inside a `Requires-Dist` marker.
_EXTRA_RE = re.compile(r"extra\s*==\s*['\"]([^'\"]+)['\"]")

#: Simple marker comparison: `var == 'value'` / `var != 'value'`.
_CMP_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=)\s*['\"]([^'\"]*)['\"]\s*$"
)


def extras_root() -> Path:
    """Root where installed extras live (``~/.phoson/extras``)."""
    return Path("~/.phoson/extras").expanduser()


def bootstrap_paths() -> list[str]:
    """Add the installed extras to the sidecar's ``sys.path`` (idempotent).

    Called at sidecar startup (so an extra installed yesterday is available
    today) and after each installation (so it is available right away).
    """
    root = extras_root()
    added: list[str] = []
    if not root.is_dir():
        return added
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        text = str(path)
        if text not in sys.path:
            sys.path.append(text)
            added.append(text)
    if added:
        importlib.invalidate_caches()
    return added


# ── Extra requirements ──────────────────────────────────────────────────────


def _extra_family(extra: str) -> list[str]:
    """The requested extra and its platform sibling, if applicable."""
    family = [extra]
    sibling = _PLATFORM_SIBLINGS.get(extra, {}).get(sys.platform)
    if sibling:
        family.append(sibling)
    elif extra == "computeruse" and sys.platform.startswith("linux"):
        # Wayland needs its extra; X11 doesn't (the backend uses Xlib, already present).
        if os.environ.get("WAYLAND_DISPLAY"):
            family.append("computeruse-wayland")
    return family


def _local_env(extra: str) -> dict[str, str]:
    """Variables for evaluating markers by hand (without `packaging`)."""
    return {
        "extra": extra,
        "os_name": os.name,
        "sys_platform": sys.platform,
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "python_version": "{}.{}.{}".format(*sys.version_info[:3]),
        "python_full_version": platform.python_version(),
        "implementation_name": sys.implementation.name,
        "implementation_version": platform.python_version(),
        "platform_python_implementation": platform.python_implementation(),
    }


def _split_top(expr: str, keyword: str) -> list[str]:
    """Split ``expr`` on ``keyword`` (``and``/``or``) outside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    index = 0
    length = len(expr)
    while index < length:
        char = expr[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0 and expr.startswith(keyword, index):
            before = current[-1] if current else " "
            after = expr[index + len(keyword) : index + len(keyword) + 1]
            if not before.isalnum() and not after.isalnum():
                parts.append("".join(current))
                current = []
                index += len(keyword)
                continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _strip_outer_parens(expr: str) -> str:
    """Remove the parentheses wrapping the whole expression."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        wraps_all = True
        for index, char in enumerate(expr):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(expr) - 1:
                    wraps_all = False
                    break
        if not wraps_all:
            break
        expr = expr[1:-1].strip()
    return expr


def _eval_marker(expr: str, env: dict[str, str]) -> bool:
    """Evaluate a marker (``and``/``or``/comparisons) without `packaging`.

    This is the fallback: `packaging` is in the engine's venv but does not ship
    in the frozen sidecar, so markers are evaluated here by hand.
    """
    expr = _strip_outer_parens(expr)
    if not expr:
        return True
    if _CMP_RE.match(expr):
        name, operator, expected = _CMP_RE.match(expr).groups()  # type: ignore[union-attr]
        actual = env.get(name, "")
        return actual == expected if operator == "==" else actual != expected
    # `or` has lower precedence than `and`.
    for part in _split_top(expr, "or"):
        if all(_eval_marker(piece, env) for piece in _split_top(part, "and")):
            return True
    return False


def _strip_extra_clause(marker: str) -> str:
    """Remove the `extra == 'x'` clause (with its connector) from the marker."""
    parts = _split_top(marker, "and")
    kept = [part for part in parts if not _EXTRA_RE.fullmatch(part.strip())]
    return " and ".join(kept)


def _requirements_with_packaging(extra: str) -> list[str]:
    """Extra requirements using `packaging` (when available)."""
    from importlib.metadata import distribution

    from packaging.requirements import Requirement

    dist = distribution(ENGINE_DIST)
    found: list[str] = []
    for raw in dist.requires or []:
        requirement = Requirement(raw)
        if requirement.marker is None:
            continue  # base dependency: not ours
        # `packaging` fills in the rest of the environment (sys_platform…); here
        # it is only told which extra is being checked.
        if requirement.marker.evaluate({"extra": extra}):
            # The marker is dropped: `pip` evaluates markers with `extra=""` and
            # would skip the whole requirement.
            found.append(requirement.name + str(requirement.specifier))
    return found


def _requirements_without_packaging(extra: str) -> list[str]:
    """Extra requirements by hand (without `packaging`)."""
    from importlib.metadata import distribution

    dist = distribution(ENGINE_DIST)
    found: list[str] = []
    for raw in dist.requires or []:
        requirement, _, marker = raw.partition(";")
        requirement = requirement.strip()
        marker = marker.strip()
        if not requirement or not marker:
            continue
        if extra not in set(_EXTRA_RE.findall(marker)):
            continue  # it is another plugin's extra
        if not _eval_marker(_strip_extra_clause(marker), _local_env(extra)):
            continue
        found.append(requirement)
    return found


def requirements_for(extra: str) -> list[str]:
    """Requirements (pins) of the requested extra, with its platform siblings."""
    family = _extra_family(extra)
    found: list[str] = []
    for name in family:
        try:
            requirements = _requirements_with_packaging(name)
        except Exception:  # noqa: BLE001 — without `packaging`, evaluate by hand
            requirements = _requirements_without_packaging(name)
        for requirement in requirements:
            if requirement not in found:
                found.append(requirement)
    return found


def module_for(extra: str) -> str:
    """Module the extra provides (to check that the installation worked)."""
    try:
        from phoson_cli.labels import PLUGIN_REQUIRED_EXTRAS
    except Exception:  # noqa: BLE001 — no engine, no map
        return ""
    for module, name in PLUGIN_REQUIRED_EXTRAS.values():
        if name == extra:
            return module
    return ""


# ── Installation ────────────────────────────────────────────────────────────


def tools_root() -> Path:
    """Where the app-downloaded `uv` lives (``~/.phoson/tools``)."""
    return Path("~/.phoson/tools").expanduser()


def _search_path() -> str:
    """The sidecar's ``PATH`` plus the locations where Python and uv usually live.

    The app launched from Finder/the dock inherits launchd's ``PATH``
    (``/usr/bin:/bin:/usr/sbin:/sbin``): neither Homebrew nor ``~/.local/bin``
    (where the official installer puts ``uv``) appear, and the sidecar concluded
    there was no installer even though the user had one.
    """
    home = Path.home()
    extra = [
        home / ".local" / "bin",
        home / ".cargo" / "bin",
    ]
    if sys.platform == "darwin":
        extra += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    elif sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        extra += [
            local
            / "Programs"
            / "Python"
            / f"Python{sys.version_info[0]}{sys.version_info[1]}"
        ]
    else:
        extra += [Path("/usr/local/bin")]
    parts = [p for p in (os.environ.get("PATH") or "").split(os.pathsep) if p]
    for path in extra:
        text = str(path)
        if text not in parts:
            parts.append(text)
    return os.pathsep.join(parts)


def _which(name: str) -> str | None:
    return shutil.which(name, path=_search_path())


def _has_pip(interpreter: str) -> bool:
    """Does this interpreter have a usable `pip`?

    `pip` ≥ 22 is required: a 2021 pip does not understand modern wheels (and
    the `cp312` tags), and the installation would fail building from source.
    """
    try:
        probe = subprocess.run(
            [interpreter, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if probe.returncode != 0:
        return False
    match = re.search(r"pip\s+(\d+)\.(\d+)", probe.stdout or "")
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2))) >= (22, 0)


def _python_version(interpreter: str) -> tuple[int, int] | None:
    """Interpreter (major, minor) version, or `None` if it cannot be read."""
    try:
        probe = subprocess.run(
            [interpreter, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    parts = (probe.stdout or "").strip().split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def _interpreter_candidates() -> list[str]:
    """Interpreters that could install (the sidecar's only if not frozen).

    Only those with the **same Python version** as the sidecar qualify: the
    installed wheels (``cp312``, …) must be ones the engine can import.
    """
    wanted = sys.version_info[:2]
    candidates: list[str] = []
    if not getattr(sys, "frozen", False):
        candidates.append(sys.executable)
    for name in (
        f"python{wanted[0]}.{wanted[1]}",
        "python3",
        "python",
    ):
        found = _which(name)
        if found and found not in candidates:
            candidates.append(found)
    matching = [c for c in candidates if _python_version(c) == wanted]
    # The sidecar's own interpreter always goes first: if its version matches,
    # it is the best candidate (it is the one running the engine).
    if not getattr(sys, "frozen", False) and sys.executable in matching:
        matching.remove(sys.executable)
        matching.insert(0, sys.executable)
    return matching


def _uv_asset() -> tuple[str, str] | None:
    """``(asset, sha256)`` of `uv` for this platform, or ``None`` if there is none."""
    plat = (
        "darwin"
        if sys.platform == "darwin"
        else "win32"
        if sys.platform == "win32"
        else "linux"
    )
    return _UV_ASSETS.get((plat, platform.machine().lower()))


def _bundled_uv() -> Path:
    """Path of the `uv` the app downloads (whether it exists or not)."""
    name = "uv.exe" if sys.platform == "win32" else "uv"
    return tools_root() / f"uv-{UV_VERSION}" / name


def _ssl_context():
    """TLS context with ``certifi``'s certificates if present (frozen sidecar).

    The frozen Python does not always find the system store; ``certifi`` ships
    in the bundle (pulled in by ``httpx``).
    """
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — no certifi, use the system store
        return ssl.create_default_context()


def download_uv() -> Path:
    """Download the pinned `uv`, verify its sha256 and put it in ``~/.phoson/tools``.

    Raises ``RuntimeError`` with a readable reason if the platform has no asset,
    the download fails or the checksum does not match (in that case nothing is
    installed).
    """
    import urllib.request

    target = _bundled_uv()
    if target.is_file():
        return target
    asset = _uv_asset()
    if asset is None:
        raise RuntimeError(
            f"no hay binario de uv para {sys.platform}/{platform.machine()}"
        )
    name, expected = asset
    url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{name}"
    tools_root().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tools_root()) as tmp:
        archive = Path(tmp) / name
        with (
            urllib.request.urlopen(  # noqa: S310 — fixed GitHub URL (https)
                url, timeout=_UV_DOWNLOAD_TIMEOUT, context=_ssl_context()
            ) as response,
            archive.open("wb") as out,
        ):
            shutil.copyfileobj(response, out)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != expected:
            raise RuntimeError(f"la suma sha256 de {name} no coincide ({digest})")
        # Only the `uv` executable is extracted, by name: nothing from the
        # archive is written outside the temporary folder.
        wanted = target.name
        extracted = Path(tmp) / wanted
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                member = next(m for m in zf.namelist() if Path(m).name == wanted)
                extracted.write_bytes(zf.read(member))
        else:
            with tarfile.open(archive, "r:gz") as tf:
                member = next(
                    m
                    for m in tf.getmembers()
                    if Path(m.name).name == wanted and m.isfile()
                )
                source = tf.extractfile(member)
                if source is None:
                    raise RuntimeError(f"{name} no contiene {wanted}")
                extracted.write_bytes(source.read())
        extracted.chmod(0o755)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(extracted, target)
    return target


def installer() -> tuple[str, list[str], str]:
    """The available installer: ``(kind, command, reason)``.

    Kind: ``"pip"``, ``"uv"`` or ``"uv-download"`` (none on the machine, but
    `uv` can be downloaded; the command is empty until then). The reason is
    ``""`` when there is an installer or ``"noPip"`` when there is not.

    The frozen sidecar does not count as an interpreter: its ``sys.executable``
    is the app binary, which cannot run ``-m pip``.
    """
    for candidate in _interpreter_candidates():
        if _has_pip(candidate):
            return "pip", [candidate, "-m", "pip", "install"], ""
    for uv in (_which("uv"), str(_bundled_uv()) if _bundled_uv().is_file() else None):
        if uv:
            return "uv", [uv, "pip", "install"], ""
    if _uv_asset() is not None:
        return "uv-download", [], ""
    return "", [], "noPip"


def installable() -> tuple[bool, str]:
    """Can this sidecar install extras? ``(yes, reason)``."""
    kind, _, blocked = installer()
    return (bool(kind), blocked)


def install_extra(extra: str, on_progress: Progress | None = None) -> dict[str, Any]:
    """Install the extra into ``~/.phoson/extras/<extra>`` and enable it.

    Returns ``{"ok": True, …}`` or ``{"ok": False, "reason": …}`` with the
    reason (``noPip``, ``unknownExtra``, ``uvDownloadFailed``,
    ``installFailed``…) and the installer output. ``on_progress`` receives the
    current phase (``downloadingInstaller``, ``installing``).
    """
    progress = on_progress or (lambda _phase: None)
    extra = str(extra or "").strip()
    requirements = requirements_for(extra)
    if not requirements:
        return {"ok": False, "reason": "unknownExtra", "extra": extra}

    kind, prefix, blocked = installer()
    if not kind:
        return {"ok": False, "reason": blocked, "extra": extra}
    if kind == "uv-download":
        progress("downloadingInstaller")
        try:
            uv = download_uv()
        except Exception as exc:  # noqa: BLE001 — net, TLS, checksum: all mean failure
            return {
                "ok": False,
                "reason": "uvDownloadFailed",
                "extra": extra,
                "error": str(exc),
            }
        kind, prefix = "uv", [str(uv), "pip", "install"]

    target = extras_root() / extra
    target.mkdir(parents=True, exist_ok=True)
    if kind == "pip":
        # `--ignore-installed` forces dependencies to ALSO be installed into
        # the target: if they were already in the system interpreter, the
        # frozen sidecar would not find them.
        args = [
            *prefix,
            "--target",
            str(target),
            "--upgrade",
            "--ignore-installed",
            *requirements,
        ]
    else:
        # `--python X.Y`: the wheels must match the sidecar's version. Without
        # it, uv used the first Python it found (macOS's 3.9) and the engine
        # could not import the native modules. If that Python is not on the
        # machine, uv downloads a managed one.
        wanted = "{}.{}".format(*sys.version_info[:2])
        args = [
            *prefix,
            "--target",
            str(target),
            "--python",
            wanted,
            "--reinstall",
            *requirements,
        ]

    progress("installing")
    env = {**os.environ, "PATH": _search_path()}
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout", "extra": extra}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "spawnFailed", "extra": extra, "error": str(exc)}

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": "installFailed",
            "extra": extra,
            "error": output[-2000:],
        }

    # The new directory joins this process's `sys.path`: the plugin finds it
    # without restarting the app.
    bootstrap_paths()
    module = module_for(extra)
    if module:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            return {
                "ok": False,
                "reason": "notImportable",
                "extra": extra,
                "error": output[-2000:],
            }

    return {
        "ok": True,
        "extra": extra,
        "dir": str(target),
        "installed": requirements,
        "output": output[-2000:],
    }
