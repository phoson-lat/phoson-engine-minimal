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
    "openai": "o200k_base",      # GPT-4o+ uses o200k natively
    "openrouter": "cl100k_base", # Mixed providers; cl100k is the safe default
    "ollama": "cl100k_base",     # Most Ollama models are Llama-based (~10-15% off)
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


def _format_messages_for_summary(messages: list[Message]) -> str:
    """Format messages as readable text for the summary LLM call."""
    parts: list[str] = []
    for msg in messages:
        role = msg.role.upper()
        if isinstance(msg.content, str):
            parts.append(f"[{role}] {msg.content}")
            continue

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
        parts.append(f"[{role}] {' '.join(text_parts)}")
    return "\n\n".join(parts)


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
    summary_prompt_template: str = SUMMARY_PROMPT_TEMPLATE

    # Internal state. Both are constructed in ``__post_init__``; using
    # ``init=False`` keeps them out of the dataclass constructor and out of
    # ``repr()``. They are non-Optional after ``__post_init__`` runs.
    _resolver: ContextWindowResolver = field(init=False, repr=False)
    _estimator: TokenEstimator = field(init=False, repr=False)
    _pending_compact_events: list[SummarizationEvent] = field(
        default_factory=list, repr=False
    )

    def __post_init__(self) -> None:
        """Initializes the resolver and estimator."""
        self._resolver = ContextWindowResolver(
            ollama_base_url=self.ollama_base_url,
            openrouter_api_key=self.openrouter_api_key,
        )
        self._estimator = TokenEstimator.for_provider(self.provider)

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

        # Generate summary
        history_text = _format_messages_for_summary(to_summarize)
        summary_prompt = self.summary_prompt_template.format(history=history_text)

        summary_messages = [Message(role="user", content=summary_prompt)]
        summary_config = ModelConfig(model=self.model, max_tokens=2048, temperature=0.3)

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
