"""Unit tests for the Moonshine speech-to-text plugin.

Push-to-talk dictation is exercised through ``SttPlugin.dictate`` (the
duck-typed entry point the CLI binds to ``Ctrl+O``). These tests never touch
the microphone, the network or the optional ``moonshine-voice`` runtime: the
engine is faked where needed, so they run in the base test environment.
"""

import os
import time
import asyncio
import importlib

import pytest

import phoson_plugin_stt.engine as engine_mod
from phoson_plugin_stt import (
    LANGUAGES,
    SttEngine,
    SttPlugin,
    SttUnavailable,
    create_plugin,
    parse_model_arch,
    normalize_language,
)

# ── Language / model selection ──────────────────────────────────────────


def test_normalize_language_accepts_code_alias_and_default():
    assert normalize_language("es") == "es"
    assert normalize_language("SPANISH") == "es"
    assert normalize_language("japonés") == "ja"
    assert normalize_language(None) == "es"
    assert normalize_language("  ") == "es"


def test_normalize_language_rejects_unknown():
    with pytest.raises(ValueError, match="Unsupported language"):
        normalize_language("fr")


def test_parse_model_arch():
    assert parse_model_arch(None) is None
    assert parse_model_arch("small_streaming") == 4
    assert parse_model_arch(2) == 2
    with pytest.raises(ValueError, match="Unknown model_arch"):
        parse_model_arch("gigantic")


# ── Plugin contract ─────────────────────────────────────────────────────


def test_plugin_identity_and_metadata():
    plugin = create_plugin()
    assert plugin.name == "stt"
    assert plugin.description
    assert isinstance(plugin, SttPlugin)


def test_plugin_is_multilingual():
    assert len(LANGUAGES) >= 5
    assert {"es", "en", "zh", "ja"} <= set(LANGUAGES)


def test_plugin_exposes_no_slash_commands():
    """Dictation is push-to-talk (Ctrl+O), not a slash command."""
    assert create_plugin().get_commands() == []


def test_configure_reads_language_seconds_and_arch():
    plugin = create_plugin()
    plugin.configure({"language": "en", "seconds": 3, "model_arch": "tiny_streaming"})
    assert plugin.language == "en"
    assert plugin.seconds == 3.0
    assert plugin.model_arch == 2


def test_configure_falls_back_on_bad_values():
    plugin = create_plugin()
    plugin.configure({"language": "klingon", "seconds": "abc", "model_arch": "huge"})
    assert plugin.language == engine_mod.DEFAULT_LANGUAGE
    assert plugin.seconds == engine_mod.DEFAULT_SECONDS
    assert plugin.model_arch is None


def test_configure_reads_delivery_flags():
    plugin = create_plugin()
    plugin.configure({"insert_into_prompt": False, "stream_preview": False})
    assert plugin.insert_into_prompt is False
    assert plugin.stream_preview is False
    plugin.configure({})
    assert plugin.insert_into_prompt is False  # unchanged when absent


# ── Lazy runtime / failure surfaces ─────────────────────────────────────


def test_moonshine_is_imported_lazily():
    importlib.import_module("phoson_plugin_stt")
    assert callable(engine_mod._moonshine)


def test_engine_reports_missing_runtime(monkeypatch):
    def boom(name):
        if name == "moonshine_voice":
            raise ImportError("missing")
        return importlib.import_module(name)

    monkeypatch.setattr(engine_mod.importlib, "import_module", boom)
    with pytest.raises(SttUnavailable, match="moonshine-voice"):
        engine_mod._moonshine()


def test_transcribe_wav_missing_file_before_runtime(monkeypatch):
    monkeypatch.setattr(engine_mod, "_moonshine", lambda: object())
    with pytest.raises(FileNotFoundError):
        SttEngine().transcribe_wav("/does/not/exist.wav", "es")


# ── Fakes ───────────────────────────────────────────────────────────────


class _FakeUI:
    def __init__(self):
        self.published = []

    def publish(self, block):
        self.published.append(block)


class _FakeContext:
    plugin_name = "stt"
    session_id = "test"

    def __init__(self):
        self.notices = []
        self.ui = _FakeUI()

    def notify(self, kind, message):
        self.notices.append((kind, message))


class _PrefillContext(_FakeContext):
    """Context whose host can place text in the prompt."""

    def __init__(self, ok: bool = True):
        super().__init__()
        self.prefill_calls: list[str] = []
        self._ok = ok

    def prefill_prompt(self, text):
        self.prefill_calls.append(text)
        return self._ok


class _StreamContext(_PrefillContext):
    """Context whose host also supports live prompt previews."""

    def __init__(self, ok: bool = True, stream_ok: bool = True):
        super().__init__(ok=ok)
        self.stream_calls: list[str] = []
        self._stream_ok = stream_ok

    def stream_prompt_text(self, text):
        self.stream_calls.append(text)
        return self._stream_ok


class _ScriptedEngine:
    """Fake engine that emits partials over time, then a final line."""

    def __init__(self, partials=(), final="", delay: float = 0.25):
        self.partials = list(partials)
        self.final = final
        self.delay = delay
        self.calls: list[tuple[str, float]] = []

    def transcribe_microphone(
        self, language, seconds, *, stop=None, on_line=None, on_partial=None
    ):
        self.calls.append((language, seconds))
        for text in self.partials:
            if on_partial is not None:
                on_partial(text)
            time.sleep(self.delay)
        if on_line is not None and self.final:
            on_line(self.final)
        return self.final


class _BlockingEngine:
    """Fake engine whose capture runs until ``stop`` is set."""

    def __init__(self, line: str = ""):
        self.stop_seen = None
        self.line = line

    def transcribe_microphone(
        self, language, seconds, *, stop=None, on_line=None, on_partial=None
    ):
        self.stop_seen = stop
        if on_line is not None and self.line:
            on_line(self.line)
        while not stop.is_set():
            time.sleep(0.02)
        return ""


# ── Dictation outcome ───────────────────────────────────────────────────


def test_dictate_prefills_prompt_when_supported():
    plugin = create_plugin()
    plugin._engine_cache = _ScriptedEngine(final="hola mundo", delay=0)
    ctx = _PrefillContext(ok=True)

    asyncio.run(plugin.dictate(ctx))

    assert ctx.prefill_calls == ["hola mundo "]
    assert ctx.ui.published == []  # no duplicate card
    assert any("prompt" in msg.lower() for _kind, msg in ctx.notices)


def test_dictate_falls_back_to_card_without_prefill():
    plugin = create_plugin()
    plugin._engine_cache = _ScriptedEngine(final="hola", delay=0)
    ctx = _FakeContext()  # host without prefill_prompt

    asyncio.run(plugin.dictate(ctx))

    assert [block.message for block in ctx.ui.published] == ["hola"]


def test_dictate_respects_insert_into_prompt_false():
    plugin = create_plugin()
    plugin.insert_into_prompt = False
    plugin._engine_cache = _ScriptedEngine(final="hola", delay=0)
    ctx = _PrefillContext(ok=True)

    asyncio.run(plugin.dictate(ctx))

    assert ctx.prefill_calls == []
    assert [block.message for block in ctx.ui.published] == ["hola"]


def test_dictate_reports_no_speech():
    plugin = create_plugin()
    plugin._engine_cache = _ScriptedEngine(final="", delay=0)
    ctx = _FakeContext()

    asyncio.run(plugin.dictate(ctx))

    assert any("No speech" in msg for _kind, msg in ctx.notices)


def test_dictate_reports_missing_runtime(monkeypatch):
    plugin = create_plugin()
    monkeypatch.setattr(
        plugin,
        "_engine",
        lambda: (_ for _ in ()).throw(SttUnavailable("no runtime")),
    )
    ctx = _FakeContext()

    asyncio.run(plugin.dictate(ctx))

    assert any(kind == "error" for kind, _ in ctx.notices)


# ── Live preview ────────────────────────────────────────────────────────


def test_dictate_streams_live_preview_into_prompt():
    plugin = create_plugin()
    plugin._engine_cache = _ScriptedEngine(
        partials=["hola", "hola mun"], final="hola mundo"
    )
    ctx = _StreamContext(ok=True)

    asyncio.run(plugin.dictate(ctx))

    assert any("hola" in call for call in ctx.stream_calls)
    assert ctx.stream_calls[-1] == ""  # preview cleared before the final text
    assert ctx.prefill_calls == ["hola mundo "]
    assert ctx.ui.published == []


def test_dictate_without_preview_support_prefills_once():
    plugin = create_plugin()
    plugin._engine_cache = _ScriptedEngine(partials=["hola"], final="hola mundo")
    ctx = _PrefillContext(ok=True)  # no stream_prompt_text

    asyncio.run(plugin.dictate(ctx))

    assert ctx.prefill_calls == ["hola mundo "]


def test_dictate_respects_stream_preview_false():
    plugin = create_plugin()
    plugin.stream_preview = False
    plugin._engine_cache = _ScriptedEngine(partials=["hola"], final="hola mundo")
    ctx = _StreamContext(ok=True)

    asyncio.run(plugin.dictate(ctx))

    assert ctx.stream_calls == []
    assert ctx.prefill_calls == ["hola mundo "]


def test_dictate_falls_back_when_preview_rejected():
    plugin = create_plugin()
    plugin._engine_cache = _ScriptedEngine(
        partials=["hola", "hola mundo"], final="hola mundo"
    )
    ctx = _StreamContext(ok=True, stream_ok=False)

    asyncio.run(plugin.dictate(ctx))

    assert len(ctx.stream_calls) == 1  # first rejection disables streaming
    assert ctx.prefill_calls == ["hola mundo "]


# ── Toggle (Ctrl+O twice) ───────────────────────────────────────────────


def test_dictate_toggle_stops_and_keeps_partial():
    plugin = create_plugin()
    engine = _BlockingEngine("lo que alcancé a decir")
    plugin._engine_cache = engine
    ctx = _FakeContext()

    async def scenario():
        first = asyncio.create_task(plugin.dictate(ctx))
        for _ in range(200):
            if engine.stop_seen is not None:
                break
            await asyncio.sleep(0.01)
        await plugin.dictate(ctx)  # second press: stop
        with pytest.raises(asyncio.CancelledError):
            await first

    asyncio.run(scenario())

    assert engine.stop_seen is not None and engine.stop_seen.is_set()
    assert [block.message for block in ctx.ui.published] == ["lo que alcancé a decir"]
    assert any("stopped" in msg.lower() for _kind, msg in ctx.notices)


def test_dictate_uses_configured_language_and_seconds():
    plugin = create_plugin()
    plugin.configure({"language": "en", "seconds": 4})
    engine = _ScriptedEngine(final="hello", delay=0)
    plugin._engine_cache = engine

    asyncio.run(plugin.dictate(_PrefillContext()))

    assert engine.calls == [("en", 4.0)]


# ── Native-stderr suppression ───────────────────────────────────────────


def test_quiet_native_stderr_suppresses_fd2_then_restores(capfd):
    from phoson_plugin_stt.engine import _quiet_native_stderr

    with _quiet_native_stderr():
        os.write(2, b"NATIVE-LIBRARY-NOISE\n")
    os.write(2, b"visible-again\n")

    _out, err = capfd.readouterr()
    assert "NATIVE-LIBRARY-NOISE" not in err
    assert "visible-again" in err


def test_quiet_native_stderr_leaves_stdout_alone(capfd):
    from phoson_plugin_stt.engine import _quiet_native_stderr

    with _quiet_native_stderr():
        os.write(1, b"paint-channel\n")

    out, _err = capfd.readouterr()
    assert "paint-channel" in out


# ── No time limit (push-to-talk) ────────────────────────────────────────


def test_seconds_defaults_to_unlimited():
    assert create_plugin().seconds is None
    assert engine_mod.DEFAULT_SECONDS is None


def test_clamp_seconds():
    from phoson_plugin_stt._plugin import _clamp_seconds

    assert _clamp_seconds(None) is None
    assert _clamp_seconds(0) is None
    assert _clamp_seconds(-3) is None
    assert _clamp_seconds(5) == 5.0
    assert _clamp_seconds(9999) == engine_mod.MAX_SECONDS
    assert _clamp_seconds("abc") is None  # falls back to DEFAULT_SECONDS


def test_configure_zero_seconds_means_unlimited():
    plugin = create_plugin()
    plugin.configure({"seconds": 0})
    assert plugin.seconds is None
    plugin.configure({"seconds": 30})
    assert plugin.seconds == 30.0


def test_dictate_passes_no_limit_to_the_engine_by_default():
    plugin = create_plugin()
    engine = _ScriptedEngine(final="hola", delay=0)
    plugin._engine_cache = engine

    asyncio.run(plugin.dictate(_PrefillContext()))

    assert engine.calls == [("es", None)]


def test_listening_notice_omits_the_window_when_unlimited():
    plugin = create_plugin()
    plugin._engine_cache = _ScriptedEngine(final="hola", delay=0)
    ctx = _PrefillContext()

    asyncio.run(plugin.dictate(ctx))

    assert any(msg.startswith("🎙️  Listening (es)") for _kind, msg in ctx.notices)
