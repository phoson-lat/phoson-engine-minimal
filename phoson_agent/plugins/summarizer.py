"""Summarization middleware for conversation compaction.

When the conversation exceeds a configurable threshold of the model's
context window (default 80%), old messages are replaced with a generated
summary to keep the conversation within limits.

Uses tiktoken for token estimation. Important caveats:

- ``tiktoken`` only ships tokenizers for OpenAI models. Counts for
  Claude (Anthropic) and Llama-family (Ollama) are *approximations*
  based on cl100k_base, typically within ±10-15% of the real count.
- For exact token counts on Anthropic models, callers should use
  ``anthropic.Anthropic().messages.count_tokens()`` from the SDK.
- The threshold default of 80% leaves enough margin to absorb this
  imprecision; tighten it if you switch to a stricter tokenizer.
"""

import json
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

import tiktoken

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    ErrorEvent,
    TokenEvent,
    UsageEvent,
    ModelConfig,
    ToolUseBlock,
    ToolResultBlock,
)
from phoson_agent.models import AgentEvent
from phoson_agent.middleware import LLMCallNext, AgentMiddleware
from phoson_agent.plugins.context_window import ContextWindowResolver

# ─────────────────────────────────────────────────────────────────────
# Token estimation with tiktoken
# ─────────────────────────────────────────────────────────────────────

# tiktoken encoding mapping by provider.
# Note: cl100k_base / o200k_base are OpenAI-native. For non-OpenAI providers
# we use them as a *best-effort approximation* — the real tokenizer differs.
_ENCODINGS: dict[str, str] = {
    "anthropic": "cl100k_base",  # Claude actual tokenizer differs (~10-15% off)
    "openai": "o200k_base",  # GPT-4o+ uses o200k natively
    "openrouter": "cl100k_base",  # Mixed providers; cl100k is the safe default
    "ollama": "cl100k_base",  # Most Ollama models are Llama-based (~10-15% off)
}

# Overhead tokens per message (role metadata, formatting)
_MSG_OVERHEAD = 4


class TokenEstimator:
    """Estimates token count for messages using tiktoken."""

    def __init__(self, provider: str = "openai") -> None:
        """Initialize the estimator with the provider's encoding.

        Args:
            provider: The LLM provider name (openai, anthropic, openrouter, ollama).
        """
        enc_name = _ENCODINGS.get(provider, "cl100k_base")
        self._encoding = tiktoken.get_encoding(enc_name)

    def count_text(self, text: str) -> int:
        """Count tokens in a raw string."""
        return len(self._encoding.encode(text))

    def count_messages(self, messages: list[Message]) -> int:
        """Estimate total tokens for a list of messages.

        Accounts for content text + per-message overhead.
        For tool_use/tool_result blocks, includes args/result text.
        """
        total = 0
        for msg in messages:
            total += _MSG_OVERHEAD
            if isinstance(msg.content, str):
                total += self.count_text(msg.content)
                continue

            for block in msg.content:
                if isinstance(block, TextBlock):
                    total += self.count_text(block.text)
                elif isinstance(block, ToolUseBlock):
                    total += self.count_text(json.dumps(block.args))
                elif isinstance(block, ToolResultBlock):
                    total += self.count_text(block.result)
                # Multimodal blocks (image/audio/video/document) carry no
                # text payload tiktoken can score; we skip them and let the
                # _MSG_OVERHEAD constant absorb their structural cost.
        return total

    @classmethod
    def for_provider(cls, provider: str) -> "TokenEstimator":
        """Factory method to create a TokenEstimator for a provider."""
        return cls(provider=provider)


# ─────────────────────────────────────────────────────────────────────
# Summary prompt
# ─────────────────────────────────────────────────────────────────────

SUMMARY_PROMPT_TEMPLATE = (
    "You are summarizing a conversation to reduce its token count while "
    "preserving all critical information.\n\n"
    "Instructions:\n"
    "1. Summarize the conversation history below, keeping:\n"
    "   - The user's original goal/task\n"
    "   - Key decisions made\n"
    "   - Important context and constraints\n"
    "   - Results of tool executions (especially file contents, code, data)\n"
    "   - Current progress and what remains to be done\n"
    "2. Be concise but thorough.\n"
    "3. Preserve any code snippets, file paths, or technical details "
    "that are relevant.\n"
    "4. Output ONLY the summary, no preamble.\n\n"
    "Conversation history to summarize:\n\n{history}"
)

#: Structured summary template (IMPROVEMENTS.md E1 — compaction
#: estructurada, patrón Anthropic long-running agents). Fixed sections
#: make the artifact reliably consumable by the next context segment,
#: unlike a free-form summary. ``{reasoning_note}`` is only non-empty
#: when the caller could supply captured reasoning.
STRUCTURED_SUMMARY_TEMPLATE = (
    "Summarize the conversation segment below into a structured handoff "
    "document that an AI assistant will read to continue this work. "
    "Output exactly these sections, in this order, as a markdown document "
    "(skip a section only when it is truly empty):\n\n"
    "## Goal\nThe user's original goal/task, in one or two sentences.\n\n"
    "## Completed\nBulleted list of concrete things finished (files "
    "created/edited, commands run, questions answered).\n\n"
    "## Key decisions\nBulleted list of decisions and the one-line reason "
    "for each.\n\n"
    "## Reasoning highlights\nBulleted distillation of the most important "
    "reasoning steps — the *why* behind key decisions — so continuity is "
    "preserved even when the raw thinking is dropped.\n\n"
    "## Open questions\nAnything unresolved, ambiguous or still to verify.\n\n"
    "## Next steps\nOrdered list of what to do next.\n\n"
    "## Constraints and context\nRelevant constraints, file paths, "
    "environment facts, and technical details that must survive.\n\n"
    "{reasoning_note}"
    "Be concise but complete; keep code snippets and file paths verbatim. "
    "Output ONLY the markdown document, no preamble.\n\n"
    "Conversation segment to summarize:\n\n{history}"
)

_REASONING_NOTE = (
    "The segment below includes the model's captured reasoning for some "
    "assistant turns (marked 'Reasoning:'); distill its essence into the "
    "'Reasoning highlights' section instead of quoting it.\n\n"
)


def _format_messages_for_summary(
    messages: list[Message],
    reasoning_for: dict[int, str] | None = None,
) -> str:
    """Format messages as readable text for the summary LLM call.

    Args:
        messages: The messages to format.
        reasoning_for: Optional mapping of message *index* (within
            ``messages``) to captured reasoning text. When provided and a
            key is present, the reasoning is appended to that message's
            formatted entry — retained-reasoning support (IMPROVEMENTS.md
            E1) so the summary can preserve the chain of thought, not
            just its conclusions.
    """
    parts: list[str] = []
    for index, msg in enumerate(messages):
        role = msg.role.upper()
        entry: list[str]
        if isinstance(msg.content, str):
            entry = [f"[{role}] {msg.content}"]
        else:
            text_parts: list[str] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    args_str = json.dumps(block.args)
                    text_parts.append(f"[Tool: {block.tool_name}({args_str})]")
                elif isinstance(block, ToolResultBlock):
                    error_tag = " [ERROR]" if block.error else ""
                    text_parts.append(f"[Result{error_tag}] {block.result}")
            entry = [f"[{role}] {' '.join(text_parts)}"]

        if (
            msg.role == "assistant"
            and reasoning_for
            and index in reasoning_for
            and reasoning_for[index]
        ):
            entry.append(f"Reasoning:\n{reasoning_for[index]}")
        parts.append("\n".join(entry))
    return "\n\n".join(parts)


def _structured_summary_prompt(
    history_text: str, *, include_reasoning_note: bool
) -> str:
    """Build the structured summary prompt (IMPROVEMENTS.md E1)."""
    return STRUCTURED_SUMMARY_TEMPLATE.format(
        reasoning_note=_REASONING_NOTE if include_reasoning_note else "",
        history=history_text,
    )


# ─────────────────────────────────────────────────────────────────────
# Summarization middleware
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SummarizationEvent(AgentEvent):
    """Emitted when the conversation is compacted."""

    original_tokens: int = 0
    compacted_tokens: int = 0
    messages_removed: int = 0
    summary_length: int = 0


@dataclass
class SummarizationMiddleware(AgentMiddleware):
    """Compacts conversation when it exceeds a threshold of the context window.

    Strategy:
    - System prompt is always preserved intact
    - The last N messages are always preserved (recent context)
    - Intermediate messages are replaced with a single summary message
      generated by the LLM itself

    Usage:
        summarizer = SummarizationMiddleware(
            threshold=0.80,
            min_keep_messages=4,
            provider="openrouter",
            model="anthropic/claude-sonnet-4-6",
        )
        engine = AgentEngine(
            chat=chat, tools=tools, middlewares=[summarizer],
        )
    """

    threshold: float = 0.80
    min_keep_messages: int = 4
    provider: str = "openrouter"
    model: str = ""
    ollama_base_url: str = "http://localhost:11434"
    openrouter_api_key: str | None = None
    vllm_base_url: str | None = None
    summary_prompt_template: str = SUMMARY_PROMPT_TEMPLATE
    #: Generate a structured handoff document instead of a free-form
    #: summary (IMPROVEMENTS.md E1). When True the summary prompt is
    #: built from :data:`STRUCTURED_SUMMARY_TEMPLATE` and any captured
    #: reasoning the caller provides is folded into the summary.
    structured: bool = True
    #: Disables automatic compaction entirely (``/compact off``). Manual
    #: compactions keep working; this only gates :meth:`wrap_llm_call`.
    auto_enabled: bool = True

    # Internal state. Both are constructed in ``__post_init__``; using
    # ``init=False`` keeps them out of the dataclass constructor and out of
    # ``repr()``. They are non-Optional after ``__post_init__`` runs.
    _resolver: ContextWindowResolver = field(init=False, repr=False)
    _estimator: TokenEstimator = field(init=False, repr=False)
    _pending_compact_events: list[SummarizationEvent] = field(
        default_factory=list, repr=False
    )
    # Retained-reasoning alignment (IMPROVEMENTS.md E1). The CLI registers
    # the run's path messages plus the reasoning captured for each of them;
    # compaction looks it up by object identity so the reasoning follows
    # the messages into any later view of the history (the engine's lists
    # and the tree's paths share Message instances). The map is immutable
    # after ``set_retained_reasoning`` — nothing mutates it mid-run — and
    # is cleared via :meth:`clear_retained_reasoning` when the run ends.
    _retained_by_id: dict[int, str] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Initializes the resolver and estimator."""
        self._resolver = ContextWindowResolver(
            ollama_base_url=self.ollama_base_url,
            openrouter_api_key=self.openrouter_api_key,
            vllm_base_url=self.vllm_base_url,
        )
        self._estimator = TokenEstimator.for_provider(self.provider)

    def estimate_tokens(self, messages: list[Message]) -> int:
        """Estimate the token count for a list of messages.

        Delegates to the internal :class:`TokenEstimator` so callers never
        need to reach into private attributes.

        Args:
            messages: The conversation messages to estimate.

        Returns:
            Estimated token count.
        """
        return self._estimator.count_messages(messages)

    # ── Core logic ────────────────────────────────────────────────────

    async def on_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        """Hook called before the LLM call.

        This middleware does not mutate messages here because real compaction
        requires an async LLM call to generate the summary, which can only
        be done in wrap_llm_call.
        """
        return messages

    async def on_agent_event(self, event: AgentEvent) -> None:
        """Hook executed on any agent event."""
        pass

    # ── LLM call wrapper: actually performs the summarization ─────────

    async def wrap_llm_call(
        self,
        call_next: LLMCallNext,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[LLMEvent]:
        """Intercept the LLM call to check if compaction is needed.

        If compaction is needed, we first perform a summarization call,
        then proceed with the compacted messages.
        """
        # Check if compaction is needed
        current_tokens = self._estimator.count_messages(messages)
        if not self.auto_enabled:
            # Automatic compaction disabled (``/compact off``) — pass
            # through untouched; manual compactions are unaffected.
            async for event in call_next(messages, config):
                yield event
            return

        context_window = await self._resolver.resolve(self.provider, self.model)
        threshold_tokens = int(context_window * self.threshold)

        if current_tokens <= threshold_tokens:
            # No compaction needed — pass through
            async for event in call_next(messages, config):
                yield event
            return

        # Separate messages
        system_msgs: list[Message] = []
        others: list[Message] = []
        for msg in messages:
            if msg.role == "system":
                system_msgs.append(msg)
            else:
                others.append(msg)

        if len(others) > self.min_keep_messages:
            keep = others[-self.min_keep_messages :]
        else:
            keep = others
        to_summarize = others[: len(others) - len(keep)]

        if not to_summarize:
            async for event in call_next(messages, config):
                yield event
            return

        # Generate summary — same prompt builder as the manual /compact
        # path so auto and manual compactions produce identical artifacts.
        summary_prompt = self.build_summary_prompt(to_summarize)

        summary_messages = [Message(role="user", content=summary_prompt)]
        # 4096 (not 2048) because the structured handoff document is longer
        # than a free-form paragraph; matches the manual /compact path.
        summary_config = ModelConfig(model=self.model, max_tokens=4096, temperature=0.3)

        summary_text = ""
        async for event in call_next(summary_messages, summary_config):
            # We swallow visual events (start/token/done) from the
            # internal summary call to keep the UX clean, but we MUST
            # forward UsageEvent so the caller can account for the cost
            # of the summarization itself, and ErrorEvent so failures
            # are visible.
            if isinstance(event, TokenEvent):
                summary_text += event.content
                continue
            if isinstance(event, (UsageEvent, ErrorEvent)):
                yield event
                continue
            # Drop LLMStart/LLMDone/Reasoning/ToolCall events — they
            # belong to the internal summary turn, not to the user's.

        # Build compacted messages
        compacted = list(system_msgs)
        if summary_text.strip():
            summary_content = (
                f"[Conversation summary up to this point: {summary_text.strip()}]"
            )
            compacted.append(Message(role="user", content=summary_content))
        compacted.extend(keep)

        compacted_tokens = self._estimator.count_messages(compacted)
        messages_removed = len(messages) - len(compacted)

        self._pending_compact_events.append(
            SummarizationEvent(
                original_tokens=current_tokens,
                compacted_tokens=compacted_tokens,
                messages_removed=messages_removed,
                summary_length=len(summary_text),
            )
        )

        # Now proceed with the actual LLM call using compacted messages
        async for event in call_next(compacted, config):
            yield event

    # ── Accessor for compact events ───────────────────────────────────

    def pop_compact_events(self) -> list[SummarizationEvent]:
        """Return and clear pending SummarizationEvents."""
        events = list(self._pending_compact_events)
        self._pending_compact_events.clear()
        return events

    def format_for_summary(self, messages: list[Message]) -> str:
        """Render *messages* as readable text for a summary LLM call (C2).

        Retained reasoning (E1): when the caller previously registered
        reasoning via :meth:`set_retained_reasoning`, matching messages
        (by object identity) carry their captured reasoning into the
        rendered text.
        """
        return _format_messages_for_summary(
            messages,
            reasoning_for=self._retained_index_for(messages) or None,
        )

    def build_summary_prompt(
        self,
        messages: list[Message],
        *,
        structured: bool | None = None,
        reasoning_for: dict[int, str] | None = None,
    ) -> str:
        """Build the full summary LLM prompt for *messages* (E1).

        This is the single place where the summary prompt is assembled,
        so the auto path (:meth:`wrap_llm_call`) and the manual path
        (``/compact``) produce identical prompts.

        Args:
            messages: The messages to summarize.
            structured: Force the structured or legacy template. When
                ``None`` the middleware's :attr:`structured` flag wins.
            reasoning_for: Mapping of message *index* (within
                ``messages``) to captured reasoning text. When given it
                is used as-is; otherwise the retained reasoning
                registered via :meth:`set_retained_reasoning` is resolved
                by object identity.

        Returns:
            The prompt string to send to the model.
        """
        use_structured = self.structured if structured is None else structured
        if reasoning_for is None:
            reasoning_for = self._retained_index_for(messages) or None
        history_text = _format_messages_for_summary(messages, reasoning_for)
        if use_structured:
            return _structured_summary_prompt(
                history_text,
                include_reasoning_note=bool(reasoning_for),
            )
        return self.summary_prompt_template.format(history=history_text)

    # ── Retained reasoning (IMPROVEMENTS.md E1) ─────────────────────────

    def set_retained_reasoning(
        self,
        path: list[Message],
        reasoning: dict[int, str] | list[str],
    ) -> None:
        """Register the current run's reasoning for retained-reasoning compaction.

        Args:
            path: The run's message path in order. Used for object-identity
                alignment (the engine's history and the tree's path share
                Message instances).
            reasoning: Either a dict mapping a *position in ``path``* to
                the captured reasoning text, or a list of reasoning
                strings aligned positionally with ``path`` (empty strings
                for messages without reasoning).
        """
        self._retained_by_id = {}
        if isinstance(reasoning, dict):
            for idx, text in reasoning.items():
                if text and 0 <= idx < len(path):
                    self._retained_by_id[id(path[idx])] = text
        else:
            for idx, text in enumerate(reasoning):
                if text and idx < len(path):
                    self._retained_by_id[id(path[idx])] = text

    def clear_retained_reasoning(self) -> None:
        """Drop the registered reasoning (called when a run ends)."""
        self._retained_by_id = {}

    def _retained_index_for(self, messages: list[Message]) -> dict[int, str]:
        """Resolve retained reasoning against *messages* by object identity."""
        if not self._retained_by_id:
            return {}
        pairs = {
            idx: text
            for idx, msg in enumerate(messages)
            if (text := self._retained_by_id.get(id(msg)))
        }
        return pairs

    def record_compaction_event(
        self,
        *,
        original_tokens: int,
        compacted_tokens: int,
        messages_removed: int,
        summary_length: int,
    ) -> None:
        """Register a manual compaction so telemetry sees it like auto ones."""
        self._pending_compact_events.append(
            SummarizationEvent(
                original_tokens=original_tokens,
                compacted_tokens=compacted_tokens,
                messages_removed=messages_removed,
                summary_length=summary_length,
            )
        )

    def build_compaction(
        self, messages: list[Message], summary_text: str
    ) -> tuple[list[Message], int, int]:
        """Assemble the compacted message list for a manual compaction.

        Same layout as the automatic path (system messages intact, a
        summary message, then the last ``min_keep_messages`` turns) but
        driven by an externally generated *summary_text* — the caller
        owns the LLM round trip (IMPROVEMENTS.md C2).

        Returns:
            ``(compacted_messages, before_tokens, after_tokens)``.
            When there is nothing to compact (at or under
            ``min_keep_messages`` non-system messages) the input list is
            returned unchanged with ``before == after``.
        """
        before = self._estimator.count_messages(messages)
        system_msgs = [m for m in messages if m.role == "system"]
        others = [m for m in messages if m.role != "system"]

        if len(others) <= self.min_keep_messages or not summary_text.strip():
            return messages, before, before

        keep = others[-self.min_keep_messages :]
        compacted = list(system_msgs)
        compacted.append(
            Message(
                role="user",
                content=(
                    f"[Conversation summary up to this point: {summary_text.strip()}]"
                ),
            )
        )
        compacted.extend(keep)
        after = self._estimator.count_messages(compacted)

        # Record the event so metrics/telemetry see manual compactions too.
        self._pending_compact_events.append(
            SummarizationEvent(
                original_tokens=before,
                compacted_tokens=after,
                messages_removed=len(messages) - len(compacted),
                summary_length=len(summary_text),
            )
        )
        return compacted, before, after
