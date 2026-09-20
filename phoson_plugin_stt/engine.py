"""Moonshine-backed speech-to-text engine.

Thin, *optional-dependency* wrapper around ``moonshine-voice``. The heavy
runtime is imported lazily so the plugin still loads (and its dictation
entry point stays advertised) when the package or the system PortAudio library is
missing; the failure then surfaces as :class:`SttUnavailable` with an
actionable message at call time instead of at import time.

Moonshine is multilingual — :data:`LANGUAGES` mirrors the languages the
library currently ships models for. Models are downloaded on first use and
cached by the library under ``~/.cache/moonshine_voice``.
"""

import os
import time
import importlib
import threading
import contextlib
import ctypes.util
from pathlib import Path
from collections.abc import Callable

#: Languages Moonshine ships models for (code -> friendly name).
LANGUAGES: dict[str, str] = {
    "ar": "Arabic",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "ja": "Japanese",
    "ko": "Korean",
    "tl": "Filipino",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "zh": "Chinese",
}

DEFAULT_LANGUAGE = "es"
#: ``None`` means "listen until stopped" (push-to-talk has no time limit).
DEFAULT_SECONDS: float | None = None
#: Upper bound for an *explicitly* requested duration (not for unlimited).
MAX_SECONDS = 120.0

#: Moonshine ``ModelArch`` integer values by friendly name.
MODEL_ARCHES: dict[str, int] = {
    "tiny": 0,
    "base": 1,
    "tiny_streaming": 2,
    "base_streaming": 3,
    "small_streaming": 4,
    "medium_streaming": 5,
}


def parse_model_arch(value: object) -> int | None:
    """Normalize a model-arch name/int to its Moonshine integer, or ``None``.

    Accepts ``None``/empty (library default), an integer in range, or any of
    :data:`MODEL_ARCHES`. Raises :class:`ValueError` for anything else.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid model_arch: {value!r}")
    if isinstance(value, int):
        if 0 <= value <= max(MODEL_ARCHES.values()):
            return value
        raise ValueError(f"model_arch out of range: {value}")
    text = str(value).strip().lower().replace("-", "_")
    if text.isdigit():
        return parse_model_arch(int(text))
    if text in MODEL_ARCHES:
        return MODEL_ARCHES[text]
    names = ", ".join(sorted(MODEL_ARCHES))
    raise ValueError(f"Unknown model_arch {value!r}. Known: {names}")


#: Friendly names users may type instead of the ISO code.
_ALIASES: dict[str, str] = {
    "english": "en",
    "ingles": "en",
    "inglés": "en",
    "spanish": "es",
    "espanol": "es",
    "español": "es",
    "castellano": "es",
    "german": "de",
    "aleman": "de",
    "alemán": "de",
    "japanese": "ja",
    "japones": "ja",
    "japonés": "ja",
    "korean": "ko",
    "coreano": "ko",
    "chinese": "zh",
    "mandarin": "zh",
    "chino": "zh",
    "ukrainian": "uk",
    "ucraniano": "uk",
    "vietnamese": "vi",
    "vietnamita": "vi",
    "arabic": "ar",
    "arabe": "ar",
    "árabe": "ar",
    "filipino": "tl",
    "tagalog": "tl",
}

_PORTAUDIO_HELP = (
    "PortAudio is required for microphone input but was not found. "
    "Install it (Debian/Ubuntu: 'sudo apt install libportaudio2') or point "
    "PHOSON_STT_PORTAUDIO at a libportaudio.so.2 file or directory. "
    "File transcription (transcribe_audio) works without PortAudio."
)


class SttUnavailable(RuntimeError):
    """Raised when the STT runtime or its audio backend is not usable."""


def normalize_language(value: str | None) -> str:
    """Return a validated Moonshine language code.

    Accepts an ISO code (``es``), a friendly name (``spanish``) or an empty
    value (which falls back to :data:`DEFAULT_LANGUAGE`). Raises
    :class:`ValueError` for anything unsupported so callers can report it.
    """
    if value is None:
        return DEFAULT_LANGUAGE
    token = value.strip().lower()
    if not token:
        return DEFAULT_LANGUAGE
    token = _ALIASES.get(token, token)
    if token not in LANGUAGES:
        supported = ", ".join(sorted(LANGUAGES))
        raise ValueError(f"Unsupported language {value!r}. Supported: {supported}")
    return token


def _moonshine():
    """Import and return the ``moonshine_voice`` module, or raise."""
    try:
        return importlib.import_module("moonshine_voice")
    except ImportError as exc:  # pragma: no cover - depends on install
        raise SttUnavailable(
            "moonshine-voice is not installed. Install it with: "
            "pip install 'phoson-engine-minimal[stt]' (or pip install moonshine-voice)."
        ) from exc


def _find_portaudio() -> str | None:
    """Locate a ``libportaudio.so`` outside the system linker cache.

    Only consulted when the normal ``sounddevice`` import fails. Set
    ``PHOSON_STT_PORTAUDIO`` to a library file or a directory to search, or
    drop the library under ``~/.cache/phoson/portaudio``.
    """
    configured = os.environ.get("PHOSON_STT_PORTAUDIO")
    roots: list[Path] = []
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        roots.append(path)
    roots.append(Path("~/.cache/phoson/portaudio").expanduser())
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("libportaudio.so*")):
            if candidate.is_file():
                return str(candidate)
    return None


def _ensure_portaudio() -> None:
    """Make ``sounddevice`` importable, or raise a clear :class:`SttUnavailable`."""
    try:
        importlib.import_module("sounddevice")
        return
    except ImportError as exc:  # pragma: no cover - depends on install
        raise SttUnavailable(
            "sounddevice is not installed. Install the STT extra: "
            "pip install 'phoson-engine-minimal[stt]'."
        ) from exc
    except OSError:
        pass

    library = _find_portaudio()
    if library is None:
        raise SttUnavailable(_PORTAUDIO_HELP)

    original = ctypes.util.find_library
    ctypes.util.find_library = lambda name: (  # noqa: ARG005 - single name used
        library if name == "portaudio" else original(name)
    )
    try:
        importlib.import_module("sounddevice")
    except OSError as exc:  # pragma: no cover - depends on host libs
        raise SttUnavailable(_PORTAUDIO_HELP) from exc


#: File descriptor the Moonshine C library logs to directly.
_STDERR_FD = 2


@contextlib.contextmanager
def _quiet_native_stderr():
    """Silence the Moonshine C library's direct writes to ``stderr``.

    The native library logs to file descriptor 2 (the license notice,
    tokenizer warnings, download progress). Inside a full-screen TUI those
    writes are painted into the frame and corrupt it — they look like input
    text and can wedge the front end — so they are redirected to
    ``/dev/null`` for the duration of each native call. Only the direct fd
    writes are suppressed: Python exceptions still propagate, and stdout
    (the TUI paint channel) is never touched.
    """
    try:
        saved = os.dup(_STDERR_FD)
    except OSError:
        yield
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        os.close(saved)
        yield
        return
    try:
        os.dup2(devnull, _STDERR_FD)
        yield
    finally:
        try:
            os.dup2(saved, _STDERR_FD)
        finally:
            os.close(saved)
            os.close(devnull)


class SttEngine:
    """Stateful Moonshine wrapper caching nothing but the chosen model arch.

    A fresh transcriber is built per call: Moonshine transcriber sessions are
    single-use, and the underlying model files/ONNX sessions are cached by the
    library, so repeated calls stay cheap after the first download.
    """

    def __init__(self, *, model_arch: int | None = None) -> None:
        self.model_arch = model_arch

    def _apply_arch(self, builder, moonshine):
        if self.model_arch is None:
            return builder
        return builder.model_arch(moonshine.ModelArch(self.model_arch))

    def transcribe_microphone(
        self,
        language: str,
        seconds: float | None = DEFAULT_SECONDS,
        *,
        stop: threading.Event | None = None,
        on_partial: Callable[[str], None] | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> str:
        """Listen to the default microphone and return the text.

        *seconds* is an optional cap: ``None`` (the default) keeps listening
        until *stop* is set — push-to-talk has no time limit. Pass a positive
        number for a fixed window (clamped to :data:`MAX_SECONDS`). A caller
        that passes ``None`` for both must pass *stop*, otherwise the capture
        would never end; without one it falls back to :data:`MAX_SECONDS`.

        Runs blocking work; call it off the event loop (e.g.
        ``asyncio.to_thread``). Passing *stop* lets a caller cut the capture
        short (e.g. the user pressing the push-to-talk key again). Returns the
        finalized lines joined by newlines.
        """
        lines: list[str] = []

        duration: float | None
        if seconds is None:
            # Unlimited: rely on *stop*. Without one, fall back to the cap so
            # the capture can never hang forever.
            duration = None if stop is not None else MAX_SECONDS
        else:
            duration = max(0.5, min(float(seconds), MAX_SECONDS))

        def _handle_line(line) -> None:
            text = getattr(line, "text", str(line)).strip()
            if text:
                lines.append(text)
                if on_line is not None:
                    on_line(text)

        with _quiet_native_stderr():
            _ensure_portaudio()
            moonshine = _moonshine()

            mic = moonshine.MicTranscriber().language(language)
            mic = self._apply_arch(mic, moonshine)
            if on_partial is not None:
                mic = mic.on_text(on_partial)
            mic = mic.on_line(_handle_line)

            mic.load()
            mic.start()
            try:
                deadline = None if duration is None else time.monotonic() + duration
                while True:
                    if stop is not None and stop.is_set():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
            finally:
                mic.stop()
                mic.close()

        return "\n".join(lines).strip()

    def transcribe_wav(self, path: str | Path, language: str) -> str:
        """Transcribe a WAV file offline (no PortAudio required)."""
        source = Path(path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"Audio file not found: {source}")

        lines: list[str] = []

        with _quiet_native_stderr():
            moonshine = _moonshine()
            model_path, arch = moonshine.get_model_for_language(
                language, self._arch(moonshine)
            )
            transcriber = moonshine.Transcriber(model_path=model_path, model_arch=arch)
            stream = transcriber.create_stream(update_interval=0.5)
            stream.start()

            class _Collector(moonshine.TranscriptEventListener):
                def __init__(self, *args, **kwargs) -> None:  # noqa: ARG002 - host signature
                    pass

                def on_line_completed(self, event) -> None:
                    text = getattr(event.line, "text", "").strip()
                    if text:
                        lines.append(text)

            stream.add_listener(_Collector())
            audio, sample_rate = moonshine.load_wav_file(str(source))
            stream.add_audio(audio, sample_rate)
            stream.stop()
        return "\n".join(lines).strip()

    def _arch(self, moonshine):
        if self.model_arch is None:
            return None
        return moonshine.ModelArch(self.model_arch)
