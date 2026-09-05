"""Unit tests for FileSink + the plugin lifecycle/config (issue #140).

Covers: the trace-file document (OTLP body + phoson envelope), atomic
writes, the ``{trace_id}`` placeholder, plugin config resolution
(sink auto/file/otlp, env fallbacks, merge semantics), the OTLP HTTP
sink against a local ``http.server`` collector, and ``cleanup()``
flushing an in-flight run.
"""

import json
import time
import asyncio
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytest

from phoson_plugin_otel import PhosonOtelPlugin
from phoson_agent.models import AgentDoneEvent, AgentRunResult, AgentStartEvent
from phoson_plugin_otel.sink import PHOSON_TRACE_KEY, FileSink, trace_envelope
from phoson_plugin_otel.span import OtelSpan, build_trace, new_trace_id


def _spans(n: int = 2) -> list[OtelSpan]:
    trace_id = new_trace_id()
    root = OtelSpan(name="phoson.run", trace_id=trace_id, start_time=1, end_time=10)
    rest = [
        OtelSpan(
            name="phoson.step",
            trace_id=trace_id,
            parent_id=root.span_id,
            start_time=2,
            end_time=9,
        )
        for _ in range(n)
    ]
    return [root, *rest]


def _run_start_done(plugin: PhosonOtelPlugin) -> None:
    """Drive one minimal run (Start → Done) through the plugin middleware."""

    async def _go() -> None:
        assert plugin._middleware is not None  # noqa: SLF001
        await plugin._middleware.on_agent_event(AgentStartEvent(model="m"))
        await plugin._middleware.on_agent_event(
            AgentDoneEvent(result=AgentRunResult("x", [], []))
        )

    asyncio.run(_go())


# ── Envelope / file document ───────────────────────────────────────────────────


class TestTraceEnvelope:
    def test_shape(self) -> None:
        spans = _spans()
        doc = trace_envelope(
            spans, {"service.name": "phoson"}, run_info={"run_id": "r1"}
        )
        assert doc[PHOSON_TRACE_KEY]["trace_id"] == spans[0].trace_id
        assert doc[PHOSON_TRACE_KEY]["span_count"] == len(spans)
        assert doc[PHOSON_TRACE_KEY]["run"]["run_id"] == "r1"
        # The OTLP body is present and complete.
        assert build_trace(spans, {"service.name": "phoson"}) == {
            k: v for k, v in doc.items() if k != PHOSON_TRACE_KEY
        }


# ── FileSink ───────────────────────────────────────────────────────────────────


class TestFileSink:
    def test_write_creates_dirs_and_file(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "dir" / "trace.json"
        sink = FileSink(target, {"service.name": "phoson"})
        written = sink.write(_spans(), run_info={"run_id": "r1"})
        assert written == target
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc[PHOSON_TRACE_KEY]["run"]["run_id"] == "r1"

    def test_atomic_write_no_temp_left(self, tmp_path: Path) -> None:
        target = tmp_path / "trace.json"
        FileSink(target, {}).write(_spans())
        leftovers = [p for p in tmp_path.iterdir() if p.name != "trace.json"]
        assert leftovers == []

    def test_trace_id_placeholder(self, tmp_path: Path) -> None:
        target = tmp_path / "runs" / "{trace_id}.json"
        sink = FileSink(target, {})
        spans = _spans()
        sink.write(spans)
        written = tmp_path / "runs" / f"{spans[0].trace_id}.json"
        assert written.exists()
        assert sink.path == written

    def test_fixed_path_overwrites(self, tmp_path: Path) -> None:
        target = tmp_path / "trace.json"
        sink = FileSink(target, {})
        a = _spans()
        sink.write(a)
        b = _spans()
        sink.write(b)
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc[PHOSON_TRACE_KEY]["trace_id"] == b[0].trace_id

    def test_empty_spans_raises(self, tmp_path: Path) -> None:
        sink = FileSink(tmp_path / "t.json", {})
        with pytest.raises(ValueError):
            sink.write([])

    def test_close_is_noop(self, tmp_path: Path) -> None:
        sink = FileSink(tmp_path / "t.json", {})
        sink.close()
        sink.close()


# ── Plugin identity & lifecycle ────────────────────────────────────────────────


class TestPluginIdentity:
    def test_name(self) -> None:
        assert PhosonOtelPlugin().name == "phoson-plugin-otel"

    def test_version(self) -> None:
        from phoson_plugin_otel import __version__

        assert PhosonOtelPlugin().version == __version__

    def test_description_nonempty(self) -> None:
        assert PhosonOtelPlugin().description

    def test_get_middlewares_builds_default_sink(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        assert len(plugin.get_middlewares()) == 1

    def test_get_middlewares_stable_instance(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        assert plugin.get_middlewares()[0] is plugin.get_middlewares()[0]


# ── Config resolution ──────────────────────────────────────────────────────────


class TestConfigure:
    def test_default_is_file_sink(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("PHOSON_OTEL_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        plugin.configure({})
        assert plugin.sink_path.endswith("t.json")
        assert plugin.export_count == 0

    def test_file_sink_explicit(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_OTEL_ENDPOINT", "http://collector:4318")
        plugin = PhosonOtelPlugin()
        plugin.configure({"sink": "file", "file_path": str(tmp_path / "f.json")})
        assert plugin.sink_path.endswith("f.json")

    def test_auto_picks_otlp_when_endpoint(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_OTEL_ENDPOINT", "http://collector:4318")
        plugin = PhosonOtelPlugin()
        plugin.configure({})
        assert plugin.sink_path == "http://collector:4318/v1/traces"

    def test_auto_picks_file_without_endpoint(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("PHOSON_OTEL_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "f.json"))
        plugin = PhosonOtelPlugin()
        plugin.configure({})
        assert plugin.sink_path.endswith("f.json")

    def test_otlp_without_endpoint_falls_back_to_file(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        monkeypatch.delenv("PHOSON_OTEL_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "f.json"))
        plugin = PhosonOtelPlugin()
        with caplog.at_level("WARNING"):
            plugin.configure({"sink": "otlp"})
        assert plugin.sink_path.endswith("f.json")
        assert any("no endpoint" in m for m in caplog.messages)

    def test_unknown_sink_warns_and_uses_auto(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        monkeypatch.delenv("PHOSON_OTEL_ENDPOINT", raising=False)
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "f.json"))
        plugin = PhosonOtelPlugin()
        with caplog.at_level("WARNING"):
            plugin.configure({"sink": "warp"})
        assert plugin.sink_path.endswith("f.json")

    def test_resource_attributes_merged(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        plugin.configure(
            {
                "service_name": "svc",
                "resource_attributes": {"deployment.environment": "dev"},
            }
        )
        assert plugin._resource["service.name"] == "svc"  # noqa: SLF001
        assert plugin._resource["deployment.environment"] == "dev"  # noqa: SLF001
        assert plugin._resource["telemetry.sdk.language"] == "python"  # noqa: SLF001

    def test_invalid_resource_attributes_ignored(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        with caplog.at_level("WARNING"):
            plugin.configure({"resource_attributes": "nope"})
        assert plugin._resource.get("deployment.environment") is None  # noqa: SLF001

    def test_service_name_env_fallback(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_SERVICE_NAME", "from-env")
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        plugin.configure({})
        assert plugin._resource["service.name"] == "from-env"  # noqa: SLF001

    def test_merge_semantics_reload_keeps_values(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The loader re-calls ``configure({})`` on reload; explicit
        values must survive (monitor-plugin contract)."""
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        plugin.configure({"service_name": "svc", "file_path": str(tmp_path / "a.json")})
        plugin.configure({})  # reload
        assert plugin._resource["service.name"] == "svc"  # noqa: SLF001
        assert plugin.sink_path.endswith("a.json")

    def test_unknown_config_key_warns(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        with caplog.at_level("WARNING"):
            plugin.configure({"wat": 1})
        assert any("unknown config key" in m for m in caplog.messages)


# ── OTLP HTTP sink ─────────────────────────────────────────────────────────────


class _CollectorHandler(BaseHTTPRequestHandler):
    """Captures POST bodies; the test reads ``received`` afterwards."""

    received: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append(body)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args) -> None:  # silence
        pass


@pytest.fixture()
def collector():
    server = HTTPServer(("127.0.0.1", 0), _CollectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _CollectorHandler.received = []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class TestOtlpHttpSink:
    def test_posts_otlp_body(self, collector: str, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_OTEL_ENDPOINT", collector)
        plugin = PhosonOtelPlugin()
        plugin.configure({})
        assert plugin.sink_path == f"{collector}/v1/traces"

        _run_start_done(plugin)

        # Wait for the background export thread.
        deadline = time.time() + 5
        while not _CollectorHandler.received and time.time() < deadline:
            time.sleep(0.01)
        assert len(_CollectorHandler.received) == 1
        doc = json.loads(_CollectorHandler.received[0])
        assert doc[PHOSON_TRACE_KEY]["span_count"] == 1  # run span only
        assert doc["resourceSpans"][0]["resource"]["attributes"]

    def test_endpoint_gets_http_prefix(self, monkeypatch) -> None:
        plugin = PhosonOtelPlugin()
        plugin.configure({"sink": "otlp", "otlp_endpoint": "collector.local:4318"})
        assert plugin.sink_path == "http://collector.local:4318/v1/traces"


# ── Export plumbing / cleanup flush ────────────────────────────────────────────


class TestExportAndCleanup:
    def test_export_count_increments(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        plugin.configure({})
        _run_start_done(plugin)
        assert plugin.export_count == 1
        assert (tmp_path / "t.json").exists()

    def test_cleanup_flushes_in_flight_run(self, tmp_path: Path, monkeypatch) -> None:
        """A run interrupted before Done/Error is flushed on cleanup
        (so nightly gate runs that die mid-iteration still export)."""
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(tmp_path / "t.json"))
        plugin = PhosonOtelPlugin()
        plugin.configure({})

        async def _go() -> None:
            assert plugin._middleware is not None  # noqa: SLF001
            await plugin._middleware.on_agent_event(AgentStartEvent(model="m"))
            plugin.cleanup()  # in the run's context → flushes

        asyncio.run(_go())
        assert plugin.export_count == 1
        doc = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
        assert doc[PHOSON_TRACE_KEY]["span_count"] >= 1  # at least the run span

    def test_export_failure_is_swallowed(self, tmp_path: Path, monkeypatch) -> None:
        # Unwritable directory → the write raises, but the run survives.
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        monkeypatch.setenv("PHOSON_OTEL_TRACE_FILE", str(ro / "t.json"))
        try:
            plugin = PhosonOtelPlugin()
            plugin.configure({})
            _run_start_done(plugin)  # must not raise
        finally:
            ro.chmod(0o700)
        assert plugin.export_count == 0
