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

from collections.abc import Iterable

from prompt_toolkit.document import Document
from prompt_toolkit.completion import (
    Completer,
    Completion,
    WordCompleter,
    FuzzyCompleter,
)

from ..commands import COMMANDS, COMMAND_SPECS
from .model_cache import ModelCache

_CMD_META: dict[str, str] = {
    name: spec.help for spec in COMMAND_SPECS for name in spec.names
}

_MODEL_ARG_PREFIXES = ("/model ", "/subagent-model ")


class SlashCompleter(Completer):
    """Completes slash commands only when the buffer starts with '/'."""

    def get_completions(
        self, document: Document, complete_event: object
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
        self, document: Document, complete_event: object
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


class StaticArgCompleter(Completer):
    """Fuzzy-completes a command's argument from a small fixed word list.

    Same "type and autocomplete, no modal" pattern as
    :class:`ModelArgCompleter`, for commands whose valid values are
    known upfront (e.g. ``/reasoning-effort <low|medium|high|off>``)
    rather than fetched from a provider.
    """

    def __init__(self, prefixes: tuple[str, ...], words: list[str]) -> None:
        self._prefixes = prefixes
        self._inner = FuzzyCompleter(WordCompleter(words, sentence=True))

    def get_completions(
        self, document: Document, complete_event: object
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        for prefix in self._prefixes:
            if text.startswith(prefix):
                query = text[len(prefix) :]
                sub_document = Document(query, len(query))
                yield from self._inner.get_completions(sub_document, complete_event)
                return


__all__ = ["SlashCompleter", "ModelArgCompleter", "StaticArgCompleter"]
