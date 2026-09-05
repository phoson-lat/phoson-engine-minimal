"""Unit tests for the OTel span model + trace builder (issue #140).

Covers the W3C id formats, the Python→OTLP/JSON attribute mapping, the
``Span`` object shape, and the ``ExportTraceServiceRequest`` body that
slice 2 will POST verbatim to ``/v1/traces``.
"""

import re

from phoson_plugin_otel.span import (
    STATUS_OK,
    STATUS_ERROR,
    STATUS_UNSET,
    SPAN_KIND_CLIENT,
    SPAN_KIND_INTERNAL,
    OtelSpan,
    to_ns,
    attr_value,
    build_trace,
    new_span_id,
    new_trace_id,
    build_resource,
    trace_id_from_env,
)

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


# ── Ids ────────────────────────────────────────────────────────────────────────


class TestIds:
    def test_trace_id_w3c_format(self) -> None:
        assert _HEX32.match(new_trace_id())

    def test_span_id_w3c_format(self) -> None:
        assert _HEX16.match(new_span_id())

    def test_ids_are_unique(self) -> None:
        traces = {new_trace_id() for _ in range(100)}
        spans = {new_span_id() for _ in range(100)}
        assert len(traces) == 100
        assert len(spans) == 100

    def test_trace_id_from_env_valid(self, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_OTEL_TRACE_ID", "ab" * 16)
        assert trace_id_from_env() == "ab" * 16

    def test_trace_id_from_env_rejects_uppercase(self, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_OTEL_TRACE_ID", "AB" * 16)
        assert trace_id_from_env() == ""

    def test_trace_id_from_env_rejects_wrong_length(self, monkeypatch) -> None:
        monkeypatch.setenv("PHOSON_OTEL_TRACE_ID", "abc123")
        assert trace_id_from_env() == ""

    def test_trace_id_from_env_absent(self, monkeypatch) -> None:
        monkeypatch.delenv("PHOSON_OTEL_TRACE_ID", raising=False)
        assert trace_id_from_env() == ""


# ── Timestamps ─────────────────────────────────────────────────────────────────


class TestTimestamps:
    def test_to_ns_aware(self) -> None:
        from datetime import UTC, datetime

        dt = datetime(2026, 9, 5, 0, 0, 0, tzinfo=UTC)
        assert to_ns(dt) == 1_788_566_400_000_000_000

    def test_span_default_start_is_now(self) -> None:
        import time

        before = time.time_ns()
        span = OtelSpan(name="x", trace_id="ab" * 16)
        after = time.time_ns()
        assert before <= span.start_time <= after

    def test_end_clamped_to_start(self) -> None:
        span = OtelSpan(name="x", trace_id="ab" * 16, start_time=1_000, end_time=500)
        assert span.end_time == 1_000

    def test_end_method_clamps(self) -> None:
        span = OtelSpan(name="x", trace_id="ab" * 16, start_time=5_000)
        span.end(end_time=1_000)
        assert span.end_time == 5_000


# ── Attribute mapping ──────────────────────────────────────────────────────────


class TestAttrValue:
    def test_string(self) -> None:
        assert attr_value("hi") == {"stringValue": "hi"}

    def test_bool_before_int(self) -> None:
        assert attr_value(True) == {"boolValue": True}
        assert attr_value(False) == {"boolValue": False}

    def test_int_is_stringified(self) -> None:
        assert attr_value(42) == {"intValue": "42"}
        assert attr_value(0) == {"intValue": "0"}

    def test_float(self) -> None:
        assert attr_value(0.5) == {"doubleValue": 0.5}

    def test_list(self) -> None:
        assert attr_value([1, "a"]) == {
            "arrayValue": {"values": [{"intValue": "1"}, {"stringValue": "a"}]}
        }

    def test_dict(self) -> None:
        assert attr_value({"a": 1}) == {
            "kvlistValue": {"values": [{"key": "a", "value": {"intValue": "1"}}]}
        }

    def test_nested(self) -> None:
        assert attr_value({"x": [1, "b"]})["kvlistValue"]["values"][0]["value"][
            "arrayValue"
        ] == {"values": [{"intValue": "1"}, {"stringValue": "b"}]}

    def test_none_becomes_empty(self) -> None:
        assert attr_value(None) == {}

    def test_unsupported_becomes_empty(self) -> None:
        assert attr_value(object()) == {}


# ── OtelSpan ───────────────────────────────────────────────────────────────────


class TestOtelSpan:
    def _span(self, name: str = "phoson.run", **kw) -> OtelSpan:
        return OtelSpan(name=name, trace_id="ab" * 16, **kw)

    def test_defaults(self) -> None:
        span = self._span()
        assert span.kind == SPAN_KIND_INTERNAL
        assert span.status == STATUS_UNSET
        assert span.parent is None
        assert _HEX16.match(span.span_id)

    def test_set_attribute_and_bulk(self) -> None:
        span = self._span()
        span.set_attribute("a", 1)
        span.set_attributes({"b": "two", "a": 99})
        assert span.attributes == {"a": 99, "b": "two"}

    def test_set_status_error_stores_description(self) -> None:
        span = self._span()
        span.set_status(STATUS_ERROR, "boom")
        assert span.status == STATUS_ERROR
        assert span.attributes["otel.status_description"] == "boom"

    def test_set_status_ok_no_description(self) -> None:
        span = self._span()
        span.set_status(STATUS_OK, "fine")
        assert "otel.status_description" not in span.attributes

    def test_add_event(self) -> None:
        span = self._span()
        span.add_event("exception", {"code": 1})
        assert span.events == [
            {
                "name": "exception",
                "attributes": [{"key": "code", "value": {"intValue": "1"}}],
            }
        ]

    def test_to_otlp_json_root(self) -> None:
        span = self._span(start_time=100, end_time=200)
        span.set_attribute("phoson.model", "m1")
        out = span.to_otlp_json()
        assert out["traceId"] == "ab" * 16
        assert out["name"] == "phoson.run"
        assert out["kind"] == SPAN_KIND_INTERNAL
        assert out["startTimeUnixNano"] == "100"
        assert out["endTimeUnixNano"] == "200"
        assert out["status"] == {"code": STATUS_UNSET}
        assert "parentSpanId" not in out
        assert out["attributes"] == [
            {"key": "phoson.model", "value": {"stringValue": "m1"}}
        ]

    def test_to_otlp_json_child_has_parent(self) -> None:
        parent = self._span()
        child = self._span(
            name="phoson.step", kind=SPAN_KIND_CLIENT, parent_id=parent.span_id
        )
        out = child.to_otlp_json()
        assert out["parentSpanId"] == parent.span_id
        assert out["kind"] == SPAN_KIND_CLIENT

    def test_attributes_are_sorted(self) -> None:
        span = self._span()
        span.set_attributes({"z": 1, "a": 2, "m": 3})
        keys = [a["key"] for a in span.to_otlp_json()["attributes"]]
        assert keys == ["a", "m", "z"]


# ── Trace + resource ───────────────────────────────────────────────────────────


class TestBuildTrace:
    def test_body_shape(self) -> None:
        root = OtelSpan(name="phoson.run", trace_id="ab" * 16)
        child = OtelSpan(name="phoson.step", trace_id="ab" * 16, parent_id=root.span_id)
        body = build_trace(
            [root, child],
            {"service.name": "phoson"},
            scope_version="0.1.0",
        )
        rs = body["resourceSpans"]
        assert len(rs) == 1
        assert rs[0]["resource"]["attributes"] == [
            {"key": "service.name", "value": {"stringValue": "phoson"}}
        ]
        scope_spans = rs[0]["scopeSpans"]
        assert scope_spans[0]["scope"] == {
            "name": "phoson_plugin_otel",
            "version": "0.1.0",
        }
        assert [s["name"] for s in scope_spans[0]["spans"]] == [
            "phoson.run",
            "phoson.step",
        ]

    def test_scope_omits_version_when_empty(self) -> None:
        body = build_trace([], {"a": "b"})
        assert body["resourceSpans"][0]["scopeSpans"][0]["scope"] == {
            "name": "phoson_plugin_otel"
        }

    def test_empty_spans_still_valid_body(self) -> None:
        body = build_trace([])
        assert body["resourceSpans"][0]["scopeSpans"][0]["spans"] == []

    def test_build_resource_extra_overrides(self) -> None:
        resource = build_resource("svc", {"service.name": "override", "x": "y"})
        assert resource["service.name"] == "override"
        assert resource["x"] == "y"
        assert resource["telemetry.sdk.name"] == "phoson"
