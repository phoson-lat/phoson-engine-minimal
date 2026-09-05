"""OpenTelemetry tracing for Phoson (issue #140).

This plugin traces every agent run as an OpenTelemetry *trace* — one
``phoson.run`` span containing ``phoson.step`` children, each of which
contains a ``phoson.llm_call`` or ``phoson.tool_call`` span — and
exports it in **OTLP/HTTP JSON** shape (the
``ExportTraceServiceRequest`` body, ``application/json`` against the
OTLP ``/v1/traces`` endpoint) using only the standard library.

Two sinks, selected by ``sink`` config (``auto`` = OTLP when an
endpoint is configured, file otherwise):

* **``file``** (slice 1) — writes the finished trace to a local
  self-describing JSON document, so runs can be inspected, diffed and
  replayed into any OTLP tool with no collector or dependency.
* **``otlp``** (slice 2) — POSTs the *pure* ``ExportTraceServiceRequest``
  body to a real collector's ``/v1/traces`` (OTel collector, Jaeger,
  Honeycomb, LGTM, …), with ``headers`` support for auth/routing
  (merged over the standard ``OTEL_EXPORTER_OTLP_HEADERS`` env).

``opentelemetry-*`` packages are **not** required at runtime. The wire
format is pinned by a conformance test that decodes the plugin's exact
payload into the real ``opentelemetry-proto`` schema (test-only
dependency).

Configuration (``config:`` block of the plugin spec)::

    name = "phoson-plugin-otel"
    [plugins.config]
    sink = "auto"                    # auto (default) | file | otlp
    service_name = "phoson"          # or $PHOSON_SERVICE_NAME
    file_path = ".phoson/trace.json" # or $PHOSON_OTEL_TRACE_FILE
    otlp_endpoint = ""               # or $PHOSON_OTEL_ENDPOINT,
                                     #   $OTEL_EXPORTER_OTLP_ENDPOINT
    otlp_timeout_s = 5.0
    # [plugins.config.headers]
    # "Authorization" = "Bearer …"   # over OTEL_EXPORTER_OTLP_HEADERS

Every engine run (main engine **and** sub-agents) produces its own
trace; sub-agent traces export on completion exactly like the parent's.
Export is best-effort — a sink failure logs a warning and never
affects the run.
"""

from .span import (
    STATUS_OK,
    STATUS_ERROR,
    SPAN_KIND_CLIENT,
    SPAN_KIND_INTERNAL,
    OtelSpan,
    build_trace,
)
from ._plugin import PhosonOtelPlugin, create_plugin
from .tracing import OtelTracingMiddleware

__version__ = "0.1.0"

# Export the plugin instance. NOTE: the module file is named `_plugin.py`
# (not `plugin.py`) so this `plugin = ...` attribute does not shadow the
# submodule attribute — otherwise `import phoson_plugin_otel.plugin as m`
# would bind the instance instead of the module.
plugin = PhosonOtelPlugin()

__all__ = [
    "OtelSpan",
    "OtelTracingMiddleware",
    "PhosonOtelPlugin",
    "SPAN_KIND_CLIENT",
    "SPAN_KIND_INTERNAL",
    "STATUS_ERROR",
    "STATUS_OK",
    "build_trace",
    "create_plugin",
    "plugin",
]
