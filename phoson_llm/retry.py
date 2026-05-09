"""Retry / backoff wrapper for LLM chat adapters.

A small, dependency-free retry helper that wraps any
:class:`phoson_llm.chats.base.BaseLLMChat` and re-attempts the request
when the provider signals a transient failure (rate limits, 5xx, network
errors). The wrapper is **streaming-aware**: it only retries while no
tokens have been emitted yet. Once the stream produces the first
``TokenEvent`` (or any user-visible event), it commits to that response
and forwards the rest as-is — partial output cannot be safely
"undone".

Usage:

    from phoson_llm.chats.openrouter import OpenRouterChat
    from phoson_llm.retry import with_retry

    chat = with_retry(OpenRouterChat(), max_attempts=3)

The wrapper preserves the contract of :class:`BaseLLMChat` so it is
fully transparent to the rest of the engine.
"""

import random
import asyncio
from dataclasses import field, dataclass
from collections.abc import Callable, AsyncIterator

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    TokenEvent,
    ModelConfig,
    ToolCallEvent,
    ToolDefinition,
    ReasoningTokenEvent,
)
from phoson_llm.chats.base import BaseLLMChat
from phoson_llm.exceptions import PhosonProviderError

# Events that mean tokens are flowing to the user; once any of these has
# been yielded, retrying a failed call would produce duplicate output.
_USER_VISIBLE_EVENTS = (
    TokenEvent,
    ReasoningTokenEvent,
    ToolCallEvent,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Configuration for the retry wrapper.

    Attributes:
        max_attempts: Maximum number of attempts including the first one.
            ``max_attempts=1`` disables retries entirely.
        initial_delay: Delay before the second attempt, in seconds.
        max_delay: Hard ceiling on any computed delay.
        multiplier: Geometric backoff factor (``delay *= multiplier`` per
            attempt).
        jitter: Fraction of the delay added as uniform random noise to
            avoid thundering-herd retries when many clients fail at once.
            ``0.25`` means up to ±25% of the base delay.
        on_retry: Optional callback invoked before each retry sleep.
            Receives ``(attempt, error)`` where ``attempt`` is 1-indexed
            (1 = first retry) and ``error`` is the :class:`ErrorEvent` that
            triggered it. Use this for observability without coupling to a
            logging framework::

                def log_retry(attempt: int, error: ErrorEvent) -> None:
                    print(f"retry {attempt}: {error.message}")

                policy = RetryPolicy(on_retry=log_retry)
    """

    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0
    jitter: float = 0.25
    on_retry: Callable[[int, "ErrorEvent"], None] | None = field(
        default=None, compare=False, hash=False
    )

    def compute_delay(self, attempt: int) -> float:
        """Return the delay (seconds) before attempt ``attempt`` (1-indexed).

        ``attempt == 1`` is the first retry (i.e. after the initial
        attempt failed). The base delay grows geometrically and is
        clamped at ``max_delay`` before jitter is applied.
        """
        if attempt < 1:
            return 0.0
        base = self.initial_delay * (self.multiplier ** (attempt - 1))
        base = min(base, self.max_delay)
        if self.jitter > 0:
            spread = base * self.jitter
            return max(0.0, base + random.uniform(-spread, spread))
        return base


class RetryingChat(BaseLLMChat):
    """A :class:`BaseLLMChat` that retries transient failures.

    Wraps another chat instance. The wrapped chat must follow the
    standard event ordering described in :class:`BaseLLMChat`. Retries
    only happen while the stream has not yet produced user-visible
    output: once a :class:`TokenEvent`, :class:`ReasoningTokenEvent` or
    :class:`ToolCallEvent` has been forwarded, an error is propagated
    as a final ``ErrorEvent`` without further attempts.

    Args:
        inner: The chat adapter to wrap.
        policy: Retry configuration. Defaults to 3 attempts with
            exponential backoff starting at 1s and capped at 30s.
    """

    def __init__(
        self,
        inner: BaseLLMChat,
        policy: RetryPolicy | None = None,
    ) -> None:
        self._inner = inner
        self._policy = policy or RetryPolicy()

    async def stream(
        self,
        messages: list[Message],
        config: ModelConfig,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        attempt = 0
        last_error: ErrorEvent | None = None

        while attempt < self._policy.max_attempts:
            attempt += 1
            committed = False
            saw_error = False

            try:
                async for event in self._inner.stream(messages, config, tools):
                    if isinstance(event, ErrorEvent):
                        saw_error = True
                        last_error = event
                        if committed or not event.retryable:
                            yield event
                            return
                        # Hold on to the error; retry below.
                        break

                    if isinstance(event, _USER_VISIBLE_EVENTS):
                        committed = True

                    yield event
            except PhosonProviderError as exc:
                if committed or not exc.retryable:
                    raise
                last_error = ErrorEvent(
                    message=str(exc),
                    code=exc.code,
                    retryable=True,
                )
                saw_error = True

            if not saw_error:
                # Stream completed normally.
                return

            if attempt >= self._policy.max_attempts:
                break

            if self._policy.on_retry is not None and last_error is not None:
                try:
                    self._policy.on_retry(attempt, last_error)
                except Exception:  # noqa: BLE001
                    pass  # callback errors must not abort the retry loop

            # Skip the LLMStartEvent that the inner stream already emitted
            # for the failed attempt by waiting for the new stream to emit
            # its own. ``LLMStartEvent`` is informational so duplicate
            # starts on retry are visible but harmless.
            await asyncio.sleep(self._policy.compute_delay(attempt))

        if last_error is not None:
            yield last_error


def with_retry(
    chat: BaseLLMChat,
    *,
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    multiplier: float = 2.0,
    jitter: float = 0.25,
    on_retry: Callable[[int, ErrorEvent], None] | None = None,
) -> RetryingChat:
    """Wrap ``chat`` in a :class:`RetryingChat` with a one-shot policy.

    Convenience helper for the common case where callers just want a
    sensible retry policy without constructing the dataclass manually.

    Example:

        chat = with_retry(
            OpenRouterChat(),
            max_attempts=5,
            initial_delay=0.5,
            on_retry=lambda n, e: print(f"retry {n}: {e.message}"),
        )
    """
    return RetryingChat(
        chat,
        RetryPolicy(
            max_attempts=max_attempts,
            initial_delay=initial_delay,
            max_delay=max_delay,
            multiplier=multiplier,
            jitter=jitter,
            on_retry=on_retry,
        ),
    )
