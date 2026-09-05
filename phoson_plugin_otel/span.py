"""OTel-shaped span model + trace builder (stdlib only).

This module is the slice-1 heart of the ``phoson_plugin_otel`` plugin:
an :class:`OtelSpan` that mirrors the OTLP/HTTP JSON wire shape for a
single span, and :func:`build_trace` which serializes a set of spans
into the ``ExportTraceServiceRequest`` body that the OTLP ``/v1/traces``
HTTP endpoint expects (``application/json``).

Keeping this model hand-rolled (instead of a hard dependency on
``opentelemetry-exporter-otlp-proto-http``) means:

* the plugin runs with the standard library only,
* the file-sink output is inspectable without any OTel tooling,
* the same :func:`build_trace` output can be replayed into any
  OTLP-compatible consumer (Jaeger, the OTel collector, ``otlpreplay``)
  once slice 2 adds the real HTTP exporter.

IDs follow W3C Trace Context: 128-bit ``trace_id`` / 64-bit
``span_id`` rendered as lowercase hex. Timestamps are epoch
nanoseconds (``int``); the OTLP/JSON proto3 mapping serializes 64-bit
integers as *strings*, so ``to_otlp_json`` stringifies the time fields.
"""

import os
import time
import secrets
from typing import Any
from datetime import datetime
from dataclasses import field, dataclass

# ── OTLP Span.SpanKind (numeric) ───────────────────────────────────────
SPAN_KIND_INTERNAL = 1
SPAN_KIND_SERVER = 2
SPAN_KIND_CLIENT = 3
SPAN_KIND_PRODUCER = 4
SPAN_KIND_CONSUMER = 5

# ── OTLP Status codes (numeric) ────────────────────────────────────────
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

# W3C: 16 bytes for trace id, 8 bytes for span id.
_TRACE_ID_BYTES = 16
_SPAN_ID_BYTES = 8


def new_trace_id() -> str:
    """Generate a fresh 32-hex-char W3C trace id."""
    return secrets.token_hex(_TRACE_ID_BYTES)


def new_span_id() -> str:
    """Generate a fresh 16-hex-char W3C span id."""
    return secrets.token_hex(_SPAN_ID_BYTES)


def to_ns(dt: datetime) -> int:
    """Render a ``datetime`` (aware or naive) as epoch nanoseconds.

    Naive datetimes are assumed to be in the system local timezone — the
    same assumption the engine makes when it serializes timestamps.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1_000_000_000)


def attr_value(value: Any) -> dict[str, Any]:
    """Map a Python value to its OTLP ``AnyValue`` JSON shape.

    * ``str`` → ``{"stringValue": ...}``
    * ``bool`` → ``{"boolValue": ...}`` (checked before ``int``)
    * ``int`` → ``{"intValue": "<str>"}`` (proto3 int64 over JSON)
    * ``float`` → ``{"doubleValue": ...}``
    * ``list``/``tuple`` → ``{"arrayValue": {"values": [...]}}``
    * ``dict`` → ``{"kvlistValue": {"values": [{key, value}...]}}``
    * ``None``/other → ``{}`` (a valid but empty value)
    """
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [attr_value(v) for v in value]}}
    if isinstance(value, dict):
        return {
            "kvlistValue": {
                "values": [
                    {"key": str(k), "value": attr_value(v)} for k, v in value.items()
                ]
            }
        }
    return {}


def attrs_to_otlp(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    """Render an attribute map as the OTLP ``KeyValue`` list (sorted)."""
    return [
        {"key": str(k), "value": attr_value(v)} for k, v in sorted(attributes.items())
    ]


@dataclass
class OtelSpan:
    """A single span, shaped for the OTLP/JSON ``ResourceSpans`` tree.

    Attributes:
        name: Span name, e.g. ``phoson.run``, ``phoson.step``,
            ``phoson.llm_call`` or ``phoson.tool_call.bash``.
        trace_id: 32-hex-char W3C trace id.
        span_id: 16-hex-char W3C span id (auto-generated).
        kind: OTLP ``SpanKind`` numeric value (default ``INTERNAL``).
        start_time: Epoch nanoseconds (defaults to ``now``).
        end_time: Epoch nanoseconds (clamped ``>= start_time``).
        attributes: OTLP ``KeyValue`` map.
        events: OTLP ``SpanEvent`` list (``{name, attributes}``).
        status: OTLP ``Status`` numeric value (default ``UNSET``).
        parent_id: Parent span id, or ``""`` for the root span.
    """

    name: str
    trace_id: str
    span_id: str = field(default_factory=new_span_id)
    kind: int = SPAN_KIND_INTERNAL
    start_time: int = 0
    end_time: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: int = STATUS_UNSET
    parent_id: str = ""

    def __post_init__(self) -> None:
        if self.start_time == 0:
            self.start_time = time.time_ns()
        if self.end_time < self.start_time:
            self.end_time = self.start_time

    @property
    def parent(self) -> str | None:
        return self.parent_id or None

    def set_attribute(self, key: str, value: Any) -> None:
        """Set (or overwrite) a span attribute."""
        self.attributes[key] = value

    def set_attributes(self, mapping: dict[str, Any]) -> None:
        """Bulk-assign attributes (existing keys are overwritten)."""
        self.attributes.update(mapping)

    def set_status(self, status: int, message: str = "") -> None:
        """Set the span status. For ``STATUS_ERROR`` the ``message`` is
        also stored under ``otel.status_description`` (mirroring the SDK's
        status-description handling)."""
        self.status = status
        if message and status == STATUS_ERROR:
            self.attributes["otel.status_description"] = message

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        """Append an OTLP ``SpanEvent`` entry."""
        self.events.append(
            {"name": name, "attributes": attrs_to_otlp(attributes or {})}
        )

    def end(self, end_time: int | None = None) -> None:
        """Mark the span finished. ``end_time`` (epoch ns) defaults to
        ``now`` and is clamped to ``>= start_time``."""
        if end_time is None:
            end_time = time.time_ns()
        self.end_time = max(int(end_time), self.start_time)

    def to_otlp_json(self) -> dict[str, Any]:
        """Render this span as the OTLP/JSON ``Span`` object."""
        if self.end_time < self.start_time:
            self.end_time = self.start_time
        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(self.start_time),
            "endTimeUnixNano": str(self.end_time),
            "attributes": attrs_to_otlp(self.attributes),
            "events": self.events,
            "status": {"code": self.status},
        }
        if self.parent_id:
            span["parentSpanId"] = self.parent_id
        return span


def build_trace(
    spans: list[OtelSpan],
    resource_attributes: dict[str, Any] | None = None,
    scope_name: str = "phoson_plugin_otel",
    scope_version: str = "",
) -> dict[str, Any]:
    """Build an ``ExportTraceServiceRequest`` body (OTLP/HTTP JSON).

    The result is shaped like::

        {
          "resourceSpans": [
            {
              "resource": {"attributes": [...]},
              "scopeSpans": [
                {"scope": {...}, "spans": [ ...OTLP Span objects... ]}
              ]
            }
          ]
        }

    ``spans`` are emitted in the order given — callers should append
    parents before children so the file reads topologically.
    """
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": attrs_to_otlp(dict(resource_attributes or {}))
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": scope_name,
                            **({"version": scope_version} if scope_version else {}),
                        },
                        "spans": [s.to_otlp_json() for s in spans],
                    }
                ],
            }
        ]
    }


def build_resource(
    service_name: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the default resource attribute map.

    Always includes ``service.name`` (plus the minimal telemetry SDK
    fields); ``extra`` overrides/extends it.
    """
    resource: dict[str, Any] = {
        "service.name": service_name,
        "telemetry.sdk.name": "phoson",
        "telemetry.sdk.language": "python",
    }
    if extra:
        resource.update(extra)
    return resource


def trace_id_from_env(env: str = "PHOSON_OTEL_TRACE_ID") -> str:
    """Return an externally-provided W3C trace id, or ``""``.

    Useful for correlating a Phoson run with an upstream system that
    already allocated a trace id (e.g. a CI runner). The value must be
    exactly 32 lowercase hex chars; anything else is rejected (returns
    ``""``) so a malformed env var can never produce an invalid trace.
    """
    value = os.environ.get(env, "").strip()
    if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
        return value
    return ""
