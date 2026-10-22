"""Session titles: heuristic seed + optional one-shot LLM upgrade (#55).

:class:`SessionTitler` owns the title state of one
:class:`~phoson_cli.controller.SessionController` session:

- :meth:`SessionTitler.ensure_title` seeds a cheap heuristic title from the
  first real user message on every save, so the session picker is never
  empty;
- after a completed turn, :meth:`SessionTitler.schedule_llm_title` upgrades
  it once, in the background, with a tool-free model call;
- a title the user set with ``/title`` (:meth:`note_user_title`) or a
  resumed session's stored title is never overwritten.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from phoson_llm.schemas import Message, TextBlock, ModelConfig

from .config import PhosonConfig

if TYPE_CHECKING:
    from .controller import SessionController

_LOGGER = logging.getLogger("phoson_cli.controller")

#: Prompt for the LLM session-title call (#55 follow-up). Tool-free, tiny and
#: temperature-cold: the model only has to name the conversation, nothing else.
_TITLE_PROMPT = (
    "Name this conversation with a short, specific title of 3 to 6 words. "
    "Use the same language as the message. Reply with the title only — no "
    "quotes, no trailing punctuation, no explanation.\n\n"
    "First user message:\n{message}"
)

#: Cap on how much of the first message is sent to the title model.
_TITLE_INPUT_CHARS = 1200

#: Cap on the generated title length (chars); longer replies are truncated.
_TITLE_MAX_CHARS = 80

#: Output cap for the title call. Generous on purpose: a reasoning model can
#: spend the budget "thinking" before emitting the title, and a tiny cap (e.g.
#: 32) can leave the completion with no content at all.
_TITLE_MAX_TOKENS = 256

#: Fallback timeout when the config is missing the field (matches the default).
_TITLE_TIMEOUT_FALLBACK = 30.0


def _sanitize_title(raw: str) -> str:
    """Reduce a model reply to a clean one-line session title.

    Strips surrounding markdown/quote noise, keeps the first non-empty line,
    collapses whitespace and truncates. Returns ``""`` when nothing usable is
    left, so the caller can keep the heuristic title instead.
    """
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if not text:
        return ""
    # Models like to wrap the title in quotes or markdown bullets/headings.
    text = text.splitlines()[0].strip()
    text = text.strip("“”\"'`*#-—• \t")
    text = " ".join(text.split())
    if len(text) > _TITLE_MAX_CHARS:
        text = text[: _TITLE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _title_candidate_models(config: PhosonConfig, current_model: str) -> list[str]:
    """Ordered, de-duplicated models to try for the title call.

    ``title_model`` → ``subagent_model`` → the active model. The subagent
    default can belong to a *different* provider than the active one (e.g. a
    Gemini id while the provider is DeepSeek), so the active model is the
    guaranteed-good last resort — the title never silently falls back to the
    heuristic just because a cheap model is stale.
    """
    models: list[str] = []
    for model in (
        getattr(config, "title_model", ""),
        getattr(config, "subagent_model", ""),
        current_model,
    ):
        if model and model not in models:
            models.append(model)
    return models


class SessionTitler:
    """Title state and generation for the controller's active session."""

    _WAKE_TITLE_PREFIXES = (
        "[MONITOR EVENTS]",
        "[BACKGROUND JOB EVENTS]",
        "[PEER MESSAGES]",
    )

    #: Banner que la compactación inyecta como mensaje `user` (ver
    #: ``_compact_history`` / ``summarizer``). No es texto del usuario: se
    #: **omite** (a diferencia de los wake prefixes, que cortan la búsqueda)
    #: porque en un historial compactado los turnos reales vienen detrás, y sin
    #: esto la sesión acababa titulada como "[Conversation summary …]".
    _TITLE_MARKER_PREFIXES = ("[Conversation summary",)

    def __init__(self, host: "SessionController") -> None:
        self._host = host
        # One background task per session, best-effort: the heuristic title is
        # applied synchronously so the picker is never empty, then this
        # upgrades it. ``is_auto`` marks a title *we* set (heuristic or
        # generated) so a user ``/title`` always wins; ``_llm_attempted``
        # makes the call one-shot.
        self._task: asyncio.Task | None = None
        self._llm_attempted = False
        self.is_auto = False

    def note_user_title(self) -> None:
        """Mark the session title as user-set (``/title``).

        A no-op when it is already user-set; it exists so a background LLM
        title call that is still in flight cannot overwrite a title the user
        chose (#55 follow-up).
        """
        self.is_auto = False

    def first_user_text(self) -> str | None:
        """First genuine user message text, or ``None``.

        Skips command-only inputs (``/...``) and injected autonomous wake
        turns, which make poor titles — and returns ``None`` at the first
        such message so a command-only or wake-only session stays untitled
        rather than getting named after a system banner.

        Compaction markers (see ``_TITLE_MARKER_PREFIXES``) are *skipped*
        instead of aborting: a rebuilt history starts with the summary banner,
        and the real turns it kept come right after it.
        """
        for node in self._host.tree.nodes.values():
            msg = node.message
            if msg.role != "user":
                continue
            content = msg.content
            text = (
                content
                if isinstance(content, str)
                else " ".join(
                    b.text for b in content if isinstance(b, TextBlock) and b.text
                )
            ).strip()
            if not text:
                continue
            stripped = text.lstrip()
            if text.startswith("/") or stripped.startswith(self._WAKE_TITLE_PREFIXES):
                return None
            if stripped.startswith(self._TITLE_MARKER_PREFIXES):
                continue
            return text
        return None

    def ensure_title(self) -> None:
        """Seed a session title from the first user message (#55).

        Cheap, synchronous heuristic — first line of the first user message,
        truncated to 60 chars — so the picker is never empty while the
        optional LLM title (see :meth:`schedule_llm_title`) is generated in
        the background. Only fires when the session has no title yet; the
        user's explicit ``/title`` always wins.

        A stored title that is itself a compaction banner (sessions saved
        before the marker filter existed) is repaired: it is replaced by the
        first real user turn, or cleared when the history holds none.
        """
        current = (self._host.tree.title or "").strip()
        if current and not current.lstrip().startswith(self._TITLE_MARKER_PREFIXES):
            return
        text = self.first_user_text()
        if not text:
            if current:
                # Nada de donde titular (historial reducido al banner): mejor
                # sin título — la UI cae a su propio fallback — que con basura.
                self._host.tree.title = None
            return
        first_line = text.splitlines()[0].strip()
        title = first_line[:57] + "…" if len(first_line) > 60 else first_line
        if title:
            self._host.tree.title = title
            self.is_auto = True

    def schedule_llm_title(self) -> None:
        """Kick off the one-shot background LLM title call (best-effort).

        Only for titles this titler auto-generated (``is_auto``),
        so a session resumed with a stored title — or one the user named with
        ``/title`` — is never renamed. No-op when disabled, already attempted,
        or there is no running loop to own the task.
        """
        if not getattr(self._host.config, "llm_titles", False):
            return
        if self._llm_attempted or not self.is_auto:
            return
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover — _save_session always has a loop
            return
        self._llm_attempted = True
        self._task = loop.create_task(self.generate_llm_title(), name="session:title")

    async def generate_llm_title(self) -> None:
        """Generate a short session title with one tool-free model call.

        Tries each candidate model in order (see
        :func:`_title_candidate_models`) until one returns a usable title, so
        a cheap model that does not belong to the active provider degrades to
        the active model instead of leaving the heuristic title in place.
        Never raises: every error/timeout/empty reply is swallowed.
        """
        text = self.first_user_text()
        if not text:
            return
        # Bind the title to the session it was generated for: a /new or
        # /resume can swap ``self._host.tree`` while the call is in flight.
        session_id = self._host.tree.session_id
        tree = self._host.tree
        prompt = _TITLE_PROMPT.format(message=text[:_TITLE_INPUT_CHARS])

        candidates = _title_candidate_models(
            self._host.config, self._host.current_model
        )
        title = ""
        last_error = ""
        for model in candidates:
            title, last_error = await self.call_title_model(model, prompt)
            if title:
                break
        if not title:
            tried = ", ".join(candidates)
            detail = f" — last error: {last_error}" if last_error else ""
            _LOGGER.warning(
                "LLM session title: no model produced a title (tried %s)%s",
                tried,
                detail,
            )
            return
        # The user may have run /title, or switched session, while the call
        # was in flight — in both cases the title must not be applied.
        if not self.is_auto or self._host.tree is not tree:
            return
        tree.title = title
        try:
            await self._host.storage.save_meta(session_id, {"title": title})
        except Exception:  # noqa: BLE001 — persistence is best-effort too
            _LOGGER.warning("Could not persist generated session title", exc_info=True)
        # Repaint ancillary chrome (the full-screen header reads ``tree.title``
        # every frame) without adding a visible notice to the transcript. The
        # hook is optional: front ends with no chrome simply do nothing.
        refresh = getattr(self._host.sink, "on_session_title", None)
        if callable(refresh):
            refresh()

    async def call_title_model(self, model: str, prompt: str) -> tuple[str, str]:
        """One title attempt against *model*.

        Returns ``(title, error)``: a non-empty title on success, otherwise
        ``""`` and a short human-readable error (surfaced in the final warning
        so a failing title call is diagnosable instead of silent).
        """
        request = ModelConfig(
            model=model,
            temperature=0.2,
            max_tokens=_TITLE_MAX_TOKENS,
            system=None,
            # Naming needs no chain-of-thought; disabling it keeps the call
            # fast and stops a reasoning model from burning the small output
            # budget on thinking and returning nothing (OpenRouter honors
            # ``think=False`` as a per-request ``reasoning`` opt-out).
            think=False,
        )
        try:
            done = await asyncio.wait_for(
                self._host.chat.complete(
                    [Message(role="user", content=prompt)],
                    request,
                ),
                timeout=float(
                    getattr(
                        self._host.config, "title_timeout_s", _TITLE_TIMEOUT_FALLBACK
                    )
                ),
            )
        except Exception as exc:  # noqa: BLE001 — a title failure must never surface
            _LOGGER.warning(
                "LLM session title call failed for model %s", model, exc_info=True
            )
            return "", f"{type(exc).__name__}: {exc}"[:200]
        return _sanitize_title(getattr(done, "content", "") or ""), ""

    def reset(self) -> None:
        """Forget the title state (new or loaded session).

        Any in-flight title task for the previous session self-guards on the
        tree identity and will not apply.
        """
        self.is_auto = False
        self._llm_attempted = False
        self._task = None

    async def aclose(self) -> None:
        """Let an in-flight title call finish briefly, then cancel it.

        Gives the task a chance to persist before the chat client closes; its
        own call is time-bounded already. Never raises.
        """
        task, self._task = self._task, None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:  # noqa: BLE001 — shutdown must not raise
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


__all__ = ["SessionTitler"]
