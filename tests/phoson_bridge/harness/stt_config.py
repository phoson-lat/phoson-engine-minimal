"""Dictation model and language: the bridge honors config.toml/environment and the UI.

It neither turns on the microphone nor downloads models: the engine is replaced
with a stub, so it also runs in CI (without `moonshine-voice` installed).

    python bridge/tests/test_stt_config.py
"""

import os
import sys
import types
import asyncio
import threading
from typing import Any
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The bridge probes these two to tell whether there is an engine. They are faked
# here so the test also holds in CI, where `moonshine-voice` is not installed. The
# fake moonshine_voice exposes the builder API that `_engine()` validates
# (`MicTranscriber()` with no arguments), same as the real one >= 0.1.1.
_moonshine = types.ModuleType("moonshine_voice")


class _MicTranscriber:  # minimal builder API, no microphone
    def language(self, _code: str) -> "_MicTranscriber":
        return self


_moonshine.MicTranscriber = _MicTranscriber
sys.modules.setdefault("moonshine_voice", _moonshine)
sys.modules.setdefault("sounddevice", types.ModuleType("sounddevice"))

# `_arch_label` reads the engine's MODEL_ARCHES to name the model; without this,
# in CI the label would fall back to the bare number and the test would depend
# on the environment.
if "phoson_plugin_stt.engine" not in sys.modules:
    _pkg = types.ModuleType("phoson_plugin_stt")
    _engine_mod = types.ModuleType("phoson_plugin_stt.engine")
    _engine_mod.MODEL_ARCHES = {
        "tiny": 0,
        "base": 1,
        "tiny_streaming": 2,
        "base_streaming": 3,
        "small_streaming": 4,
        "medium_streaming": 5,
    }
    _pkg.engine = _engine_mod
    sys.modules["phoson_plugin_stt"] = _pkg
    sys.modules["phoson_plugin_stt.engine"] = _engine_mod

from phoson_bridge import stt  # noqa: E402
from phoson_bridge.stt import merge_plugin_config  # noqa: E402

failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(name)


class _Engine:
    """Fake SttEngine: remembers its arch and "captures" until it is stopped."""

    def __init__(self, model_arch: int | None = None) -> None:
        self.model_arch = model_arch

    def transcribe_microphone(
        self, language: str, seconds: float | None = None, *, stop: Any = None, **_kw
    ) -> str:
        assert isinstance(stop, threading.Event), (
            "el bridge debe pasar su evento de parada"
        )
        while not stop.wait(0.02):
            pass
        return f"[{language}]"


class _Plugin:
    """Fake phoson_plugin_stt, with the same surface the bridge uses."""

    LANGUAGES = {"es": "Spanish", "en": "English", "zh": "Chinese"}
    SttEngine = _Engine
    seen: list[Any] = []

    @staticmethod
    def parse_model_arch(value: object) -> int | None:
        _Plugin.seen.append(value)
        if value in (None, ""):
            return None
        known = {"tiny": 0, "base": 1, "small-streaming": 4, "medium-streaming": 5}
        if isinstance(value, str) and value in known:
            return known[value]
        raise ValueError(f"Unknown model_arch {value!r}")

    @staticmethod
    def normalize_language(lang: str | None) -> str:
        code = (lang or "es").strip().lower().split("-")[0]
        if code not in _Plugin.LANGUAGES:
            raise ValueError(f"Idioma no admitido: {code}")
        return code


def _manager(**env: str | None) -> stt.SttManager:
    """SttManager with a fake engine and an empty config.toml config."""
    for key in ("PHOSON_STT_MODEL_ARCH", "PHOSON_STT_LANGUAGE"):
        os.environ.pop(key, None)
    for key, value in env.items():
        if value is not None:
            os.environ[key] = value
    stt._plugin_config_cache = {}  # no config.toml: only the environment decides
    manager = stt.SttManager(lambda _m, _p: None)
    manager._plugin = lambda: _Plugin
    return manager


async def _dictate(
    manager: stt.SttManager, key: str, language: str | None
) -> dict[str, Any]:
    """Start the capture, read the status, and stop it (leaves no hanging threads)."""
    started = await manager.start(key, language)
    await asyncio.sleep(0)
    status = manager.status()
    await manager.stop(key)
    return {**started, "_status": status}


# 1) No configuration: the library's default model, engine operational.
manager = _manager()
engine, reason = manager._engine()
status = manager.status()
_check(
    "sin configuración usa el modelo de la librería",
    engine is not None
    and reason is None
    and _Plugin.seen[-1] is None
    and status["modelArch"] is None
    and status["modelName"] is None,
    str(status),
)

# 2) PHOSON_STT_MODEL_ARCH reaches the engine and is reported to the UI.
manager = _manager(PHOSON_STT_MODEL_ARCH="medium-streaming")
engine, reason = manager._engine()
status = manager.status()
_check(
    "PHOSON_STT_MODEL_ARCH se propaga al motor",
    engine is not None and engine.model_arch == 5,
    str(reason),
)
_check(
    "stt.status reporta el modelo en uso",
    status["supported"]
    and status["modelArch"] == 5
    and status["modelName"] == "medium_streaming",
    str(status),
)

# 3) config.toml beats the environment (same precedence as the engine's CLI).
manager = _manager(PHOSON_STT_MODEL_ARCH="base")
stt._plugin_config_cache = {"model_arch": "tiny"}
engine, _ = manager._engine()
_check(
    "config.toml gana a PHOSON_STT_MODEL_ARCH",
    engine is not None and engine.model_arch == 0,
    str(manager._model_arch),
)

# 4) An invalid value does not bring down the sidecar: it says so and names the source.
manager = _manager(PHOSON_STT_MODEL_ARCH="no-existe")
engine, reason = manager._engine()
_check(
    "arch inválida => supported false con motivo accionable",
    engine is None
    and reason is not None
    and "PHOSON_STT_MODEL_ARCH" in reason
    and "medium-streaming" in reason,
    str(reason),
)

# 5) Language: the UI's wins, then the configured one; an unknown language does not
#    capture.
loop_result = asyncio.run(_dictate(_manager(), "s1", "en"))
_check(
    "stt.start respeta el idioma del UI",
    loop_result.get("ok") and loop_result.get("language") == "en",
    str(loop_result),
)

loop_result = asyncio.run(_dictate(_manager(PHOSON_STT_LANGUAGE="zh"), "s2", None))
_check(
    "sin idioma del UI usa PHOSON_STT_LANGUAGE",
    loop_result.get("language") == "zh" and loop_result["_status"]["language"] == "zh",
    str(loop_result),
)

loop_result = asyncio.run(_dictate(_manager(), "s3", "fr"))
_check(
    "idioma no admitido => error accionable y sin captura",
    loop_result.get("ok") is False and "fr" in str(loop_result.get("reason")),
    str(loop_result),
)

# 6) stt.status exposes what Settings needs: the pinned language (and its source) +
#    the accepted architectures for the selector.
manager = _manager(PHOSON_STT_LANGUAGE="en")
status = manager.status()
_check(
    "status expone configuredLanguage + languageSource + arches",
    status["configuredLanguage"] == "en"
    and status["languageSource"] == "PHOSON_STT_LANGUAGE"
    and status["languageNames"].get("es") == "Spanish"
    and status["arches"] == sorted(status["arches"])
    and "medium_streaming" in status["arches"],
    str(status),
)

# 7) pick_language: what the next session will capture (UI → config → default).
manager = _manager()
stt._plugin_config_cache = {"language": "zh"}
_check(
    "pick_language: el configurado gana al default",
    manager.pick_language(None) == "zh",
    manager.pick_language(None),
)
_check(
    "pick_language: el pedido gana al configurado",
    manager.pick_language("en") == "en",
    manager.pick_language("en"),
)


# 8) merge_plugin_config: the stt plugin's section in cfg.plugins.
class _Cfg:
    def __init__(self, plugins: list[Any]) -> None:
        self.plugins = plugins


cfg = _Cfg(["phoson-plugin-stt"])
merge_plugin_config(cfg, {"language": "en", "model_arch": "tiny"})
_check(
    "una spec en cadena pasa a tabla con su config",
    cfg.plugins
    == [
        {
            "name": "phoson-plugin-stt",
            "config": {"language": "en", "model_arch": "tiny"},
        }
    ],
    str(cfg.plugins),
)
merge_plugin_config(cfg, {"language": None})
_check(
    "borrar el idioma deja el resto y la forma de tabla",
    cfg.plugins == [{"name": "phoson-plugin-stt", "config": {"model_arch": "tiny"}}],
    str(cfg.plugins),
)
merge_plugin_config(cfg, {"model_arch": None})
_check(
    "tabla vacía vuelve a la cadena suelta original",
    cfg.plugins == ["phoson-plugin-stt"],
    str(cfg.plugins),
)
cfg = _Cfg([])
merge_plugin_config(cfg, {"language": "en"})
_check(
    "sin spec previa se crea la sección del plugin stt",
    cfg.plugins == [{"name": "phoson-plugin-stt", "config": {"language": "en"}}],
    str(cfg.plugins),
)
cfg = _Cfg([])
merge_plugin_config(cfg, {"language": None})
_check(
    "sin spec previa y sin valores no se crea nada",
    cfg.plugins == [],
    str(cfg.plugins),
)
cfg = _Cfg([{"name": "phoson-plugin-stt", "config": {"language": "zh"}}])
merge_plugin_config(cfg, {"model_arch": "base"})
_check(
    "una tabla previa se fusiona sin perder el resto",
    cfg.plugins
    == [
        {
            "name": "phoson-plugin-stt",
            "config": {"language": "zh", "model_arch": "base"},
        }
    ],
    str(cfg.plugins),
)
cfg = _Cfg(["otro-plugin"])
merge_plugin_config(cfg, {"language": "en"})
_check(
    "las specs ajenas se respetan",
    cfg.plugins
    == ["otro-plugin", {"name": "phoson-plugin-stt", "config": {"language": "en"}}],
    str(cfg.plugins),
)

# 9) reload(): after a config change, the engine is rebuilt with the new values.
manager = _manager()
manager._engine()
stt._plugin_config_cache = {"model_arch": "tiny"}
manager.reload()
_check(
    "reload descarta el motor y la config cacheada",
    manager._engine_instance is None and stt._plugin_config_cache is None,
    f"{manager._engine_instance} {stt._plugin_config_cache}",
)
# Re-reading the toml (the file already persisted the change) sees the new arch…
stt._plugin_config_cache = {"model_arch": "tiny"}
engine, reason = manager._engine()
_check(
    "reload aplica la nueva model_arch sin reiniciar",
    engine is not None and engine.model_arch == 0 and manager._model_arch == 0,
    f"{reason} {manager._model_arch}",
)

for key in ("PHOSON_STT_MODEL_ARCH", "PHOSON_STT_LANGUAGE"):
    os.environ.pop(key, None)
stt._plugin_config_cache = None

print("-" * 40)
print("FAILED:", failures) if failures else print("todo OK")
sys.exit(1 if failures else 0)
