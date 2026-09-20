"""Multilingual on-device speech-to-text plugin (Moonshine).

Contributes:

- **Push-to-talk dictation** (the CLI binds it to ``Ctrl+O``): the transcript
  is streamed *live* into the user's prompt and left there for review/edit.
  A host finds it by the duck-typed :meth:`SttPlugin.dictate` entry point.
- ``transcribe_audio`` — an engine tool so the agent can transcribe WAV files
  offline in any supported language.

The Moonshine runtime is an optional dependency: loading this plugin never
imports it, so a host without the extra still starts cleanly and only the
calls fail (with an actionable message) when the backend is unavailable.
"""

import os
import asyncio
import threading
from typing import Annotated

from phoson_agent import (
    Plugin,
    NoticeBlock,
    ToolRenderSpec,
    CliCommandContext,
    tool,
)

from .engine import (
    LANGUAGES,
    MAX_SECONDS,
    DEFAULT_SECONDS,
    DEFAULT_LANGUAGE,
    SttEngine,
    SttUnavailable,
    parse_model_arch,
    normalize_language,
)

_SUPPORTED = ", ".join(sorted(LANGUAGES))


def _clamp_seconds(value: object) -> float | None:
    """Normalize a configured duration.

    ``None``, ``0`` or a negative value means "listen until stopped"
    (push-to-talk). A positive value is capped at :data:`MAX_SECONDS`.
    """
    if value is None:
        return None
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    if seconds <= 0:
        return None
    return min(seconds, MAX_SECONDS)


class SttPlugin(Plugin):
    """Moonshine-backed multilingual dictation and file transcription."""

    def __init__(self) -> None:
        self.language: str = os.environ.get("PHOSON_STT_LANGUAGE") or DEFAULT_LANGUAGE
        # ``None`` = no time limit: dictation runs until Ctrl+O again.
        self.seconds: float | None = DEFAULT_SECONDS
        self.model_arch: int | None = None
        # Put the transcript in the user's prompt (editable) instead of only
        # printing it, when the front end supports it.
        self.insert_into_prompt: bool = True
        # Show the partial transcript in the prompt while listening.
        self.stream_preview: bool = True
        self._active: asyncio.Task | None = None
        self._stop: threading.Event = threading.Event()
        self._engine_cache: SttEngine | None = None

    @property
    def name(self) -> str:
        return "stt"

    @property
    def description(self) -> str:
        return "On-device multilingual speech-to-text (Moonshine)."

    def configure(self, config: dict) -> None:
        """Read ``language``, ``seconds`` and ``model_arch`` from plugin config."""
        configured_language = config.get("language") or os.environ.get(
            "PHOSON_STT_LANGUAGE"
        )
        if configured_language:
            try:
                self.language = normalize_language(str(configured_language))
            except ValueError:
                self.language = DEFAULT_LANGUAGE
        if config.get("seconds") is not None:
            self.seconds = _clamp_seconds(config["seconds"])
        raw_arch = config.get("model_arch")
        if raw_arch is None:
            raw_arch = os.environ.get("PHOSON_STT_MODEL_ARCH")
        if raw_arch is not None:
            try:
                self.model_arch = parse_model_arch(raw_arch)
            except ValueError:
                self.model_arch = None
        if config.get("insert_into_prompt") is not None:
            self.insert_into_prompt = bool(config["insert_into_prompt"])
        if config.get("stream_preview") is not None:
            self.stream_preview = bool(config["stream_preview"])
        self._engine_cache = None

    # ── Engine ──────────────────────────────────────────────────────────

    def _engine(self) -> SttEngine:
        if self._engine_cache is None:
            self._engine_cache = SttEngine(model_arch=self.model_arch)
        return self._engine_cache

    # ── Push-to-talk dictation ──────────────────────────────────────────

    async def dictate(self, context: CliCommandContext) -> None:
        """Start dictation — or stop it when a session is already running.

        Called by the CLI's push-to-talk key binding. It must run on the
        host's event loop (the host keeps rendering while it awaits, which is
        what makes the live prompt preview visible).
        """
        if self._active is not None and not self._active.done():
            self._stop.set()
            self._active.cancel()
            return
        self._active = asyncio.current_task()
        try:
            await self._listen(context)
        finally:
            self._active = None

    async def _listen(self, context: CliCommandContext) -> None:
        language, seconds = self.language, self.seconds
        # Lines the engine has finalized, kept here so an early stop can still
        # surface what was heard. ``partial`` is written by the capture thread
        # and polled below to drive the live prompt preview.
        captured: list[str] = []
        partial = {"text": ""}
        stop = self._stop = threading.Event()
        streamed = False
        live_last = ""

        def _on_partial(text: str) -> None:
            partial["text"] = text

        def _preview() -> str:
            done = "\n".join(captured).strip()
            current = partial["text"].strip()
            return f"{done}\n{current}" if done and current else (done or current)

        streaming = self.stream_preview
        try:
            engine = self._engine()
            task = asyncio.ensure_future(
                asyncio.to_thread(
                    engine.transcribe_microphone,
                    language,
                    seconds,
                    stop=stop,
                    on_line=captured.append,
                    on_partial=_on_partial,
                )
            )
            context.notify(
                "info",
                f"🎙️  Listening ({language}, {seconds:g}s) — Ctrl+O to stop…"
                if seconds is not None
                else f"🎙️  Listening ({language}) — Ctrl+O to stop…",
            )
            while not task.done():
                await asyncio.sleep(0.2)
                if not streaming:
                    continue
                live = _preview()
                if live and live != live_last:
                    if self._stream(context, live):
                        streamed, live_last = True, live
                    else:
                        streaming = False  # host cannot repaint mid-capture
            transcript = await task
        except asyncio.CancelledError:
            # Ctrl+O again: stop promptly and keep whatever was heard.
            stop.set()
            if streamed:
                self._stream(context, "")
            heard = "\n".join(captured).strip()
            if heard:
                self._deliver(context, heard)
            context.notify("warn", "Listening stopped.")
            raise
        except SttUnavailable as exc:
            if streamed:
                self._stream(context, "")
            context.notify("error", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never break the REPL
            if streamed:
                self._stream(context, "")
            context.notify("error", f"Transcription failed: {exc}")
            return

        if streamed:
            self._stream(context, "")
        if not transcript:
            context.notify("warn", "No speech detected.")
            return

        self._deliver(context, transcript)

    @staticmethod
    def _stream(context: CliCommandContext, text: str) -> bool:
        """Best-effort live preview update; never raises."""
        stream = getattr(context, "stream_prompt_text", None)
        if not callable(stream):
            return False
        try:
            return bool(stream(text))
        except Exception:  # noqa: BLE001 - a preview must never break a command
            return False

    def _deliver(self, context: CliCommandContext, text: str) -> None:
        """Hand the transcript to the user: prefill the prompt, else a card."""
        if self.insert_into_prompt:
            prefill = getattr(context, "prefill_prompt", None)
            if callable(prefill):
                try:
                    if prefill(text + " "):
                        context.notify(
                            "info",
                            "🎙️  Added to the prompt — edit it and press Enter to send.",
                        )
                        return
                except Exception:  # noqa: BLE001 - fall back to the card
                    pass
        self._show_transcript(context, text)

    @staticmethod
    def _show_transcript(context: CliCommandContext, text: str) -> None:
        """Publish the transcript as a card, falling back to a plain notice."""
        try:
            context.ui.publish(
                NoticeBlock(id="stt-transcript", kind="info", message=text)
            )
        except Exception:  # noqa: BLE001 - hosts without rich cards
            context.notify("info", text)

    # ── Agent tool ──────────────────────────────────────────────────────

    def get_tools(self):
        plugin = self

        @tool
        async def transcribe_audio(
            path: Annotated[str, "Path to a WAV audio file to transcribe"],
            language: Annotated[
                str,
                "Language code or name (e.g. 'es', 'english'); empty uses the "
                "configured default",
            ] = "",
        ) -> str:
            """Transcribe a WAV file to text offline (Moonshine, multilingual).

            Supported languages: ar, de, en, es, ja, ko, tl, uk, vi, zh.
            The first call for a language downloads its model (cached after).
            """
            try:
                chosen = normalize_language(language or plugin.language)
                engine = plugin._engine()
                text = await asyncio.to_thread(engine.transcribe_wav, path, chosen)
            except (SttUnavailable, FileNotFoundError, ValueError) as exc:
                return f"Error: {exc}"
            except Exception as exc:  # noqa: BLE001 - report, never raise
                return f"Error: transcription failed: {exc}"
            return text or "(no speech detected)"

        return [transcribe_audio]

    def get_tool_render_specs(self) -> list[ToolRenderSpec]:
        return [
            ToolRenderSpec(
                tool_name="transcribe_audio", verb="transcribing audio", icon="🎙"
            )
        ]

    async def aclose(self) -> None:
        self._engine_cache = None


def create_plugin() -> SttPlugin:
    """Entry-point factory (``phoson.plugins`` group)."""
    return SttPlugin()
