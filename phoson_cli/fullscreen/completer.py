"""Completers for the full-screen input line.

Slash-command completion mirrors the classic REPL's completer
(``phoson_cli.repl``), sourced directly from ``COMMAND_SPECS``/
``COMMANDS`` so both stay in sync with ``/help`` and the dispatch table
without depending on ``repl.py``'s prompt-loop code.

Model-id completion for ``/model``/``/subagent-model`` follows the
reference prototype's approach (cli_abel.py's ``ChatCommandCompleter``):
a plain fuzzy dropdown fed by a background-refreshed id list, rather
than a modal picker — picking a model is just "type and autocomplete",
same as any other command.
"""

from collections.abc import Callable, Iterable

from prompt_toolkit.document import Document
from prompt_toolkit.completion import (
    Completer,
    Completion,
    CompleteEvent,
    WordCompleter,
    FuzzyCompleter,
)

from ..commands import COMMANDS, COMMAND_SPECS
from .model_cache import ModelCache
from .session_cache import SessionListCache

_CMD_META: dict[str, str] = {
    name: spec.help for spec in COMMAND_SPECS for name in spec.names
}

_MODEL_ARG_PREFIXES = ("/model ", "/subagent-model ")


class SlashCompleter(Completer):
    """Completes slash commands only when the buffer starts with '/'."""

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        # Only complete the command word itself (no args)
        if " " in text:
            return

        word = text.lower()
        for cmd in sorted(COMMANDS):
            if cmd.startswith(word):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=_CMD_META.get(cmd, ""),
                )


class ModelArgCompleter(Completer):
    """Fuzzy-completes the model id argument of /model and /subagent-model."""

    def __init__(self, cache: ModelCache) -> None:
        self._inner = FuzzyCompleter(
            WordCompleter(lambda: self._cache.model_ids, sentence=True)
        )
        self._cache = cache

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        for prefix in _MODEL_ARG_PREFIXES:
            if text.startswith(prefix):
                query = text[len(prefix) :]
                # start_position is a relative offset (chars back from the
                # cursor), so completions from this sub-document apply
                # unchanged to the real one — same trailing substring.
                sub_document = Document(query, len(query))
                yield from self._inner.get_completions(sub_document, complete_event)
                return


class SessionsArgCompleter(Completer):
    """Fuzzy-completes '/sessions load <n>' with session summaries.

    Like :class:`ModelArgCompleter`, but each dropdown entry shows a
    human-readable label (date · msgs · cost · model) while inserting
    just the number — sessions are UUIDs, so the number is what the
    command actually consumes.
    """

    def __init__(self, cache: SessionListCache) -> None:
        self._cache = cache

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        prefix = "/sessions load "
        if not text.startswith(prefix):
            return
        query = text[len(prefix) :]
        for i, meta in enumerate(self._cache.sessions, start=1):
            label = f"{i}"
            if query and not label.startswith(query):
                continue
            updated = meta.updated_at.strftime("%m-%d %H:%M")
            cost = f"${meta.total_cost:.4f}" if meta.total_cost else "—"
            model = (meta.last_model or "—").split("/")[-1][:24]
            yield Completion(
                label,
                start_position=-len(query),
                display=f"{i}. {updated}  {meta.message_count} msgs  {cost}",
                display_meta=model,
            )


class StaticArgCompleter(Completer):
    """Fuzzy-completes a command's argument from a small fixed word list.

    Same "type and autocomplete, no modal" pattern as
    :class:`ModelArgCompleter`, for commands whose valid values are
    known upfront (e.g. ``/reasoning-effort <low|medium|high|off>``)
    rather than fetched from a provider.

    ``words`` may be a plain list or a zero-arg callable returning the
    list (evaluated per completion pass), so callers can feed dynamic
    values like the currently enabled providers.
    """

    def __init__(
        self, prefixes: tuple[str, ...], words: list[str] | Callable[[], list[str]]
    ) -> None:
        self._prefixes = prefixes
        self._words = words

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        for prefix in self._prefixes:
            if text.startswith(prefix):
                query = text[len(prefix) :]
                words = self._words() if callable(self._words) else self._words
                inner = FuzzyCompleter(WordCompleter(words, sentence=True))
                sub_document = Document(query, len(query))
                yield from inner.get_completions(sub_document, complete_event)
                return


__all__ = [
    "SlashCompleter",
    "ModelArgCompleter",
    "SessionsArgCompleter",
    "StaticArgCompleter",
]
