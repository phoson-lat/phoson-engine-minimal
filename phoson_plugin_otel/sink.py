"""Trace sinks (issue #140, slice 1: local JSON file sink).

A *sink* consumes the finished :class:`~phoson_plugin_otel.span.OtelSpan`
tree of one agent run and persists it somewhere. Slice 1 ships:

* :class:`FileSink` — writes a single self-describing JSON document
  (the OTLP/HTTP JSON ``ExportTraceServiceRequest`` body plus a
  small ``phoson`` envelope with run metadata) to a local file,
  pretty-printed. This is the default when no OTLP endpoint is
  configured.

Slice 2 will add :class:`OtlpHttpSink` (POST to the collector's
``/v1/traces``), reusing the exact ``build_trace()`` body.

Sinks are best-effort: :meth:`close` never raises, so a broken disk or
an unreadable directory can never take the agent run down with it.
"""

import os
import json
import logging
import tempfile
from typing import Any
from pathlib import Path

from phoson_plugin_otel.span import OtelSpan, build_trace

_LOGGER = logging.getLogger(__name__)

#: Envelope key marking the document as a Phoson OTel trace dump.
PHOSON_TRACE_KEY = "phoson_trace"


def trace_envelope(
    spans: list[OtelSpan],
    resource_attributes: dict[str, str],
    run_info: dict[str, Any] | None = None,
    scope_version: str = "",
) -> dict[str, Any]:
    """Build the file-sink document.

    The document is the OTLP/HTTP JSON body *plus* a ``phoson_trace``
    envelope (``schema_version``, run info, ``trace_id``) so that
    ``jq .phoson_trace`` answers "which run is this?" while
    ``jq '.resourceSpans' | otlpreplay`` still works unchanged.
    """
    otlp_body = build_trace(spans, resource_attributes, scope_version=scope_version)
    trace_id = spans[0].trace_id if spans else ""
    envelope: dict[str, Any] = {
        PHOSON_TRACE_KEY: {
            "schema_version": 1,
            "trace_id": trace_id,
            "span_count": len(spans),
            "export_format": "otlp_http_json",
        }
    }
    if run_info:
        envelope[PHOSON_TRACE_KEY]["run"] = run_info
    return {**envelope, **otlp_body}


class FileSink:
    """Write each finished run's trace as one JSON file.

    Args:
        path: Destination path (string or :class:`~pathlib.Path`).
            Relative paths are resolved against the CWD *at construction
            time* so the plugin does not surprise the user if the CWD
            changes mid-run. ``{trace_id}`` in the filename is replaced
            with the run's trace id; without it the fixed path is
            overwritten by each run (one file per process lifetime is
            the common local case).
        resource_attributes: Resource map stamped on every document.
        pretty: Pretty-print (default ``True``).
    """

    def __init__(
        self,
        path: str | Path,
        resource_attributes: dict[str, str],
        pretty: bool = True,
    ) -> None:
        self._path_template = str(Path(path))
        self._resource = dict(resource_attributes)
        self._pretty = pretty
        # Expand {trace_id} placeholders lazily — the trace id only
        # exists once a run has started.
        self._last_trace_id = ""
        self._closed = False

    @property
    def path(self) -> Path:
        """The path that will be (or was) written for the current run."""
        if "{trace_id}" in self._path_template:
            return Path(self._path_template.format(trace_id=self._last_trace_id))
        return Path(self._path_template)

    def set_trace_id(self, trace_id: str) -> None:
        """Remember the active trace id (called when a run starts)."""
        self._last_trace_id = trace_id

    def write(
        self,
        spans: list[OtelSpan],
        run_info: dict[str, Any] | None = None,
        scope_version: str = "",
    ) -> Path:
        """Serialize and atomically write the trace for one run.

        Returns the written path. Raises on I/O failure — the plugin
        wraps this in its best-effort guard.
        """
        if not spans:
            raise ValueError("FileSink.write called with no spans")
        self._last_trace_id = spans[0].trace_id
        document = trace_envelope(
            spans, self._resource, run_info=run_info, scope_version=scope_version
        )
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            document, indent=2 if self._pretty else None, ensure_ascii=False
        )
        # Atomic write: temp file in the same directory, then replace —
        # a reader never observes a half-written trace.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return target

    def close(self) -> None:
        """No persistent resources; idempotent and non-raising."""
        self._closed = True
