"""OpenTelemetry tracing for Phoson (issue #140, slice 1).

This plugin traces every agent run as an OpenTelemetry *trace* — one
``phoson.run`` span containing ``phoson.step`` children, each of which
contains a ``phoson.llm_call`` or ``phoson.tool_call`` span — and
exports it in **OTLP/HTTP JSON** shape (the
``ExportTraceServiceRequest`` body, ``application/json`` against the
OTLP ``/v1/traces`` endpoint) using only the standard library.

Slice 1 of #140 ships the **local trace-file sink**: with no collector
configured (or with ``sink: "file"``), the finished trace is written as
a self-describing JSON document to disk, so runs can be inspected,
diffed and later replayed into any OTLP-compatible tool (Jaeger, the
OTel collector, ``otlpreplay``) without any extra dependency. The
minimal OTLP/HTTP POST sink is included for slice 2's real-collector
path; ``opentelemetry-*`` packages are **not** required by this plugin.

Configuration (``config:`` block of the plugin spec)::

    name = "phoson-plugin-otel"
    [plugins.config]
    sink = "auto"                    # auto (default) | file | otlp
    service_name = "phoson"          # or $PHOSON_SERVICE_NAME
    file_path = ".phoson/trace.json" # or $PHOSON_OTEL_TRACE_FILE; {trace_id}
    otlp_endpoint = ""               # or $PHOSON_OTEL_ENDPOINT /
                                     #   $OTEL_EXPORTER_OTLP_ENDPOINT
    otlp_timeout_s = 5.0

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
