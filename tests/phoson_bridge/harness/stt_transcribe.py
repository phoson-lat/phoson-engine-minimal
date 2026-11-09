"""``stt.transcribe_wav``: the microphone-free dictation test (for the frozen build).

The PyInstaller binary can answer the protocol and still be unable to dictate
(it lacks Moonshine's native libraries or cannot download the model). That is
why the bridge exposes ``--stt-transcribe``, which dictates a WAV through the
same path as the microphone but without PortAudio. This test fakes the engine,
so it also runs in CI, without ``moonshine-voice`` installed.

    python bridge/tests/test_stt_transcribe.py
"""

import sys
import types
from typing import Any
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.modules.setdefault("moonshine_voice", types.ModuleType("moonshine_voice"))
sys.modules.setdefault("sounddevice", types.ModuleType("sounddevice"))

from phoson_bridge import stt  # noqa: E402

failures: list[str] = []
_REAL_ENGINE = stt.SttManager._engine


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(name)


class _Plugin:
    """The part of ``phoson_plugin_stt`` the helper uses (with name aliases)."""

    _ALIASES = {"spanish": "es", "english": "en"}

    @staticmethod
    def normalize_language(lang: str | None) -> str:
        code = (lang or "es").strip().lower().split("-")[0]
        code = _Plugin._ALIASES.get(code, code)
        if code not in ("es", "en"):
            raise ValueError(f"Idioma no admitido: {code}")
        return code


class _Engine:
    """Fake SttEngine that remembers how it was called."""

    def __init__(
        self, *, text: str = "hola", error: BaseException | None = None
    ) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def transcribe_wav(self, path: Any, language: str) -> str:
        self.calls.append((str(path), language))
        if self.error is not None:
            raise self.error
        return self.text


class _OldEngine:
    """Engine from an old plugin: no ``transcribe_wav``."""

    def transcribe_microphone(self, *_a: Any, **_kw: Any) -> str:
        return ""


def _fake_engine(engine: Any, reason: str | None = None) -> None:
    """``SttManager._engine``/``._plugin`` returned by the test (restored at the end).

    The helper builds its own ``SttManager``, so the class is faked, not an
    instance: otherwise ``transcribe_wav`` would use the venv's real plugin.
    """

    def _method(self: Any) -> tuple[Any, str | None]:  # noqa: ARG001 — original signature
        return engine, reason

    stt.SttManager._engine = _method
    stt.SttManager._plugin = lambda self: _Plugin  # type: ignore[method-assign]


WAV = str(Path(__file__).resolve().parent / "audio.wav")

# 1) Happy path: dictates and normalizes the language requested by the CLI.
engine = _Engine(text="esto es una prueba")
_fake_engine(engine)
ok, text = stt.transcribe_wav(WAV, "SPANISH")
_check(
    "dicta el WAV con el idioma normalizado",
    ok and text == "esto es una prueba" and engine.calls == [(WAV, "es")],
    f"{ok} {text!r} {engine.calls}",
)

# 2) No language: the configuration chain decides (here, the default "es").
engine = _Engine(text="vale")
_fake_engine(engine)
ok, _ = stt.transcribe_wav(WAV)
_check(
    "sin idioma usa el default", ok and engine.calls == [(WAV, "es")], str(engine.calls)
)

# 3) Unsupported language: readable reason and the engine is not even touched.
engine = _Engine()
_fake_engine(engine)
ok, text = stt.transcribe_wav(WAV, "klingon")
_check(
    "idioma no admitido se reporta como error",
    not ok and "no admitido" in text and engine.calls == [],
    f"{ok} {text!r}",
)

# 4) Missing file: the FileNotFoundError arrives as a reason, not a traceback.
_fake_engine(_Engine(error=FileNotFoundError(f"Audio file not found: {WAV}")))
ok, text = stt.transcribe_wav(WAV, "es")
_check("archivo ausente se reporta", not ok and "not found" in text, repr(text))

# 5) Native runtime failure (exactly what the packaging check wants to catch).
_fake_engine(_Engine(error=RuntimeError("onnxruntime failed to initialize")))
ok, text = stt.transcribe_wav(WAV, "es")
_check(
    "un fallo del motor se etiqueta con su tipo",
    not ok and text.startswith("RuntimeError:") and "onnxruntime" in text,
    repr(text),
)

# 6) No engine (extra missing or PortAudio broken): _engine()'s reason propagates.
_fake_engine(None, reason="El plugin de dictado no está instalado.")
ok, text = stt.transcribe_wav(WAV, "es")
_check("sin motor se devuelve su motivo", not ok and "plugin" in text, repr(text))

# 7) Old plugin without ``transcribe_wav``: actionable notice, no AttributeError.
_fake_engine(_OldEngine())
ok, text = stt.transcribe_wav(WAV, "es")
_check(
    "un plugin sin dictado de archivos lo dice",
    not ok and "actualiza" in text,
    repr(text),
)

stt.SttManager._engine = _REAL_ENGINE  # restore the class as it was
print("todo OK" if not failures else f"FALLOS: {failures}")
sys.exit(1 if failures else 0)
