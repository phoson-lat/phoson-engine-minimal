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

from phoson_llm.utils import (
    CONTEXT_LENGTH_ERROR_CODE,
    extract_context_window,
)
from phoson_llm.schemas import (
    Message,
    LLMEvent,
    TextBlock,
    AudioBlock,
    ErrorEvent,
    ImageBlock,
    TokenEvent,
    UsageEvent,
    VideoBlock,
    ModelConfig,
    ToolUseBlock,
    DocumentBlock,
    ToolCallEvent,
    ToolDefinition,
    ToolResultBlock,
    ReasoningTokenEvent,
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

# Conservative flat estimates for multimodal blocks (I-91). tiktoken cannot
# score media payloads, and a 4-token per-message overhead wildly
# underestimates them: OpenAI's high-detail image tiles to ~1056-1700
# tokens, Anthropic's PDF is ~20 tokens/page. These constants are
# deliberately on the high side — underestimating is what makes the
# auto-compact gate fire too late.
_IMAGE_TOKENS = 1_700  # OpenAI high-detail ceiling
_IMAGE_LOW_DETAIL_TOKENS = 1_056
_AUDIO_TOKENS = 2_000  # ~30s of audio at GPT-4o-mini rates
_VIDEO_TOKENS = 8_000  # sampled frames; provider-dependent
_DOCUMENT_TOKENS_PER_PAGE = 20  # Anthropic PDF pages
_DOCUMENT_TOKENS_DEFAULT = 1_000  # page count unknown
#: Fraction of the context window reserved on top of ``max_tokens`` to
#: absorb tokenizer approximation error (±10-15% on non-OpenAI models).
_ESTIMATION_SAFETY_FRACTION = 0.10


def estimate_block_tokens(encoding, block) -> int:
    """Token estimate for one content block (text is exact, media flat)."""
    if isinstance(block, TextBlock):
        return len(encoding.encode(block.text))
    if isinstance(block, ToolUseBlock):
        return len(encoding.encode(json.dumps(block.args)))
    if isinstance(block, ToolResultBlock):
        return len(encoding.encode(block.result))
    if isinstance(block, ImageBlock):
        return _IMAGE_LOW_DETAIL_TOKENS if block.detail == "low" else _IMAGE_TOKENS
    if isinstance(block, AudioBlock):
        return _AUDIO_TOKENS
    if isinstance(block, VideoBlock):
        return _VIDEO_TOKENS
    if isinstance(block, DocumentBlock):
        pages = block.pages if isinstance(block.pages, int) and block.pages > 0 else 0
        return pages * _DOCUMENT_TOKENS_PER_PAGE + _DOCUMENT_TOKENS_DEFAULT
    # Unknown block type — count its repr so nothing is silently free.
    return len(encoding.encode(repr(block)))


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
        Multimodal blocks (image/audio/video/document) carry a
        conservative flat estimate each (I-91) — they were previously
        skipped, which undercounted vision/document sessions.
        """
        total = 0
        for msg in messages:
            total += _MSG_OVERHEAD
            if isinstance(msg.content, str):
                total += self.count_text(msg.content)
                continue

            for block in msg.content:
                total += estimate_block_tokens(self._encoding, block)
        return total

    def count_tools(self, tools: list[ToolDefinition] | None) -> int:
        """Estimate the token weight of the tool schemas sent with the call.

        The provider serializes the full JSON schema of every tool on
        *every* request; the auto-compact gate must count them (I-91).
        """
        if not tools:
            return 0
        return self.count_text(
            json.dumps(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        },
                    }
                    for t in tools
                ],
                ensure_ascii=True,
            )
        )

    def count_system(self, system: str | None) -> int:
        """Estimate the token weight of the system prompt (I-91).

        The system prompt travels in ``ModelConfig.system`` (or as a
        leading system message); it is part of every request and the
        auto-compact gate must count it.
        """
        if not system:
            return 0
        return self.count_text(system)

    def estimate_request(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        """Conservative estimate of the *full request* token count (I-91).

        Messages + system prompt + tool schemas — i.e. everything the
        provider will charge input tokens for. This is the number the
        auto-compact gate and the header indicator use.
        """
        return (
            self.count_messages(messages)
            + self.count_system(system)
            + self.count_tools(tools)
        )

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
    #: Tool schemas sent with every LLM call (I-91). The controller
    #: mirrors the engine's tool registry here so the auto-compact gate
    #: counts the schema weight in its token estimate. ``None`` means
    #: "no tools" — the estimate then skips the schema term.
    tool_definitions: list[ToolDefinition] | None = None

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

    def estimate_request(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        """Conservative estimate of a full request (I-91).

        Messages + system prompt + tool schemas — the same number the
        auto-compact gate uses, so the header indicator and ``/status``
        agree with the gate. When *tools* is None the middleware's
        :attr:`tool_definitions` (mirrored by the controller) is used.
        """
        if tools is None:
            tools = self.tool_definitions
        return self._estimator.estimate_request(messages, system=system, tools=tools)

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

        I-91 changes over the original gate:

        - The estimate is *conservative*: it counts the system prompt
          and the tool schemas in addition to the messages, and the
          trigger threshold reserves ``config.max_tokens`` plus a safety
          fraction of the window, so the gate fires before the provider
          would reject the request.
        - If the provider still rejects the request with a
          context-length error (HTTP 400) *before any user-visible
          output*, the middleware performs an **emergency compaction**
          and retries the call once. A second context-length error
          propagates — no retry loops.
        - Compaction splices the compacted list *in place* into
          ``messages`` so the engine's history (the same list object)
          stays compact for the rest of the run instead of re-compacting
          every iteration.
        """
        if self.auto_enabled and await self._should_compact(messages, config):
            (
                compacted,
                before_tokens,
                after_tokens,
                summary_text,
                fwd,
            ) = await self._compact(call_next, messages, config)
            for event in fwd:
                yield event
            if compacted is not None:
                self._record_compaction(
                    messages,
                    compacted,
                    before_tokens=before_tokens,
                    after_tokens=after_tokens,
                    summary_length=len(summary_text),
                )
                # Splice in place: the engine's history list is the same
                # object, so every subsequent iteration sees the
                # compacted history (no re-compaction per iteration).
                messages[:] = compacted

        # Run the call and, if the provider rejects it for context
        # length before any visible output, rescue with an emergency
        # compaction and retry once.
        async for event in self._call_with_context_rescue(call_next, messages, config):
            yield event

    # ── Gate (I-91) ───────────────────────────────────────────────────

    def _request_tokens(self, messages: list[Message], config: ModelConfig) -> int:
        """Conservative token estimate of the full request (I-91).

        Messages + system prompt (``config.system`` or a leading system
        message) + tool schemas.
        """
        system = config.system
        if not system:
            for msg in messages:
                if msg.role == "system" and isinstance(msg.content, str):
                    system = msg.content
                    break
        return self._estimator.estimate_request(
            messages, system=system, tools=self.tool_definitions
        )

    def _trigger_tokens(self, context_window: int, config: ModelConfig) -> int:
        """Token count at which the gate fires (I-91).

        The configured threshold, but never so high that the request
        plus the reserved output plus a safety margin would exceed the
        window — the provider rejects the request once *input + output*
        exceeds the context window, so a gate that only watches the
        input fraction fires too late.

        The output reservation is capped at half the window: a
        ``max_tokens`` larger than the window (the default 32768 on a
        small local model) cannot be reserved in full, and the
        provider would cap the output at ``window - input`` anyway.
        """
        output_reserve = min(config.max_tokens, max(1, context_window // 2))
        safety = int(context_window * _ESTIMATION_SAFETY_FRACTION)
        return max(
            0,
            min(
                int(context_window * self.threshold),
                context_window - output_reserve - safety,
            ),
        )

    async def _should_compact(
        self, messages: list[Message], config: ModelConfig
    ) -> bool:
        """Whether the auto-compact gate fires for this request (I-91)."""
        context_window = await self._resolver.resolve(self.provider, self.model)
        current_tokens = self._request_tokens(messages, config)
        return current_tokens > self._trigger_tokens(context_window, config)

    # ── Compaction core ───────────────────────────────────────────────

    async def _compact(
        self,
        call_next: LLMCallNext,
        messages: list[Message],
        config: ModelConfig,
    ) -> tuple[list[Message] | None, int, int, str, list[LLMEvent]]:
        """Summarize the old middle of *messages* and build the compacted list.

        Returns ``(compacted, before_tokens, after_tokens, summary_text,
        forward_events)``. ``compacted`` is None when there is nothing to
        summarize (the tail already holds everything) or the summary call
        failed. ``forward_events`` carries the summary call's
        ``UsageEvent`` so its cost is not silently dropped.
        """
        before = self._request_tokens(messages, config)
        system_msgs: list[Message] = []
        others: list[Message] = []
        for msg in messages:
            if msg.role == "system":
                system_msgs.append(msg)
            else:
                others.append(msg)

        keep = others[-self.min_keep_messages :]
        to_summarize = others[: len(others) - len(keep)]

        if not to_summarize:
            return None, before, before, "", []

        # Generate summary — same prompt builder as the manual /compact
        # path so auto and manual compactions produce identical artifacts.
        summary_prompt = self.build_summary_prompt(to_summarize)

        summary_messages = [Message(role="user", content=summary_prompt)]
        # 4096 (not 2048) because the structured handoff document is longer
        # than a free-form paragraph; matches the manual /compact path.
        summary_config = ModelConfig(
            model=config.model or self.model, max_tokens=4096, temperature=0.3
        )

        summary_text, summary_failed, forward = await self._run_summary_call(
            call_next(summary_messages, summary_config)
        )
        if summary_failed:
            # The summary round trip itself failed — compaction cannot
            # proceed; the caller passes the original messages through.
            return None, before, before, "", forward

        compacted = list(system_msgs)
        if summary_text.strip():
            summary_content = (
                f"[Conversation summary up to this point: {summary_text.strip()}]"
            )
            compacted.append(Message(role="user", content=summary_content))
        compacted.extend(keep)
        after = self._request_tokens(compacted, config)
        return compacted, before, after, summary_text, forward

    async def _run_summary_call(
        self, events: AsyncIterator[LLMEvent]
    ) -> tuple[str, bool, list[LLMEvent]]:
        """Consume the internal summary stream.

        Returns ``(summary_text, failed, forward_events)``. Visual
        events of the summary turn are swallowed (they belong to the
        internal call, not the user's turn) but the ``UsageEvent`` is
        kept so the caller can forward it — the cost of the
        summarization must reach the session metrics. A failure of the
        summary call is reported via ``failed`` so the caller can fall
        back to the un-compacted messages instead of losing the turn.
        """
        summary_text = ""
        failed = False
        forward: list[LLMEvent] = []
        async for event in events:
            if isinstance(event, TokenEvent):
                summary_text += event.content
            elif isinstance(event, ErrorEvent):
                failed = True
            elif isinstance(event, UsageEvent):
                forward.append(event)
        return summary_text, failed, forward

    def _record_compaction(
        self,
        messages: list[Message],
        compacted: list[Message],
        *,
        before_tokens: int,
        after_tokens: int,
        summary_length: int,
    ) -> None:
        """Queue a :class:`SummarizationEvent` for the front end."""
        self._pending_compact_events.append(
            SummarizationEvent(
                original_tokens=before_tokens,
                compacted_tokens=after_tokens,
                messages_removed=len(messages) - len(compacted),
                summary_length=summary_length,
            )
        )

    # ── Emergency rescue on context-length 400 (I-91) ─────────────────

    async def _call_with_context_rescue(
        self,
        call_next: LLMCallNext,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[LLMEvent]:
        """Run the LLM call; rescue a context-length 400 with compaction.

        If the stream yields a context-length :class:`ErrorEvent`
        *before any user-visible output* (token/reasoning/tool call),
        the middleware:

        1. learns the real context window from the error message when
           the provider states it (calibrates future gates),
        2. performs an emergency compaction (or, when the summary call
           fails or there is nothing to summarize, a hard truncation
           that keeps the recent tail plus a notice),
        3. retries the call **once**. A second context-length error —
           or any error after visible output — propagates.
        """
        committed = False
        rescued = False
        context_error: ErrorEvent | None = None

        async for event in call_next(messages, config):
            if isinstance(event, ErrorEvent):
                if (
                    not committed
                    and not rescued
                    and event.code == CONTEXT_LENGTH_ERROR_CODE
                ):
                    context_error = event
                    break
                yield event
                return
            if isinstance(event, (TokenEvent, ReasoningTokenEvent, ToolCallEvent)):
                committed = True
            yield event

        if context_error is None:
            return

        # 1) Learn the window the provider just told us about.
        window = extract_context_window(context_error.message)
        if window is not None:
            try:
                self._resolver.override(self.provider, self.model, window)
            except AttributeError:  # non-resolver stand-ins in tests
                pass

        # 2) Emergency compaction — the prompt itself must fit the
        #    window, so cut the front of the history until it does.
        compacted, rescue_summary_length = await self._emergency_compact(
            call_next, messages, config
        )
        if compacted is None:
            # Nothing left to cut (only the recent tail remains) —
            # compaction cannot make the request smaller; propagate.
            yield context_error
            return

        self._record_compaction(
            messages,
            compacted,
            before_tokens=self._request_tokens(messages, config),
            after_tokens=self._request_tokens(compacted, config),
            summary_length=rescue_summary_length,
        )
        messages[:] = compacted

        # 3) Retry once.
        rescued = True
        async for event in call_next(messages, config):
            if isinstance(event, ErrorEvent):
                if not committed and event.code == CONTEXT_LENGTH_ERROR_CODE:
                    # Still too long after compaction — give up.
                    yield event
                    return
            if isinstance(event, (TokenEvent, ReasoningTokenEvent, ToolCallEvent)):
                committed = True
            yield event

    async def _emergency_compact(
        self,
        call_next: LLMCallNext,
        messages: list[Message],
        config: ModelConfig,
    ) -> tuple[list[Message] | None, int]:
        """Build an emergency-compacted list guaranteed to fit the window.

        Strategy: keep the system prefix and the recent tail, then cut
        the oldest non-system messages until the *summary prompt itself*
        fits — the summary call must not hit the same 400. If the
        summary call fails (or there is nothing to summarize), fall back
        to a hard truncation: recent tail + a notice naming the dropped
        messages, which is always smaller than the original request.

        Returns:
            ``(compacted, summary_length)`` — the compacted list (or
            None when the request cannot be made smaller, i.e. only the
            recent tail remains) and the length of the generated summary
            (0 for the hard-truncation fallback).
        """
        context_window = await self._resolver.resolve(self.provider, self.model)
        budget = self._trigger_tokens(context_window, config)

        system_msgs: list[Message] = []
        others: list[Message] = []
        for msg in messages:
            if msg.role == "system":
                system_msgs.append(msg)
            else:
                others.append(msg)

        keep = others[-self.min_keep_messages :]
        to_summarize = others[: len(others) - len(keep)]

        if not to_summarize:
            return None, 0

        # Cut the front until the summary prompt fits the budget.
        max_prompt_tokens = max(
            1,
            budget - self._estimator.count_system(config.system) - _MSG_OVERHEAD,
        )
        chunk = to_summarize
        while chunk:
            if (
                self._estimator.count_messages(
                    [Message(role="user", content=self.build_summary_prompt(chunk))]
                )
                <= max_prompt_tokens
            ):
                break
            chunk = chunk[1:]

        summary_text = ""
        if chunk:
            summary_messages = [
                Message(role="user", content=self.build_summary_prompt(chunk))
            ]
            summary_config = ModelConfig(
                model=config.model or self.model, max_tokens=4096, temperature=0.3
            )
            summary_text, failed, _fwd = await self._run_summary_call(
                call_next(summary_messages, summary_config)
            )
        else:
            failed = True

        if not failed and summary_text.strip():
            compacted = list(system_msgs)
            compacted.append(
                Message(
                    role="user",
                    content=(
                        f"[Emergency compaction — conversation summary up to "
                        f"this point: {summary_text.strip()}]"
                    ),
                )
            )
            compacted.extend(keep)
            return compacted, len(summary_text)

        # Hard truncation: the summary call failed (or nothing fit) —
        # keep the recent tail and a notice. Always smaller than the
        # original request, which is all the rescue can promise.
        dropped = len(to_summarize)
        compacted = list(system_msgs)
        compacted.append(
            Message(
                role="user",
                content=(
                    f"[Emergency compaction — {dropped} older message(s) were "
                    "dropped to fit the model's context window; the summary "
                    "call failed.]"
                ),
            )
        )
        compacted.extend(keep)
        return compacted, 0

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
