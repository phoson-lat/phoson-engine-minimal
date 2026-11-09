"""Dictation model pre-download with progress (`stt.progress`).

The first capture for a language downloads ~100 MB of the Moonshine model with
the native stderr silenced. The bridge does it ahead of time
(`_download_model`) to emit ``phase="downloading"`` with ``percent`` 0-100 and
``phase="ready"`` when done; with a warm cache it must not render anything.

Runs in CI: the Moonshine runtime is replaced with a stub, same as
`test_stt_config.py`.

    python bridge/tests/test_stt_download.py
"""

import os
import sys
import enum
import types
import asyncio
import threading
from typing import Any
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ModelArch(enum.IntEnum):
    """Fake ModelArch: it is enough that `ModelArch(n)` works."""

    TINY = 0
    BASE = 1
    TINY_STREAMING = 2
    BASE_STREAMING = 3
    SMALL_STREAMING = 4
    MEDIUM_STREAMING = 5


class _MicTranscriber:  # minimal builder API, no microphone
    def language(self, _code: str) -> "_MicTranscriber":
        return self


#: Calls to `get_model_for_language`: ``(language, arch, duration)``.
get_calls: list[tuple[str, Any, float]] = []
#: If true, `get_model_for_language` blows up (download impossible).
fail_download = False


def _get_model_for_language(language, arch=None, *, on_progress=None, **_kw):
    """Fake downloader: records the call and notifies like the real tracker.

    The moonshine-voice tracker calls the callback once per file even when they
    are cached (`force` events): this also tests the grace window that tells
    "warm cache" apart from "real download".
    """
    duration = 0.01 if not fail_download else 0.0
    get_calls.append((language, arch, duration))
    if fail_download:
        raise RuntimeError("sin red")
    if on_progress is not None:
        on_progress(0.5, "model.ort")
        on_progress(1.0, "model.ort")
    return "/cache/model", arch


_moonshine = types.ModuleType("moonshine_voice")
_moonshine.MicTranscriber = _MicTranscriber
_moonshine.ModelArch = ModelArch
_moonshine.get_model_for_language = _get_model_for_language
sys.modules.setdefault("moonshine_voice", _moonshine)
sys.modules.setdefault("sounddevice", types.ModuleType("sounddevice"))

# Fake `phoson_plugin_stt`, with the surface the bridge uses.
if "phoson_plugin_stt" not in sys.modules:
    _engine_mod = types.ModuleType("phoson_plugin_stt.engine")
    _engine_mod.MODEL_ARCHES = {
        name: value for name, value in ModelArch.__members__.items()
    }
    _pkg = types.ModuleType("phoson_plugin_stt")
    _pkg.engine = _engine_mod
    sys.modules["phoson_plugin_stt"] = _pkg
    sys.modules["phoson_plugin_stt.engine"] = _engine_mod

from phoson_bridge import stt  # noqa: E402

failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(name)


class _Engine:
    """Fake SttEngine: "captures" until it is stopped."""

    def __init__(self, model_arch: int | None = None) -> None:
        self.model_arch = model_arch

    def transcribe_microphone(
        self, language: str, seconds: float | None = None, *, stop: Any = None, **_kw
    ) -> str:
        assert isinstance(stop, threading.Event), (
            "el bridge debe pasar su evento de parada"
        )
        _kw["on_ready"]()
        while not stop.wait(0.02):
            pass
        return f"[{language}]"


class _Plugin:
    LANGUAGES = {"es": "Spanish", "en": "English", "zh": "Chinese"}
    SttEngine = _Engine

    @staticmethod
    def parse_model_arch(value: object) -> int | None:
        if value in (None, ""):
            return None
        known = {"medium-streaming": 5}
        if isinstance(value, str) and value in known:
            return known[value]
        raise ValueError(f"Unknown model_arch {value!r}")

    @staticmethod
    def normalize_language(lang: str | None) -> str:
        code = (lang or "es").strip().lower().split("-")[0]
        if code not in _Plugin.LANGUAGES:
            raise ValueError(f"Idioma no admitido: {code}")
        return code


def _manager() -> stt.SttManager:
    """SttManager with a fake engine that records every notification."""
    for key in ("PHOSON_STT_MODEL_ARCH", "PHOSON_STT_LANGUAGE"):
        os.environ.pop(key, None)
    stt._plugin_config_cache = {}  # no config.toml: only the environment decides
    notes: list[tuple[str, dict[str, Any]]] = []
    manager = stt.SttManager(
        lambda method, params: notes.append((method, dict(params)))
    )
    manager._plugin = lambda: _Plugin
    manager.notes = notes  # type: ignore[attr-defined]
    return manager


async def _dictate(
    manager: stt.SttManager, key: str, language: str | None
) -> list[tuple[str, dict[str, Any]]]:
    """Start the capture, let the pre-download run, and stop it."""
    await manager.start(key, language)
    # The pre-download runs in a thread and its events cross to the loop via
    # `call_soon_threadsafe`: the loop has to spin to see them.
    await asyncio.sleep(0.2)
    await manager.stop(key)
    return manager.notes  # type: ignore[attr-defined]


def _phases(notes: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, Any]]:
    """Emitted `stt.progress` phases: ``[(phase, percent), ...]``."""
    return [
        (params.get("phase"), params.get("percent"))
        for method, params in notes
        if method == "stt.progress"
    ]


# 1) Cold download: loading → downloading (with percent) → ready.
stt._DOWNLOAD_GRACE_SECONDS = 0  # no grace: the stub "downloads" instantly
notes = asyncio.run(_dictate(_manager(), "s1", "en"))
_check(
    "descarga fría: loading → downloading(percent) → ready",
    _phases(notes)
    == [("loading", None), ("downloading", 50), ("downloading", 100), ("ready", None)],
    str(_phases(notes)),
)
_check(
    "la captura sigue tras la pre-descarga (stt.done)",
    any(method == "stt.done" for method, _params in notes),
    str([m for m, _ in notes]),
)
_check(
    "se pre-descarga el idioma pedido (no el default)",
    get_calls and get_calls[-1][0] == "en" and get_calls[-1][1] is None,
    str(get_calls[-1]),
)

# 2) Warm cache: the rewarm takes milliseconds and draws no bars.
stt._DOWNLOAD_GRACE_SECONDS = 0.75  # real value
notes = asyncio.run(_dictate(_manager(), "s2", "en"))
_check(
    "caché caliente: loading → ready al abrir el micrófono, sin downloading",
    _phases(notes) == [("loading", None), ("ready", None)],
    str(_phases(notes)),
)

# 3) Download impossible: no progress, the capture reports its error.
fail_download = True
get_calls.clear()
notes = asyncio.run(_dictate(_manager(), "s3", "en"))
fail_download = False
_check(
    "descarga rota: sin fase downloading; el motor puede abrir con la caché local",
    all(phase != "downloading" for phase, _percent in _phases(notes)),
    str(_phases(notes)),
)
_check(
    "descarga rota: la captura no se aborta por la pre-descarga",
    any(method == "stt.done" for method, _params in notes),
    str([m for m, _ in notes]),
)

# 4) The pre-download warms the SAME configured architecture.
manager = _manager()  # first: `_manager` clears the environment
os.environ["PHOSON_STT_MODEL_ARCH"] = "medium-streaming"
manager._engine()  # sets self._model_arch from the environment
asyncio.run(_dictate(manager, "s4", "en"))
os.environ.pop("PHOSON_STT_MODEL_ARCH", None)
_check(
    "model_arch configurada se propaga a la pre-descarga",
    get_calls and get_calls[-1][1] == ModelArch.MEDIUM_STREAMING,
    str(get_calls[-1]),
)

stt._plugin_config_cache = None

print("-" * 40)
print("FAILED:", failures) if failures else print("todo OK")
sys.exit(1 if failures else 0)
