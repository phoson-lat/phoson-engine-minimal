"""
Module for agent middlewares.
"""

import json
import time
import hashlib
import warnings
from abc import ABC
from dataclasses import dataclass
from collections.abc import Callable, AsyncIterator

from phoson_llm.schemas import (
    Message,
    LLMEvent,
    ErrorEvent,
    ModelConfig,
    ToolCallEvent,
)
from phoson_agent.models import AgentEvent, AgentStartEvent
from phoson_agent.exceptions import DoomLoopDetectedError

LLMCallNext = Callable[
    [list[Message], ModelConfig],
    AsyncIterator[LLMEvent],
]


class AgentMiddleware(ABC):
    """
    Base class for agent engine middlewares.
    """

    async def on_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        """Hook executed before calling the LLM."""
        return messages

    async def wrap_llm_call(
        self,
        call_next: LLMCallNext,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[LLMEvent]:
        """Wraps the LLM call to intercept events."""
        async for event in call_next(messages, config):
            yield event

    async def on_before_tool(
        self,
        call: ToolCallEvent,
    ) -> ToolCallEvent | None:
        """Hook executed before executing a tool."""
        return call

    async def on_after_tool(
        self,
        call: ToolCallEvent,
        result: str,
        error: bool,
    ) -> str:
        """Hook executed after executing a tool."""
        return result

    async def on_agent_event(self, event: AgentEvent) -> None:
        """Hook executed on any agent event."""
        return None


class RetryMiddleware(AgentMiddleware):
    """Middleware to automatically retry LLM calls on errors.

    .. deprecated::
        Use :class:`phoson_llm.retry.RetryingChat` instead. It has the
        correct streaming semantics (it only retries *before* any token has
        been emitted, so a committed stream is never re-run and output is
        never duplicated) and it is the layer the CLI actually wires up via
        :func:`phoson_cli.config.build_chat`. This middleware marks a call
        as "visible" on the very first ``LLMStartEvent`` every adapter
        emits, so a retryable error that arrives after the start is
        re-sent instead of retried — it effectively never retries. It is
        kept (and only emits a deprecation warning on construction) so
        existing code that imports it keeps working; do not add it to a
        new middleware chain.
    """

    def __init__(
        self,
        max_retries: int = 2,
        base_delay_seconds: float = 0.5,
        backoff_multiplier: float = 2.0,
    ) -> None:
        warnings.warn(
            "RetryMiddleware is deprecated and does not reliably retry "
            "(a stream is marked visible on LLMStartEvent, which every "
            "adapter emits first). Wrap your chat in "
            "phoson_llm.retry.RetryingChat instead — the CLI does this "
            "automatically via build_chat (config.llm_max_attempts).",
            DeprecationWarning,
            stacklevel=2,
        )
        self.max_retries = max_retries
        self.base_delay_seconds = base_delay_seconds
        self.backoff_multiplier = backoff_multiplier

    async def wrap_llm_call(
        self,
        call_next: LLMCallNext,
        messages: list[Message],
        config: ModelConfig,
    ) -> AsyncIterator[LLMEvent]:
        import asyncio

        attempt = 0

        while True:
            visible_event_seen = False
            retryable_error: ErrorEvent | None = None

            async for event in call_next(messages, config):
                if (
                    isinstance(event, ErrorEvent)
                    and event.retryable
                    and not visible_event_seen
                ):
                    retryable_error = event
                    break

                if not isinstance(event, (ErrorEvent,)):
                    visible_event_seen = True

                yield event

            if retryable_error is None:
                return

            attempt += 1
            if attempt > self.max_retries:
                yield retryable_error
                return

            delay = self.base_delay_seconds * (self.backoff_multiplier ** (attempt - 1))
            await asyncio.sleep(delay)


# ── Doom loop detection (#142) ─────────────────────────────────────────────


@dataclass
class _LoopEntry:
    """Per-fingerprint doom-loop state: consecutive error count."""

    error_count: int = 0


class DoomLoopMiddleware(AgentMiddleware):
    """Detects a doom loop: the same tool call failing ``n`` times in a row.

    A "same tool call" is one with an identical
    :meth:`_fingerprint` — tool name plus normalized args (sorted keys,
    stripped string values, whitespace-collapsed). Only **errors** count
    toward the loop: a successful result resets that fingerprint's
    counter, so legitimate "retry until it works" sequences that
    eventually succeed never trip the detector.

    Modes:

    - ``"inject"`` (default): when the ``n``-th consecutive error of a
      fingerprint lands, the *result* of that call is annotated with a
      ``[⚠ doom-loop]`` observation so the model is told to change
      approach. The run continues.
    - ``"abort"``: once the threshold is reached, the **next** call with
      the same fingerprint is refused — :meth:`on_before_tool` raises
      :class:`~phoson_agent.exceptions.DoomLoopDetectedError`. Inside
      the engine the tool runner converts that into an actionable error
      result (the run continues, the model sees why); a direct caller of
      the hook can catch it to terminate the run.

    State is per-run: :meth:`on_agent_event` resets everything on
    :class:`~phoson_agent.models.AgentStartEvent`.

    ``n <= 0`` disables the middleware entirely (every hook is a
    no-op) — this is how ``PHOSON_LOOP_DETECT_N=0`` turns detection off.
    """

    def __init__(self, n: int = 3, mode: str = "inject") -> None:
        if mode not in ("inject", "abort"):
            raise ValueError(
                f"DoomLoopMiddleware mode must be 'inject' or 'abort', got {mode!r}"
            )
        self._n = n
        self._mode = mode
        self._disabled = n <= 0
        self._counts: dict[str, _LoopEntry] = {}
        self._triggered: set[str] = set()

    # ── hooks ─────────────────────────────────────────────────────────

    async def on_before_tool(self, call: ToolCallEvent) -> ToolCallEvent | None:
        if self._disabled:
            return call
        if self._mode == "abort" and self._fingerprint(call) in self._triggered:
            raise DoomLoopDetectedError(
                f"Doom loop: {self._n} consecutive errors for {call.tool_name}; "
                "refusing to repeat the identical call. "
                "Change the approach (different args or a different tool).",
                tool_name=call.tool_name,
            )
        return call

    async def on_after_tool(
        self,
        call: ToolCallEvent,
        result: str,
        error: bool,
    ) -> str:
        if self._disabled:
            return result

        fingerprint = self._fingerprint(call)
        if not error:
            # A success breaks the loop for this fingerprint.
            self._counts.pop(fingerprint, None)
            return result

        entry = self._counts.get(fingerprint)
        if entry is None:
            entry = _LoopEntry()
            self._counts[fingerprint] = entry
        entry.error_count += 1

        if entry.error_count >= self._n and fingerprint not in self._triggered:
            self._triggered.add(fingerprint)
            if self._mode == "inject":
                result = (
                    result + f"\n\n[⚠ doom-loop] {call.tool_name} has now failed "
                    f"{entry.error_count} times in a row with the same "
                    "arguments. Repeating it will not work — change the "
                    "approach (different arguments, a different tool, or "
                    "fix the underlying problem first)."
                )
        return result

    async def on_agent_event(self, event: AgentEvent) -> None:
        if isinstance(event, AgentStartEvent):
            self._counts.clear()
            self._triggered.clear()

    # ── fingerprinting ────────────────────────────────────────────────

    def _fingerprint(self, call: ToolCallEvent) -> str:
        """Stable hash of (tool name, normalized args)."""
        normalized = self._normalize_args(call.args)
        payload = f"{call.tool_name}:{normalized}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _normalize_args(args: dict) -> str:
        """Canonical form of a tool call's args.

        Two calls are "identical" when this returns the same string:
        keys are sorted, string values are stripped and whitespace is
        collapsed (so ``"pytest -q"`` ≡ ``"pytest  -q "``), and non-string
        scalars are stringified. Dict/list values are serialized with
        sorted keys so key order cannot hide a difference.
        """
        normalized: dict[str, str] = {}
        for key in sorted(args):
            value = args[key]
            if isinstance(value, str):
                normalized[key] = " ".join(value.split())
            elif isinstance(value, (dict, list)):
                normalized[key] = json.dumps(value, sort_keys=True, default=str)
            else:
                normalized[key] = str(value)
        return json.dumps(normalized, sort_keys=True)


# ── Environmental context (#143) ─────────────────────────────────────────


class EnvironmentalContextMiddleware(AgentMiddleware):
    """Appends a one-line environmental context block before each LLM call.

    The block reports the iteration position (``step N/M``) and, when a
    wall-clock run budget is configured, how much time has elapsed and
    how much remains:

        ``[env: step 12/20, time 45s elapsed, 555s remaining]``

    **Design constraint — always at the END.** The block is appended as
    the *last* message of the context, never prepended: the stable
    prefix (system prompt + history) must stay byte-identical across
    turns so the provider's prompt cache is not invalidated. Only the
    numeric content of the trailing line changes between turns.

    The block is injected on **every** LLM call (including the first),
    so the agent can always see its remaining budget.

    **Role: ``user`` with a plain string.** A trailing ``role="system"``
    message would be silently dropped by the adapters (Anthropic skips
    every system message in ``_convert_messages``; the OpenAI-compatible
    adapter empties a system message whose content is a block list), so
    the context would never reach the model. A ``user`` message with a
    plain string is handled by the fast path in every adapter, and
    consecutive ``user`` messages already occur in this codebase (multi-
    tool results), so it is accepted by the providers. The ``[env:``
    prefix keeps it visually distinct from a real user turn.

    State is per-run: :meth:`on_agent_event` resets the step counter and
    restarts the clock on :class:`~phoson_agent.models.AgentStartEvent`.
    """

    def __init__(
        self,
        *,
        max_iterations: int = 20,
        run_budget_seconds: float | None = None,
    ) -> None:
        if max_iterations <= 0:
            raise ValueError(f"max_iterations must be > 0, got {max_iterations}")
        self._max_iterations = max_iterations
        self._run_budget = run_budget_seconds
        self._step = 0
        self._start_time: float | None = None

    def reset(self) -> None:
        """Restart the step counter and the run clock (new run)."""
        self._step = 0
        self._start_time = time.monotonic()

    async def on_agent_event(self, event: AgentEvent) -> None:
        if isinstance(event, AgentStartEvent):
            self.reset()

    async def on_before_llm(
        self,
        messages: list[Message],
        config: ModelConfig,
    ) -> list[Message]:
        if self._start_time is None:
            self._start_time = time.monotonic()
        self._step += 1

        parts = [f"step {self._step}/{self._max_iterations}"]
        if self._run_budget and self._run_budget > 0:
            elapsed = time.monotonic() - self._start_time
            remaining = max(0.0, self._run_budget - elapsed)
            parts.append(f"time {int(elapsed)}s elapsed, {int(remaining)}s remaining")

        env_text = f"[env: {', '.join(parts)}]"
        updated = list(messages)
        updated.append(Message(role="user", content=env_text))
        return updated
