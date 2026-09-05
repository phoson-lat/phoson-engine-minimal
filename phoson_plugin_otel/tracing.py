"""Span-tree construction from the agent event stream (issue #140).

:class:`OtelTracingMiddleware` listens to the engine's public agent
events (``on_agent_event``, the single funnel every event passes
through via ``_prepare_event``) and, per run, builds the OTel span
tree required by issue #140::

    phoson.run                    (one per engine.run() / sub-agent run)
    └── phoson.step               (one per RunStep, ``phoson.step.index``)
        ├── phoson.llm_call       (for kind="llm": model, tokens, cost)
        └── phoson.tool_call      (for kind="tool": name, call id, outcome)

``AgentStepDoneEvent`` is the single source of truth: each one carries
the fully populated :class:`~phoson_agent.models.RunStep`
(kind, model, tool_name, tool_call_id, usage, cost, timing, error,
payload), so the span attributes for a step and its child are derived
from *the same data the CLI reports in ``/cost`` / ``/tokens``* — they
cannot drift.

Concurrency model
-----------------

The same middleware instance is shared by the main engine **and** every
sub-agent engine (the CLI hands the same middleware list to each
sub-engine, #174/F-01), and parallel sub-agents run in separate tasks.
The active run state therefore lives in a :class:`contextvars.ContextVar`
(never plain instance state): each task sees the run started in its own
task context, so a sub-agent starting a run never clobbers the
in-flight parent run, and two parallel sub-agents cannot interleave.

Every hook is best-effort: any internal failure is logged and
swallowed — observability must never take a run down with it.
"""

import uuid
import logging
import contextvars
from typing import Any
from dataclasses import field, dataclass

from phoson_agent.models import (
    RunStep,
    AgentDoneEvent,
    AgentErrorEvent,
    AgentStartEvent,
    AgentStepDoneEvent,
)
from phoson_agent.middleware import AgentMiddleware
from phoson_plugin_otel.span import (
    STATUS_OK,
    STATUS_ERROR,
    SPAN_KIND_CLIENT,
    SPAN_KIND_INTERNAL,
    OtelSpan,
    to_ns,
    new_trace_id,
    trace_id_from_env,
)

_LOGGER = logging.getLogger(__name__)

#: Cap for string attribute values (tool args, error messages) so a
#: single 1 MB tool arg cannot blow up the trace file.
_MAX_ATTR_STR = 500
#: Cap for the serialized tool-args attribute.
_MAX_ARGS_JSON = 4000

# Tool-step ``RunStep.error`` markers the engine sets for non-handler
# outcomes (see ``phoson_agent._tool_runner``); anything else is a
# handler/runtime error.
_BLOCKED = "blocked_by_middleware"
_REFUSED = "permission_denied"
_UNUSABLE_ARGS = "unusable_args"


def _clip(value: str, limit: int = _MAX_ATTR_STR) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _tool_outcome(error: str | None) -> str:
    """Map a tool step's ``error`` to a canonical outcome label."""
    if error is None:
        return "ok"
    if error == _BLOCKED:
        return "denied_by_middleware"
    if error == _REFUSED:
        return "denied_by_permission"
    if error == _UNUSABLE_ARGS:
        return "unusable_args"
    return "error"


def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    """Clip long string values in tool args for the span attribute."""
    clean: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > _MAX_ATTR_STR:
            clean[key] = _clip(value)
        elif isinstance(value, list):
            clean[key] = [
                _clip(v) if isinstance(v, str) and len(v) > _MAX_ATTR_STR else v
                for v in value
            ]
        else:
            clean[key] = value
    return clean


@dataclass
class _RunState:
    """Mutable per-run trace state (one per engine.run() invocation)."""

    run_id: str
    trace_id: str
    run_span: OtelSpan
    spans: list[OtelSpan] = field(default_factory=list)
    step_count: int = 0
    exported: bool = False

    def add_step(self, step_span: OtelSpan, child: OtelSpan | None) -> None:
        """Append a step span (and its llm/tool child) to the trace."""
        self.spans.append(step_span)
        if child is not None:
            self.spans.append(child)


def _start_run_state(trace_id: str) -> _RunState:
    run_id = uuid.uuid4().hex
    run_span = OtelSpan(
        name="phoson.run",
        trace_id=trace_id,
        kind=SPAN_KIND_INTERNAL,
    )
    run_span.set_attribute("phoson.run.id", run_id)
    # Parents before children: the run span is the root of the trace and
    # is listed first from the very start, so the span list is always in
    # topological order (even a cleanup-time flush exports a valid tree).
    return _RunState(
        run_id=run_id, trace_id=trace_id, run_span=run_span, spans=[run_span]
    )


class OtelTracingMiddleware(AgentMiddleware):
    """Build an OTel span tree per run from the public agent events.

    The middleware is *stateless across runs* by design: the active run
    lives in the ``_current`` contextvar, and :meth:`_export` finalizes +
    ships the trace exactly once per run (idempotent via
    ``_RunState.exported``).
    """

    def __init__(self, exporter) -> None:
        """Args:
        exporter: Callable taking ``(_RunState)``; called once per
            run at termination (best-effort; exceptions logged).
            Typically a bound :meth:`PhosonOtelPlugin._export`.
        """
        self._exporter = exporter

        # Active run for *this task context*. Sub-agent tasks copy the
        # parent context and then set their own on AgentStartEvent, so
        # concurrent runs are isolated without any locking.
        self._current: contextvars.ContextVar[_RunState | None] = (
            contextvars.ContextVar("phoson_otel_current_run", default=None)
        )
        self.last_run_id: str | None = None
        self.last_trace_id: str | None = None

    # ── Hooks ───────────────────────────────────────────────────────────

    async def on_agent_event(self, event) -> None:
        try:
            if isinstance(event, AgentStartEvent):
                self._on_start(event)
            elif isinstance(event, AgentStepDoneEvent):
                self._on_step(event.step)
            elif isinstance(event, AgentDoneEvent):
                self._on_done(event)
            elif isinstance(event, AgentErrorEvent):
                self._on_error(event)
        except Exception:  # noqa: BLE001 — observability must never break a run
            _LOGGER.warning(
                "otel tracing: failed to process %s",
                type(event).__name__,
                exc_info=True,
            )

    # ── Event handlers ──────────────────────────────────────────────────

    def _on_start(self, event: AgentStartEvent) -> None:
        # A caller (CI runner, upstream system) may pin the trace id for
        # cross-system correlation; otherwise a fresh W3C trace id.
        trace_id = trace_id_from_env() or new_trace_id()
        state = _start_run_state(trace_id)
        state.run_span.set_attribute("phoson.model", event.model)
        state.run_span.set_attribute("phoson.message_count", event.message_count)
        state.run_span.set_attribute("phoson.max_iterations", event.max_iterations)
        self._current.set(state)

    def _on_step(self, step: RunStep) -> None:
        state = self._current.get()
        if state is None:
            return  # no run open in this context (should not happen)
        index = state.step_count
        state.step_count += 1

        step_span = OtelSpan(
            name="phoson.step",
            trace_id=state.trace_id,
            parent_id=state.run_span.span_id,
            start_time=to_ns(step.started_at),
            end_time=to_ns(step.ended_at),
        )
        step_span.set_attributes(
            {
                "phoson.step.index": index,
                "phoson.step.kind": step.kind,
                "phoson.step.duration_ms": step.duration_ms,
            }
        )

        child: OtelSpan | None
        if step.kind == "llm":
            child = self._llm_child(state, step, step_span)
        else:
            child = self._tool_child(state, step, step_span)

        status = STATUS_OK
        if step.error:
            status = STATUS_ERROR
            step_span.set_attribute("phoson.step.error", _clip(str(step.error)))
        step_span.set_status(status)
        if child is not None:
            child.set_status(status)

        state.add_step(step_span, child)

    def _llm_child(
        self,
        state: _RunState,
        step: RunStep,
        step_span: OtelSpan,
    ) -> OtelSpan:
        span = OtelSpan(
            name="phoson.llm_call",
            trace_id=state.trace_id,
            kind=SPAN_KIND_CLIENT,
            parent_id=step_span.span_id,
            start_time=to_ns(step.started_at),
            end_time=to_ns(step.ended_at),
        )
        attrs: dict[str, Any] = {
            "gen_ai.request.model": step.model or "",
            "phoson.cost_usd": step.cost_usd,
            "phoson.credits": step.credits,
        }
        usage = step.usage
        if usage is not None:
            attrs.update(
                {
                    "gen_ai.usage.input_tokens": usage.input,
                    "gen_ai.usage.output_tokens": usage.output,
                    "gen_ai.usage.cache_read_tokens": usage.cache_read,
                    "gen_ai.usage.cache_write_tokens": usage.cache_write,
                }
            )
        span.set_attributes(attrs)
        if step.error:
            span.set_status(STATUS_ERROR, _clip(str(step.error)))
        else:
            span.set_status(STATUS_OK)
        return span

    def _tool_child(
        self,
        state: _RunState,
        step: RunStep,
        step_span: OtelSpan,
    ) -> OtelSpan:
        span = OtelSpan(
            name="phoson.tool_call",
            trace_id=state.trace_id,
            parent_id=step_span.span_id,
            start_time=to_ns(step.started_at),
            end_time=to_ns(step.ended_at),
        )
        outcome = _tool_outcome(step.error)
        attrs: dict[str, Any] = {
            "phoson.tool.name": step.tool_name or "",
            "phoson.tool.outcome": outcome,
            "phoson.tool.duration_ms": step.duration_ms,
        }
        if step.tool_call_id:
            attrs["phoson.tool.call_id"] = step.tool_call_id
        args = step.payload.get("args")
        if isinstance(args, dict):
            attrs["phoson.tool.args"] = _sanitize_args(args)
        result = step.payload.get("result")
        if isinstance(result, str):
            attrs["phoson.tool.result_chars"] = len(result)
        span.set_attributes(attrs)
        if outcome == "ok":
            span.set_status(STATUS_OK)
        else:
            span.set_status(STATUS_ERROR, _clip(str(step.error or outcome)))
        return span

    def _on_done(self, event: AgentDoneEvent) -> None:
        state = self._current.get()
        if state is None:
            return
        result = event.result
        # step_count comes from the middleware's own tally (== len(result.steps)
        # for a real engine run, but the tally stays correct even if the
        # result's step list is a subset/summary).
        state.run_span.set_attributes(
            {
                "phoson.step_count": state.step_count,
                "phoson.total_cost_usd": result.total_cost_usd,
                "phoson.total_credits": result.total_credits,
                "phoson.truncated": result.truncated,
                "phoson.final_content_chars": len(result.final_content or ""),
            }
        )
        state.run_span.set_status(STATUS_OK)
        self._finalize(state)

    def _on_error(self, event: AgentErrorEvent) -> None:
        state = self._current.get()
        if state is None:
            return
        state.run_span.set_attribute("phoson.error.code", event.code or "unknown")
        state.run_span.set_attribute("phoson.error.message", _clip(event.message))
        state.run_span.set_status(STATUS_ERROR, _clip(event.message))
        self._finalize(state)

    # ── Finalization ────────────────────────────────────────────────────

    def _finalize(self, state: _RunState) -> None:
        """End the run span, record ids, and export exactly once."""
        state.run_span.end()
        self.last_run_id = state.run_id
        self.last_trace_id = state.trace_id
        if state.exported:
            return
        state.exported = True
        try:
            self._exporter(state)
        except Exception:  # noqa: BLE001 — best-effort export
            _LOGGER.warning(
                "otel tracing: export failed for run %s",
                state.run_id,
                exc_info=True,
            )
