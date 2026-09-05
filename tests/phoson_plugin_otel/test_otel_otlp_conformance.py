"""Collector-grade conformance test for the OTLP/HTTP JSON wire format.

The OTLP/HTTP *JSON* encoding (OTEP-0122) is a proto3-JSON mapping with
two documented deviations from generic ``google.protobuf.json_format``:

1. ``traceId``/``spanId`` byte arrays are **hex** strings (NOT base64).
2. ``traceId`` receivers MUST be 128-bit / ``spanId`` 64-bit.

The generic protojson parser does *not* implement deviation (1) — it
base64-decodes ids — so it is the right oracle for the **shape** of the
message (resource/scope/attributes/status/timestamps/kind, int-as-string,
enum-as-int, lowerCamelCase) but the wrong oracle for the id *value*.

This test therefore splits the check:

* **Source-level** (what an OTLP/JSON receiver actually reads): the
  ids are exactly 32/16 lowercase-hex, parent linkage holds, and the
  body has the ``resourceSpans``/``scopeSpans``/``spans`` envelope.
* **Schema-level**: the message is valid proto3-JSON — decoded with the
  real ``opentelemetry-proto`` schema (after mapping the hex ids to the
  base64 form the *generic* parser expects, and honoring the spec's
  "ignore unknown fields" rule). This exercises every other field's
  encoding.

``opentelemetry-proto`` is an **optional, test-only** dependency used as
the conformance oracle — the plugin runtime stays stdlib-only. The test
skips cleanly when the package is absent (the default in a minimal env).
"""

import re
import json
import base64

import pytest

proto = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
json_format = pytest.importorskip("google.protobuf.json_format")

from phoson_plugin_otel.span import OtelSpan, build_trace, new_trace_id  # noqa: E402

ExportTraceServiceRequest = proto.ExportTraceServiceRequest
Parse = json_format.Parse

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _tree() -> tuple[list[OtelSpan], str]:
    trace_id = new_trace_id()
    run = OtelSpan(
        name="phoson.run", trace_id=trace_id, start_time=10**18, end_time=2 * 10**18
    )
    run.set_attribute("phoson.run.id", "rid-1")
    run.set_attribute("phoson.total_cost_usd", 0.03)
    step = OtelSpan(
        name="phoson.step",
        trace_id=trace_id,
        parent_id=run.span_id,
        start_time=10**18,
        end_time=2 * 10**18,
    )
    step.set_attribute("phoson.step.index", 0)
    llm = OtelSpan(
        name="phoson.llm_call",
        trace_id=trace_id,
        kind=3,  # SPAN_KIND_CLIENT
        parent_id=step.span_id,
        start_time=10**18,
        end_time=2 * 10**18,
    )
    llm.set_attribute("gen_ai.request.model", "test-model")
    llm.set_attribute("gen_ai.usage.input_tokens", 120)
    return [run, step, llm], trace_id


def _spans_of(doc: dict) -> list[dict]:
    return doc["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _to_base64_ids(doc: dict) -> dict:
    """Re-encode hex ids as base64 so the *generic* protojson oracle
    accepts them as `bytes` (it cannot apply OTLP's hex deviation)."""
    import copy

    doc = copy.deepcopy(doc)
    for span in _spans_of(doc):
        if "traceId" in span:
            span["traceId"] = base64.b64encode(bytes.fromhex(span["traceId"])).decode()
        if "spanId" in span:
            span["spanId"] = base64.b64encode(bytes.fromhex(span["spanId"])).decode()
        if "parentSpanId" in span:
            span["parentSpanId"] = base64.b64encode(
                bytes.fromhex(span["parentSpanId"])
            ).decode()
    return doc


# ── Source-level: the exact bytes an OTLP/JSON receiver reads ─────────────────


def test_ids_are_spec_hex() -> None:
    """OTLP deviation (1)+(2): hex ids, 32/16 chars, lowercase."""
    spans, trace_id = _tree()
    doc = json.loads(json.dumps(build_trace(spans, {"service.name": "svc"})))
    assert _HEX32.match(trace_id)
    for span in _spans_of(doc):
        assert _HEX32.match(span["traceId"]), "traceId must be 32 hex"
        assert _HEX16.match(span["spanId"]), "spanId must be 16 hex"
        if "parentSpanId" in span:
            assert _HEX16.match(span["parentSpanId"])


def test_envelope_and_parent_linkage() -> None:
    spans, trace_id = _tree()
    doc = json.loads(json.dumps(build_trace(spans, {"service.name": "svc"})))
    assert set(doc) == {"resourceSpans"}
    assert len(doc["resourceSpans"]) == 1
    ss = doc["resourceSpans"][0]["scopeSpans"]
    assert len(ss) == 1 and ss[0]["scope"]["name"] == "phoson_plugin_otel"
    spans_j = _spans_of(doc)
    by_id = {s["spanId"]: s for s in spans_j}
    for span in spans_j:
        assert span["traceId"] == trace_id  # one trace per run
        if "parentSpanId" in span:
            assert span["parentSpanId"] in by_id  # every parent is present


def test_enum_and_int_encodings() -> None:
    """OTLP rules: enums as integers; 64-bit ints as decimal strings."""
    spans, _ = _tree()
    doc = json.loads(json.dumps(build_trace(spans, {"service.name": "svc"})))
    spans_j = _spans_of(doc)
    # kind is an integer enum value (client span = 3)
    assert spans_j[2]["kind"] == 3
    # span status code is an integer
    assert isinstance(spans_j[0]["status"]["code"], int)
    # start/end times are 64-bit ints → decimal strings
    assert re.match(r"^\d+$", spans_j[0]["startTimeUnixNano"])
    # an int attribute is a decimal string
    llm_attrs = {a["key"]: a["value"] for a in spans_j[2]["attributes"]}
    assert llm_attrs["gen_ai.usage.input_tokens"] == {"intValue": "120"}


# ── Schema-level: valid proto3-JSON against the real otel-proto schema ────────


def _parse_schema(doc: dict) -> ExportTraceServiceRequest:
    req = ExportTraceServiceRequest()
    # Spec: "OTLP/JSON receivers MUST ignore message fields with unknown
    # names" → mirror that with ignore_unknown_fields=True.
    Parse(json.dumps(_to_base64_ids(doc)), req, ignore_unknown_fields=True)
    return req


def test_message_is_valid_proto3_json() -> None:
    spans, _ = _tree()
    doc = json.loads(json.dumps(build_trace(spans, {"service.name": "svc"})))
    req = _parse_schema(doc)
    assert len(req.resource_spans) == 1
    spans_pb = req.resource_spans[0].scope_spans[0].spans
    assert len(spans_pb) == 3
    assert spans_pb[0].name == "phoson.run"
    assert spans_pb[2].name == "phoson.llm_call"
    # kind survived as the integer enum
    assert spans_pb[2].kind == 3
    # parent linkage (decoded ids are base64-consistent, so comparable)
    assert spans_pb[2].parent_span_id == spans_pb[1].span_id


def test_attribute_values_roundtrip_through_schema() -> None:
    spans, _ = _tree()
    doc = json.loads(json.dumps(build_trace(spans, {"service.name": "svc"})))
    req = _parse_schema(doc)
    spans_pb = req.resource_spans[0].scope_spans[0].spans

    def attr(idx, key):
        for a in spans_pb[idx].attributes:
            if a.key == key:
                return a.value
        raise AssertionError(f"attr {key!r} missing on span {idx}")

    assert attr(2, "gen_ai.usage.input_tokens").int_value == 120
    assert attr(0, "phoson.total_cost_usd").double_value == pytest.approx(0.03)
    assert attr(2, "gen_ai.request.model").string_value == "test-model"


def test_resource_service_name_roundtrips_through_schema() -> None:
    spans, _ = _tree()
    doc = json.loads(json.dumps(build_trace(spans, {"service.name": "my-svc"})))
    req = _parse_schema(doc)
    attrs = {a.key: a.value for a in req.resource_spans[0].resource.attributes}
    assert attrs["service.name"].string_value == "my-svc"
