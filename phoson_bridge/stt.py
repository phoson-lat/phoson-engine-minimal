"""Voice dictation (STT) for the bridge, backed by ``phoson_plugin_stt``.

The Web Speech API depends on the webview's browser: WebView2 exposes it but it
fails (the Google service is missing in Edge) and WebKitGTK/WKWebView does not
implement it. The primary backend is the engine's own STT (Moonshine,
*on-device*, multilingual), which re-emits the transcript as JSON-RPC
notifications. The microphone is captured by the host running the sidecar — the
same machine that displays the app.

Notifications:

- ``stt.partial`` ``{sessionId, text, final}`` — ``final=false`` is interim text
  (replaced); ``final=true`` is an already closed line (appended).
- ``stt.done``    ``{sessionId, text}`` — end of capture.
- ``stt.error``   ``{sessionId, message}`` — failure with an actionable message.
- ``stt.progress`` ``{sessionId, phase, message}`` — transient notice from the
  STT engine (model load/download). The first capture downloads ~100 MB of the
  Moonshine model with native stderr silenced: without this, the UI looks hung.
  ``phase``: ``"loading"`` (preparing the session), ``"downloading"``
  (downloading the model; carries ``percent`` 0-100) and ``"ready"`` (model
  available).

Same ``interim/final`` contract as the Web Speech API, so the frontend
dictation hook is agnostic to the STT engine.

Configuration — the same chain the engine CLI uses, resolved when building the
STT engine (once per process) and exposed by ``stt.status``:

- ``model_arch`` / ``language`` in the ``stt`` plugin section of
  ``config.toml`` (``[[plugins]] name = "phoson-plugin-stt"`` with its
  ``config``),
- otherwise, ``PHOSON_STT_MODEL_ARCH`` / ``PHOSON_STT_LANGUAGE``,
- otherwise, the library default (``small-streaming``) and ``es``.

The language the frontend sends in ``stt.start`` (the UI's) wins over the above
— unless ``config.toml`` pinned one (``configuredLanguage`` in ``stt.status``):
then Settings chose it by hand and the frontend sends nothing.
``model_arch`` accepts a name (``tiny``, ``base``, ``tiny-streaming``,
``base-streaming``, ``small-streaming``, ``medium-streaming``) or a number
(0-5): it is the equivalent of the CLI's ``--stt-model-arch``.

Settings changes that section with ``stt.config.set`` (server.py, which
persists with ``save_config`` and calls :meth:`SttManager.reload`) and can
download the model ahead of time with ``stt.download``, which shares the
progress-reporting pre-download with capture.
"""

import os
import json
import time
import asyncio
import logging
import importlib
import threading
from typing import Any
from collections.abc import Callable

from .extras import requirements_for

log = logging.getLogger("phoson_bridge")

#: Seconds ``stop`` waits for the capture thread to finish.
_STOP_TIMEOUT = 4.0

#: The first capture downloads the Moonshine model (tens of MB) and opens the
#: ONNX session: 10-20 s without any visible output (the STT engine silences
#: native stderr). The wait is announced so the UI does not look hung.
_LOADING_MESSAGE = "Cargando el modelo de dictado (la primera vez se descarga)…"
_DOWNLOADING_MESSAGE = "Descargando el modelo de dictado…"
_READY_MESSAGE = "Modelo de dictado listo."

#: Seconds before rendering download progress: with a warm cache the model
#: "re-warm" finishes in milliseconds and must not show a bar.
_DOWNLOAD_GRACE_SECONDS = 0.75

_MISSING_RUNTIME = (
    "El runtime de dictado (moonshine-voice) no está instalado. "
    "Instálalo con: pip install 'phoson-engine-minimal[stt]'."
)
_MISSING_AUDIO = (
    "El backend de audio (sounddevice) no está instalado. "
    "Instálalo con: pip install 'phoson-engine-minimal[stt]'."
)

#: Hint for a misspelled ``model_arch``: the names Moonshine accepts.
_ARCH_HINT = (
    "Usa tiny|base|tiny-streaming|base-streaming|small-streaming|medium-streaming "
    "(o su número 0-5)."
)

_plugin_config_cache: dict[str, Any] | None = None


def _plugin_config() -> dict[str, Any]:
    """Dictation plugin config from ``config.toml``, or ``{}``.

    It is the same source the CLI consumes (``PhosonConfig.plugins``); the
    bridge does not reinvent the resolution, it only reads it. A failure loading
    the config (engine without ``phoson_cli``, no file) leaves dictation on the
    defaults: it never crashes.
    """
    global _plugin_config_cache
    if _plugin_config_cache is None:
        try:
            from phoson_cli.config import load_config

            specs = load_config().plugins or []
        except Exception:  # noqa: BLE001 — config is optional
            specs = []
        for spec in specs:
            if isinstance(spec, dict) and "stt" in str(spec.get("name", "")).lower():
                inner = spec.get("config")
                _plugin_config_cache = inner if isinstance(inner, dict) else {}
                break
        else:
            _plugin_config_cache = {}
    return _plugin_config_cache


def _setting(key: str) -> tuple[Any, str]:
    """``(value, source)`` of a dictation option: config.toml → environment → None."""
    value = _plugin_config().get(key)
    if value not in (None, ""):
        return value, f"la opción '{key}' del plugin stt en config.toml"
    env = f"PHOSON_STT_{key.upper()}"
    raw = os.environ.get(env)
    if raw:
        return raw, env
    return None, ""


def _arch_label(plugin, arch: int | None) -> str | None:
    """Human-readable architecture name; ``None`` if the library decides."""
    if arch is None:
        return None
    try:
        arches = getattr(
            importlib.import_module("phoson_plugin_stt.engine"), "MODEL_ARCHES", {}
        )
    except Exception:  # noqa: BLE001 — it is only a label for the UI
        return str(arch)
    return next((name for name, value in arches.items() if value == arch), str(arch))


def _arches() -> list[str]:
    """Architecture names Moonshine accepts (Settings options)."""
    try:
        arches = getattr(
            importlib.import_module("phoson_plugin_stt.engine"), "MODEL_ARCHES", {}
        )
    except Exception:  # noqa: BLE001 — it is only for the UI
        return []
    return sorted(arches)


#: Canonical name of the dictation plugin (the config.toml spec references it).
_STT_PLUGIN_NAME = "phoson-plugin-stt"


def _is_stt_spec(spec: object) -> bool:
    """Is this the dictation plugin's spec (string or inline table)?"""
    if isinstance(spec, dict):
        return "stt" in str(spec.get("name", "")).lower()
    return "stt" in str(spec or "").lower()


def merge_plugin_config(cfg: Any, values: dict[str, Any]) -> None:
    """Merge ``values`` into the stt plugin's ``config`` section in ``cfg.plugins``.

    ``values`` maps option → new value; ``None``/``""`` deletes the option (back
    to the library default). The plugin spec may be a bare string
    (``"phoson-plugin-stt"``) or an inline table with its ``config``: it is
    normalized to a table and the keys are merged; if no options remain it is
    collapsed back to the bare string so the toml stays clean. Persisting is the
    caller's job (``save_config`` with ``only_fields={"plugins"}``).
    """
    specs: list[Any] = list(getattr(cfg, "plugins", None) or [])
    index = next((i for i, spec in enumerate(specs) if _is_stt_spec(spec)), None)
    if index is None:
        if all(value in (None, "") for value in values.values()):
            return  # no section and nothing to set: do not create one
        spec: dict[str, Any] = {"name": _STT_PLUGIN_NAME, "config": {}}
    else:
        original = specs[index]
        if isinstance(original, dict):
            spec = {**original, "config": dict(original.get("config") or {})}
        else:
            spec = {"name": str(original), "config": {}}
    inner: dict[str, Any] = spec["config"]
    for key, value in values.items():
        if value in (None, ""):
            inner.pop(key, None)
        else:
            inner[key] = value
    if index is None:
        specs.append(spec)
    elif not inner and set(spec.keys()) <= {"name", "config"}:
        # No options left: minimal form (bare string), keeps the toml clean.
        specs[index] = spec["name"]
    else:
        specs[index] = spec
    cfg.plugins = specs


class SttManager:
    """Per-session microphone capture, on the sidecar loop.

    Transcription runs in a thread (``asyncio.to_thread``) because Moonshine is
    blocking; the loop only *polls* the lines/partials and emits them.
    """

    def __init__(self, emit: Callable[[str, dict[str, Any]], None]) -> None:
        self._emit = emit
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stops: dict[str, threading.Event] = {}
        #: Language per session (one capture per session) + last used, for polling.
        self._languages: dict[str, str] = {}
        self._language: str | None = None
        #: The STT engine is built once: it only stores the model architecture,
        #: and the environment does not change during the sidecar's lifetime.
        self._engine_instance: Any | None = None
        self._model_arch: int | None = None

    # ── Availability ──────────────────────────────────────────────────────
    def _plugin(self):
        """Import ``phoson_plugin_stt`` (lazily). Returns the module or ``None``."""
        try:
            return importlib.import_module("phoson_plugin_stt")
        except ImportError:
            return None

    def _download_model(
        self,
        language: str,
        session_key: str,
        emit: Callable[[str, dict[str, Any]], None],
    ) -> str:
        """Download (if missing) the Moonshine model for ``language``, with progress.

        Called BEFORE opening the microphone session — the first capture of a
        language downloads ~100 MB without any visible output (the STT engine
        silences native stderr), and with this the UI shows ``stt.progress`` with
        ``phase="downloading"`` and ``percent`` 0-100 instead of the generic
        "loading" notice. ``emit`` must be thread-safe: the method runs in a
        separate thread and the loop does the marshaling.

        Returns a short status, also consumed by ``stt.download`` (Settings):

        - ``"downloaded"`` — there was a visible download (with progress emitted);
        - ``"cached"`` — the model was already there (a warm cache re-warms in
          milliseconds and must not render bars);
        - ``"unavailable"`` — the runtime does not expose the download API;
        - ``"failed"`` — the download blew up (capture will retry and report the
          error only once via ``stt.error``).
        """
        try:
            moonshine = importlib.import_module("moonshine_voice")
        except ImportError:
            return "unavailable"
        get_model = getattr(moonshine, "get_model_for_language", None)
        if get_model is None:
            # Runtime without a public download API: the old flow ("loading"
            # notice) is still valid.
            return "unavailable"
        arch = None
        if self._model_arch is not None:
            arch_enum = getattr(moonshine, "ModelArch", None)
            if arch_enum is None:
                return "unavailable"
            try:
                # The same architecture the capture will use, to warm up
                # exactly the same model.
                arch = arch_enum(self._model_arch)
            except ValueError:
                return "unavailable"
        started = time.monotonic()
        reported = False

        def _on_progress(fraction: float, _file: str) -> None:
            nonlocal reported
            if time.monotonic() - started < _DOWNLOAD_GRACE_SECONDS:
                return
            reported = True
            percent = max(0, min(100, round(fraction * 100)))
            emit(
                "stt.progress",
                {
                    "sessionId": session_key,
                    "phase": "downloading",
                    "percent": percent,
                    "message": _DOWNLOADING_MESSAGE,
                },
            )

        try:
            # `on_progress` silences the library's tqdm bars and yields a
            # 0-1 fraction covering model + spell checker.
            get_model(language, arch, on_progress=_on_progress)
        except Exception as exc:  # noqa: BLE001 — capture will retry and report
            log.info("pre-descarga del modelo de dictado no disponible: %s", exc)
            return "failed"
        return "downloaded" if reported else "cached"

    def _engine(self) -> tuple[Any | None, str | None]:
        """Return ``(SttEngine | None, failure-reason | None)``, built once.

        Does not download models: it only checks that the plugin, the Moonshine
        runtime and the audio backend (sounddevice) are available, and honors the
        configured ``model_arch`` (config.toml → environment).
        """
        if self._engine_instance is not None:
            return self._engine_instance, None
        plugin = self._plugin()
        if plugin is None:
            return None, "El plugin de dictado (phoson_plugin_stt) no está instalado."
        try:
            importlib.import_module("moonshine_voice")
        except ImportError:
            return None, _MISSING_RUNTIME
        # API compatibility: phoson_plugin_stt builds MicTranscriber() with no
        # arguments (builder API, moonshine-voice >= 0.1.1). A 0.1.0 imports
        # fine — this check catches it here, with an actionable message, instead
        # of letting the microphone blow up mid-dictation (alpha.12).
        try:
            from moonshine_voice import MicTranscriber

            MicTranscriber()
        except TypeError as exc:
            return None, (
                "El runtime de dictado (moonshine-voice) es demasiado viejo: "
                f"{exc}. Actualízalo con: pip install 'moonshine-voice>=0.1.5'."
            )
        except ImportError:
            return None, _MISSING_RUNTIME
        try:
            importlib.import_module("sounddevice")
        except ImportError:
            return None, _MISSING_AUDIO
        except OSError:
            # PortAudio is missing: the STT engine has its own locator
            # (PHOSON_STT_PORTAUDIO / ~/.cache/phoson/portaudio); let it
            # fail during capture with its actionable message.
            pass
        raw_arch, arch_source = _setting("model_arch")
        try:
            arch = plugin.parse_model_arch(raw_arch)
        except (ValueError, TypeError) as exc:
            return (
                None,
                f"{arch_source or 'model_arch'} no es válido: {exc} {_ARCH_HINT}",
            )
        try:
            engine = plugin.SttEngine(model_arch=arch)
        except Exception as exc:  # noqa: BLE001 — never crash the UI
            return None, str(exc)
        self._engine_instance = engine
        self._model_arch = arch
        return engine, None

    def reload(self) -> None:
        """Drop the STT engine and the cached config after ``config.toml`` changes.

        ``stt.config.set`` writes the plugin section and calls this: the next
        capture rebuilds the STT engine with the new ``model_arch`` instead of
        waiting for an app restart. An in-flight capture is left alone (the task
        already holds its reference to the engine).
        """
        global _plugin_config_cache
        _plugin_config_cache = None
        self._engine_instance = None
        self._model_arch = None

    def pick_language(self, requested: str | None) -> str:
        """Effective language for a capture: requested → config.toml/env → ``es``.

        Raises ``ValueError`` (the plugin's actionable message) if the language
        is not supported. Besides ``start``, ``stt.download`` and Settings use it
        to predict which language the next session will capture.
        """
        plugin = self._plugin()
        configured = _setting("language")[0]
        wanted = requested or (str(configured) if configured else None) or None
        if plugin is not None:
            return plugin.normalize_language(wanted)
        return wanted or "es"

    def status(self) -> dict[str, Any]:
        """Dictation status so the UI can enable or explain the button."""
        engine, reason = self._engine()
        languages: list[str] = []
        plugin = self._plugin()
        if plugin is not None:
            languages = sorted(getattr(plugin, "LANGUAGES", {}) or {})
        # Language of the in-flight capture (usually there is one); else the last.
        active = next(iter(self._languages.values()), None)
        configured, language_source = _setting("language")
        _, arch_source = _setting("model_arch")
        return {
            "supported": engine is not None,
            "listening": any(not task.done() for task in self._tasks.values()),
            "language": active or self._language,
            "languages": languages,
            # Friendly names (es → "Spanish") and accepted architectures:
            # options for the Dictation section in Settings.
            "languageNames": dict(getattr(plugin, "LANGUAGES", {}) or {})
            if plugin
            else {},
            "arches": _arches(),
            # Language pinned in config.toml/env: the frontend stops sending its
            # own and the source explains who decides. null = the UI's.
            "configuredLanguage": str(configured) if configured else None,
            "languageSource": language_source or None,
            "archSource": arch_source or None,
            # Model in use; null = moonshine-voice's default for the language.
            "modelArch": self._model_arch,
            "modelName": _arch_label(plugin, self._model_arch) if engine else None,
            "reason": reason,
            # Engine extra that fixes it (the UI offers to install it); empty if
            # the failure is not a missing dependency or the platform lacks it
            # (Moonshine does not publish wheels for Intel macOS).
            "missingExtra": "stt"
            if reason in (_MISSING_RUNTIME, _MISSING_AUDIO) and requirements_for("stt")
            else "",
        }

    # ── Capture cycle ─────────────────────────────────────────────────────
    async def start(
        self, session_key: str, language: str | None = None
    ) -> dict[str, Any]:
        """Start dictation for a session (stopping the previous one, if any)."""
        await self.stop(session_key)

        engine, reason = self._engine()
        if engine is None:
            self._emit("stt.error", {"sessionId": session_key, "message": reason})
            return {"ok": False, "supported": False, "reason": reason}

        # The UI language (sent by the frontend in stt.start) beats the configured one.
        try:
            chosen = self.pick_language(language)
        except ValueError as exc:
            self._emit("stt.error", {"sessionId": session_key, "message": str(exc)})
            return {"ok": False, "supported": True, "reason": str(exc)}

        self._language = chosen
        self._languages[session_key] = chosen
        stop = threading.Event()
        self._stops[session_key] = stop
        self._tasks[session_key] = asyncio.create_task(
            self._listen(session_key, engine, chosen, stop)
        )
        return {"ok": True, "supported": True, "language": chosen}

    async def stop(self, session_key: str) -> dict[str, Any]:
        """Stop a session's capture and wait for it to emit its result."""
        stop = self._stops.get(session_key)
        if stop is not None:
            stop.set()
        task = self._tasks.get(session_key)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_STOP_TIMEOUT)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                # Wait for the old task's `finally` to complete BEFORE
                # returning: otherwise an immediate `start` would insert the new
                # task and the old one's `finally` would delete it (orphaned
                # capture).
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        return {"ok": True}

    async def stop_all(self) -> None:
        for key in list(self._tasks):
            await self.stop(key)

    async def _listen(
        self,
        key: str,
        engine: Any,
        language: str,
        stop: threading.Event,
    ) -> None:
        captured: list[str] = []
        partial = {"text": ""}

        # The STT engine downloads the model and sets up the ONNX session
        # inside the thread, with native stderr silenced: without this notice
        # the wait looks like a hang.
        self._emit(
            "stt.progress",
            {"sessionId": key, "phase": "loading", "message": _LOADING_MESSAGE},
        )

        # Pre-download with progress (see `_download_model`). Marshaling goes
        # through `call_soon_threadsafe` because `self._emit` touches the loop's
        # asyncio queue, which is not thread-safe.
        loop = asyncio.get_running_loop()

        def _emit_threadsafe(method: str, params: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(self._emit, method, params)

        worker: asyncio.Future[str] | None = None
        emitted = 0
        last_partial = ""
        try:
            await asyncio.to_thread(
                self._download_model, language, key, _emit_threadsafe
            )
            if stop.is_set():
                # The user stopped dictation while the model was downloading.
                self._emit("stt.done", {"sessionId": key, "text": ""})
                return
            worker = asyncio.ensure_future(
                asyncio.to_thread(
                    engine.transcribe_microphone,
                    language,
                    None,
                    stop=stop,
                    on_line=captured.append,
                    on_partial=lambda text: partial.__setitem__("text", text),
                    on_ready=lambda: _emit_threadsafe(
                        "stt.progress",
                        {"sessionId": key, "phase": "ready", "message": _READY_MESSAGE},
                    ),
                )
            )
            while not worker.done():
                await asyncio.sleep(0.15)
                emitted = self._emit_new_lines(key, captured, emitted)
                current = partial["text"].strip()
                if current and current != last_partial:
                    self._emit(
                        "stt.partial",
                        {"sessionId": key, "text": current, "final": False},
                    )
                    last_partial = current
            text = await worker
        except asyncio.CancelledError:
            stop.set()
            if worker is not None:
                worker.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 — audio errors don't crash the bridge
            log.warning("dictado falló: %s", exc)
            self._emit(
                "stt.error",
                {"sessionId": key, "message": f"Fallo de transcripción: {exc}"},
            )
            return
        finally:
            # Only clean up if we are still the registered task: a later `start`
            # may have replaced the entry, and a blind `pop` would delete the
            # NEW capture.
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)
                self._stops.pop(key, None)
                self._languages.pop(key, None)

        self._emit_new_lines(key, captured, emitted)
        self._emit("stt.done", {"sessionId": key, "text": text})

    def _emit_new_lines(self, key: str, captured: list[str], emitted: int) -> int:
        while emitted < len(captured):
            self._emit(
                "stt.partial",
                {"sessionId": key, "text": captured[emitted], "final": True},
            )
            emitted += 1
        return emitted


def report() -> tuple[bool, str]:
    """``phoson-bridge --stt-check``: dictation status as text, no microphone.

    Useful for debugging the *frozen* sidecar (PyInstaller): a binary without
    the ``stt`` extra responds, but ``supported`` is ``false`` with the reason.
    """
    manager = SttManager(lambda _method, _params: None)
    status = manager.status()
    return bool(status["supported"]), json.dumps(status, ensure_ascii=False, indent=2)


def transcribe_wav(path: str, language: str | None = None) -> tuple[bool, str]:
    """``phoson-bridge --stt-transcribe``: dictate a WAV and return the text.

    It is the deterministic packaging test: it goes through the same Moonshine
    runtime (its native libraries and the model download) that the microphone
    button uses, but fed from a file and without PortAudio, so it runs in CI,
    without a mic and in the frozen binary. ``language``, when given, bypasses
    the configuration chain (config.toml → environment). Returns ``(ok, text)``
    or ``(False, reason)``.
    """
    manager = SttManager(lambda _method, _params: None)
    engine, reason = manager._engine()
    if engine is None:
        return False, reason or "El motor de dictado no está disponible."
    plugin = manager._plugin()
    try:
        chosen = plugin.normalize_language(language) if plugin else (language or "es")
    except ValueError as exc:
        return False, str(exc)
    if not hasattr(engine, "transcribe_wav"):
        return False, (
            "El plugin de dictado no puede dictar archivos "
            "(actualiza phoson-engine-minimal)."
        )
    try:
        text = engine.transcribe_wav(path, chosen)
    except FileNotFoundError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — CLI diagnostic, never crash
        return False, f"{type(exc).__name__}: {exc}"
    return True, text
