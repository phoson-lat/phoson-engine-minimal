"""Phoson OpenTelemetry plugin (issue #140).

Traces every agent run as an OTel trace — ``phoson.run`` →
``phoson.step`` → ``phoson.llm_call`` / ``phoson.tool_call`` — and
exports it via one of two sinks:

* **file** (slice 1) — a local trace-file in OTLP/HTTP JSON shape,
  inspectable and replayable into any OTLP tool with no collector.
* **otlp** (slice 2) — a real POST to a collector's ``/v1/traces``
  (pure ``ExportTraceServiceRequest`` body, ``headers`` for
  auth/routing, standard ``OTEL_EXPORTER_OTLP_HEADERS`` honored).

Design
------

* **One middleware, zero new engine hooks.** The engine already funnels
  every public event through ``on_agent_event``; the plugin builds the
  span tree from ``AgentStepDoneEvent`` (which carries the full
  ``RunStep``), so span attributes cannot drift from what ``/cost`` and
  ``/tokens`` report.
* **Stdlib only at runtime.** Span model + both sinks use no
  third-party dependency; ``opentelemetry-*`` packages are **not**
  required (they are a test-only conformance oracle).
* **Concurrency-safe.** The middleware instance is shared between the
  main engine and all sub-agent engines; active-run state lives in a
  ``ContextVar`` so parallel sub-agents never interleave traces.
* **Best-effort.** A sink I/O failure logs a warning and is swallowed —
  observability must never take a run down with it.

Configuration (``config:`` block of the plugin spec)
----------------------------------------------------

.. code-block:: toml

    [[plugins]]
    name = "phoson-plugin-otel"
    [plugins.config]
    # sink = "file"              # "file" | "otlp" | "auto" (default: auto)
    # service_name = "phoson"    # resource service.name ($PHOSON_SERVICE_NAME)
    # file_path = ".phoson/trace.json"  # ($PHOSON_OTEL_TRACE_FILE)
    # otlp_endpoint = ""         # ($PHOSON_OTEL_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT)
    # [plugins.config.headers]   # extra OTLP request headers (auth/routing)
    # "Authorization" = "Bearer …"
    # [plugins.config.resource_attributes]
    # "deployment.environment" = "dev"

``auto`` (default) picks the OTLP sink when an endpoint is configured
(via config or env) and the file sink otherwise. ``file_path`` may
contain a ``{trace_id}`` placeholder for one file per run. ``headers``
is merged over the standard ``OTEL_EXPORTER_OTLP_HEADERS`` env var
(``key=value,key2=value2``) so the exporter can carry an
``Authorization`` bearer or a vendor API key — the way you point it at
Honeycomb, LGTM or any hosted backend.
"""

import os
import logging
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from phoson_agent import Plugin
from phoson_agent.middleware import AgentMiddleware
from phoson_plugin_otel.sink import FileSink
from phoson_plugin_otel.span import build_resource
from phoson_plugin_otel.tracing import OtelTracingMiddleware, _RunState

_LOGGER = logging.getLogger(__name__)

_PLUGIN_NAME = "phoson-plugin-otel"

# OTLP/HTTP path for the traces signal.
_OTLP_TRACES_PATH = "/v1/traces"
_OTLP_CONTENT_TYPE = "application/json"

_DEFAULT_SERVICE_NAME = "phoson"
_DEFAULT_FILE_PATH = ".phoson/trace.json"
_DEFAULT_OTLP_TIMEOUT_S = 5.0

_SINKS = ("auto", "file", "otlp")


def _env_str(name: str) -> str:
    return os.environ.get(name, "").strip()


def _parse_otlp_headers(value: str) -> dict[str, str]:
    """Parse the standard ``OTEL_EXPORTER_OTLP_HEADERS`` format.

    ``key1=value1,key2=value2`` → ``{"key1": "value1", "key2": "value2"}``.
    Commas inside a value are *not* supported (matches the OTel spec:
    values must not contain `,`); a segment without ``=`` is skipped.
    """
    headers: dict[str, str] = {}
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, val = part.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            headers[key] = val.strip()
    return headers


class OtlpHttpSink:
    """Export a finished run's trace to an OTLP/HTTP collector.

    Slice 2: a production-shaped OTLP/HTTP (JSON) exporter.

    * Sends the **pure** ``ExportTraceServiceRequest`` body (no
      ``phoson_trace`` envelope) to ``<endpoint>/v1/traces`` — the exact
      payload a real collector (the OTel collector, Jaeger, Honeycomb,
      LGTM, …) expects. The run id is preserved as the ``phoson.run.id``
      attribute on the root span, so nothing is lost by dropping the
      envelope.
    * Supports **custom headers** (``headers`` config / env) for auth and
      routing — the way you point a single exporter at a hosted backend
      (``Authorization``, ``X-OTLP-API-Key``, a vendor trace path, …).
    * The request (URL + headers + body) is built **synchronously** so
      the wire format is deterministically testable; only the network
      send runs on a short-lived daemon thread so a slow or dead
      collector never blocks the agent. Failures are logged, never
      raised.
    """

    def __init__(
        self,
        endpoint: str,
        resource_attributes: dict[str, Any],
        timeout_s: float = _DEFAULT_OTLP_TIMEOUT_S,
        headers: dict[str, str] | None = None,
    ) -> None:
        endpoint = endpoint.rstrip("/")
        if not endpoint.lower().startswith(("http://", "https://")):
            endpoint = "http://" + endpoint
        self._url = endpoint + _OTLP_TRACES_PATH
        self._resource = dict(resource_attributes)
        self._timeout_s = timeout_s
        self._headers = {k: str(v) for k, v in (headers or {}).items()}

    def build_request(self, state: _RunState) -> tuple[str, dict[str, str], bytes]:
        """Return ``(url, headers, body)`` for one run — pure & testable.

        The body is the canonical OTLP/HTTP JSON
        ``ExportTraceServiceRequest`` (``build_trace``), so the same
        bytes a collector receives can be asserted in a test.
        """
        import json

        from phoson_plugin_otel.sink import build_trace

        body = json.dumps(
            build_trace(state.spans, self._resource), ensure_ascii=False
        ).encode("utf-8")
        headers = {
            "Content-Type": _OTLP_CONTENT_TYPE,
            "Content-Encoding": "identity",
            **self._headers,
        }
        return self._url, headers, body

    def write(self, state: _RunState) -> None:
        """POST the run's trace; logs (does not raise) on failure."""
        import threading

        url, headers, payload = self.build_request(state)
        request = Request(url, data=payload, headers=headers, method="POST")

        def _post() -> None:
            try:
                with urlopen(request, timeout=self._timeout_s) as response:
                    status = getattr(response, "status", None) or getattr(
                        response, "code", 0
                    )
                    if status >= 400:
                        _LOGGER.warning(
                            "otel: collector %s returned HTTP %s for run %s",
                            url,
                            status,
                            state.run_id,
                        )
            except HTTPError as exc:
                _LOGGER.warning(
                    "otel: collector %s returned HTTP %s for run %s: %s",
                    url,
                    exc.code,
                    state.run_id,
                    exc.reason,
                )
            except (URLError, OSError, ValueError) as exc:
                _LOGGER.warning(
                    "otel: failed to export run %s to %s: %s",
                    state.run_id,
                    url,
                    exc,
                )

        threading.Thread(target=_post, name="phoson-otel-export", daemon=True).start()

    def close(self) -> None:
        """No persistent resources; kept for sink-interface parity."""
        return None


class PhosonOtelPlugin(Plugin):
    """Official OTel tracing plugin (issue #140)."""

    def __init__(self) -> None:
        # ``None`` = "not set by config yet" → resolved from env/defaults
        # in ``_build_sink``. Merge semantics (see ``configure``) keep
        # explicit config values across reloads.
        self._cfg: dict[str, Any] = {
            "sink": "auto",
            "service_name": None,
            "file_path": None,
            "otlp_endpoint": None,
            "otlp_timeout_s": None,
            "resource_attributes": {},
            "headers": {},
        }
        self._sink: FileSink | OtlpHttpSink | None = None
        self._middleware: OtelTracingMiddleware | None = None
        self._resource: dict[str, Any] = {}
        self._export_count = 0

    # ── Plugin identity ─────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return _PLUGIN_NAME

    @property
    def version(self) -> str:
        from phoson_plugin_otel import __version__

        return __version__

    @property
    def description(self) -> str:
        return (
            "Traces every agent run as an OTel span tree (run → step → "
            "llm_call/tool_call) and exports it to a local JSON trace "
            "file (or an OTLP/HTTP collector endpoint)."
        )

    # ── Lifecycle ───────────────────────────────────────────────────────

    def configure(self, config: dict[str, Any]) -> None:
        """Merge user config; keys absent keep their current value.

        The loader calls ``configure`` again (with an empty dict) when
        the same instance is reloaded after an engine rebuild, so this
        must never reset state (same contract as the monitor plugin).

        Config keys (all optional; env fallbacks applied at sink build
        time, documented in the module docstring): ``sink``,
        ``service_name``, ``file_path``, ``otlp_endpoint``,
        ``otlp_timeout_s``, ``resource_attributes``.
        """
        for key, value in (config or {}).items():
            if key not in self._cfg:
                _LOGGER.warning("otel: ignoring unknown config key %r", key)
                continue
            if key == "resource_attributes":
                if not isinstance(value, dict):
                    _LOGGER.warning(
                        "otel: 'resource_attributes' must be a mapping; ignoring"
                    )
                    continue
                self._cfg[key] = dict(value)
            else:
                self._cfg[key] = value
        # (Re)build the sink/middleware — idempotent and cheap.
        self._build_sink()

    def _build_sink(self) -> None:
        """Resolve sink + resource from merged config and env."""
        cfg = self._cfg

        service_name = str(
            cfg["service_name"]
            or _env_str("PHOSON_SERVICE_NAME")
            or _DEFAULT_SERVICE_NAME
        )
        self._resource = build_resource(service_name, dict(cfg["resource_attributes"]))

        file_path = str(
            cfg["file_path"] or _env_str("PHOSON_OTEL_TRACE_FILE") or _DEFAULT_FILE_PATH
        )
        otlp_endpoint = str(
            cfg["otlp_endpoint"]
            or _env_str("PHOSON_OTEL_ENDPOINT")
            or _env_str("OTEL_EXPORTER_OTLP_ENDPOINT")
            or ""
        )
        timeout_raw = cfg["otlp_timeout_s"]
        timeout_s = (
            float(timeout_raw) if timeout_raw is not None else _DEFAULT_OTLP_TIMEOUT_S
        )
        if timeout_s <= 0:
            _LOGGER.warning(
                "otel: otlp_timeout_s must be > 0; using %.1f", _DEFAULT_OTLP_TIMEOUT_S
            )
            timeout_s = _DEFAULT_OTLP_TIMEOUT_S

        # Headers: env (standard OTel format) first, then the config
        # mapping (which wins on key collisions) — e.g. an
        # ``Authorization`` bearer or a vendor API key.
        headers = _parse_otlp_headers(_env_str("OTEL_EXPORTER_OTLP_HEADERS"))
        cfg_headers = cfg["headers"]
        if not isinstance(cfg_headers, dict):
            _LOGGER.warning("otel: 'headers' must be a mapping; ignoring")
            cfg_headers = {}
        headers.update({str(k): str(v) for k, v in cfg_headers.items()})

        sink = str(cfg["sink"]).lower()
        if sink not in _SINKS:
            _LOGGER.warning(
                "otel: unknown sink %r (expected one of %s); falling back to 'auto'",
                sink,
                ", ".join(_SINKS),
            )
            sink = "auto"

        use_otlp = (sink == "otlp") or (sink == "auto" and bool(otlp_endpoint))
        if use_otlp and not otlp_endpoint:
            # Explicit otlp but no endpoint: nothing to talk to.
            _LOGGER.warning(
                "otel: sink 'otlp' requested but no endpoint configured "
                "(set 'otlp_endpoint' or PHOSON_OTEL_ENDPOINT); "
                "falling back to the file sink"
            )
            use_otlp = False

        if use_otlp:
            self._sink = OtlpHttpSink(
                otlp_endpoint,
                self._resource,
                timeout_s=timeout_s,
                headers=headers,
            )
            _LOGGER.info("otel: tracing enabled → OTLP %s", self._url_of(self._sink))
        else:
            self._sink = FileSink(file_path, self._resource)
            _LOGGER.info("otel: tracing enabled → file %s", file_path)

        self._middleware = OtelTracingMiddleware(self._export)

    def initialize(self) -> None:
        # No resources to open; sinks are per-write. Kept for clarity.
        return None

    @staticmethod
    def _url_of(sink: OtlpHttpSink | FileSink | None) -> str:
        if isinstance(sink, OtlpHttpSink):
            return sink._url  # noqa: SLF001 — same-package diagnostic
        return str(getattr(sink, "path", "?"))

    def cleanup(self) -> None:
        # Best-effort flush of a run still in flight when the host shuts
        # down: the shared middleware (the same instance reaches sub-agent
        # engines) may hold a parent run that never saw Done/Error. Its
        # contextvar is read here in the *calling* task's context — the
        # parent run's context, which is where the state lives.
        middleware = self._middleware
        if middleware is not None:
            try:
                state = middleware._current.get()  # noqa: SLF001 — same package
            except Exception:  # noqa: BLE001
                state = None
            if state is not None and not state.exported:
                state.run_span.end()
                state.exported = True
                self._export(state)
        if self._sink is not None:
            try:
                self._sink.close()
            except Exception:  # noqa: BLE001 — cleanup must not raise
                _LOGGER.warning("otel: sink close failed", exc_info=True)

    # ── Hooks ───────────────────────────────────────────────────────────

    def get_middlewares(self) -> list[AgentMiddleware]:
        if self._middleware is None:
            # Defensive: a direct ``PhosonOtelPlugin()`` (API use) may not
            # have been ``configure``d yet — build the default sink so
            # tracing works out of the box.
            self._build_sink()
        assert self._middleware is not None
        return [self._middleware]

    def _export(self, state: _RunState) -> None:
        """Ship a finished run's trace to the configured sink (best-effort)."""
        sink = self._sink
        if sink is None or not state.spans:
            return
        try:
            if isinstance(sink, FileSink):
                written = sink.write(state.spans, run_info={"run_id": state.run_id})
                _LOGGER.info("otel: wrote trace %s → %s", state.trace_id, written)
            else:
                sink.write(state)
            self._export_count += 1
        except Exception:  # noqa: BLE001 — observability must never break a run
            _LOGGER.warning(
                "otel: failed to export run %s (trace %s)",
                state.run_id,
                state.trace_id,
                exc_info=True,
            )

    # ── Host-facing diagnostics (duck-typed, like find_monitor_plugin) ──

    @property
    def export_count(self) -> int:
        """Number of traces exported so far (for tests / `/details`)."""
        return self._export_count

    @property
    def sink_path(self) -> str:
        """Human-readable sink target (file path or OTLP URL)."""
        return self._url_of(self._sink)


def create_plugin() -> PhosonOtelPlugin:
    """Factory for the ``path:`` loader and entry-point form."""
    return PhosonOtelPlugin()
