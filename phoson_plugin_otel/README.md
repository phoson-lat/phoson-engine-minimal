# phoson_plugin_otel

OpenTelemetry tracing for Phoson (issue #140, **slice 1**: local
trace-file sink).

Every agent run — main engine *and* sub-agents — is traced as an OTel
span tree and exported in **OTLP/HTTP JSON** shape (the
`ExportTraceServiceRequest` body, `application/json`, the format the
OTLP `/v1/traces` endpoint expects). Slice 1 writes that body to a
local JSON file; no collector or extra dependency is required, and the
same document can be replayed into any OTLP-compatible tool (Jaeger,
the OTel collector, `otlpreplay`).

```
phoson.run                     one per engine.run() (or sub-agent run)
└── phoson.step                one per RunStep   (phoson.step.index)
    ├── phoson.llm_call        kind="llm"  — model, tokens, cache, cost, credits
    └── phoson.tool_call       kind="tool" — name, call id, outcome, args, result size
```

## Enabling

Off by default (tracing must be opt-in — it writes a file per run).

```toml
# ~/.phoson/config.toml
[defaults]
enable_otel = true
# optional:
otel_service_name = "my-app"          # resource service.name
otel_file_path = ".phoson/trace.json" # {trace_id} supported → one file per run
otel_endpoint = "http://localhost:4318"  # or set the env var below
```

Env overrides: `PHOSON_ENABLE_OTEL`, `PHOSON_SERVICE_NAME`,
`PHOSON_OTEL_TRACE_FILE`, `PHOSON_OTEL_ENDPOINT` (also honors the
standard `OTEL_EXPORTER_OTLP_ENDPOINT`).

Alternatively, list it like any other plugin (your spec wins over
`enable_otel`, no double-tracing):

```toml
[defaults]
plugins = [
  { name = "phoson-plugin-otel", config = { sink = "file", service_name = "my-app" } }
]
```

## Configuration keys

| Key | Default | Meaning |
| --- | --- | --- |
| `sink` | `auto` | `auto` = OTLP when an endpoint is configured, file otherwise; `file`; `otlp` |
| `service_name` | `$PHOSON_SERVICE_NAME` or `phoson` | resource `service.name` |
| `file_path` | `.phoson/trace.json` | file-sink path; `{trace_id}` → one file per run |
| `otlp_endpoint` | `$PHOSON_OTEL_ENDPOINT` / `$OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP base URL |
| `otlp_timeout_s` | `5.0` | export timeout |
| `resource_attributes` | `{}` | extra resource attributes (e.g. `deployment.environment`) |

Correlation: set `PHOSON_OTEL_TRACE_ID` to a 32-hex-char W3C trace id
to pin the trace id (e.g. from a CI runner).

## Output

The file is the OTLP/HTTP JSON body plus a small `phoson_trace`
envelope:

```json
{
  "phoson_trace": {
    "schema_version": 1,
    "trace_id": "62a30871…",
    "span_count": 7,
    "export_format": "otlp_http_json",
    "run": { "run_id": "1a2cc2dc…" }
  },
  "resourceSpans": [
    { "resource": { "attributes": [ … ] }, "scopeSpans": [ { "scope": {…}, "spans": [ … ] } ] }
  ]
}
```

- `jq '.phoson_trace'` → which run is this;
- `jq '.resourceSpans'` → the OTLP body, feedable straight into any
  OTLP consumer.

Span attributes use the `gen_ai.*` semantic conventions where they
apply (`gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read_tokens`,
`gen_ai.usage.cache_write_tokens`) plus `phoson.*` for run/step/tool
metadata. Tool outcomes are normalized: `ok`, `error`,
`denied_by_permission`, `denied_by_middleware`, `unusable_args`.

## Design notes

- **One middleware, zero engine changes.** The engine funnels every
  public event through `on_agent_event`; the plugin builds the tree
  from `AgentStepDoneEvent`, whose `RunStep` carries the exact
  usage/cost the CLI reports in `/cost` and `/tokens` — so span
  attributes cannot drift from those commands.
- **Shared instance, isolated runs.** The CLI hands the *same*
  middleware instance to the parent engine and every sub-agent engine
  (#174/F-01). Active-run state lives in a `contextvars.ContextVar`,
  so parallel sub-agents (separate tasks) never interleave traces, and
  a sub-agent run never clobbers the parent's in-flight run.
- **Best-effort, by design.** Export failures (disk, dead collector)
  log a warning and are swallowed — observability must never take a run
  down with it. A run still open when the plugin is cleaned up (host
  shutdown mid-run) is flushed at `cleanup()`.
- **Stdlib only.** No `opentelemetry-*` dependency. Slice 2 may prefer
  the official SDK/exporter when present, reusing this span tree and
  the already-shipped minimal OTLP HTTP sink.

## Development

```bash
uv run pytest tests/phoson_plugin_otel/ -q
```
