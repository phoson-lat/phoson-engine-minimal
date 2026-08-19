# TODO tecnico — phoson-engine-minimal

> **2026-08-19:** Backlog completo cerrado en la rama
> `fix/review-fixes-and-backlog-items`. Los 12 issues originales
> (#18–#29) quedaron resueltos; este archivo queda como índice de lo
> hecho y de lo pendiente de merge.

## Issues resueltos (rama fix/review-fixes-and-backlog-items)

| # | Área | Issue |
|---|------|-------|
| 1 | Contrato de `ToolHandler` documentado + tests de regresión | [#18](https://github.com/phoson-lat/phoson-engine-minimal/issues/18) |
| 2 | Tests de `SummarizationMiddleware.wrap_llm_call` (resumen real, sin fuga de prompt, sin doble compaction) | [#19](https://github.com/phoson-lat/phoson-engine-minimal/issues/19) |
| 3 | Barrido de docs (README + docs/api + examples) + snippets críticos con tests | [#20](https://github.com/phoson-lat/phoson-engine-minimal/issues/20) |
| 4 | `ToolCallAccumulator` extraída + 20 tests directos del helper OpenAI-compatible | [#21](https://github.com/phoson-lat/phoson-engine-minimal/issues/21) |
| 5 | Aliases JSON (`JsonValue`/`JsonObject`/`JsonSchema`) aplicados en schemas + adapters | [#22](https://github.com/phoson-lat/phoson-engine-minimal/issues/22) |
| 6 | Política de logging: `logger` por módulo, warning/debug en fallbacks | [#23](https://github.com/phoson-lat/phoson-engine-minimal/issues/23) |
| 7 | Limitaciones de las APIs sync documentadas (`stream_sync` bufferiza, `run_sync` loop check) | [#24](https://github.com/phoson-lat/phoson-engine-minimal/issues/24) |
| 8 | `JsonlStorage` I/O async (ya usaba `asyncio.to_thread`; item obsoleto) | [#25](https://github.com/phoson-lat/phoson-engine-minimal/issues/25) |
| 9 | `plugin_loader`: `_sys_path_guard` con restore exacto + 6 tests | [#26](https://github.com/phoson-lat/phoson-engine-minimal/issues/26) |
| 10 | `plugin.py` → `_plugin.py` en los 3 plugins (adiós shadowing) + convención documentada | [#27](https://github.com/phoson-lat/phoson-engine-minimal/issues/27) |
| 11 | Consistencia de idioma: API pública 100% inglés; español solo en demos | [#28](https://github.com/phoson-lat/phoson-engine-minimal/issues/28) |
| 12 | Auditoría de eventos: `AgentSubagentResult` y `LLMModalitiesEvent` marcados experimental | [#29](https://github.com/phoson-lat/phoson-engine-minimal/issues/29) |

## Pendiente (fuera de esta rama)

- Merge del PR y tag de release.
- Opcional: infraestructura de doctests para snippets de docs.
- Opcional: wirear `AgentSubagentResult` / `LLMModalitiesEvent` cuando
  existan sus features (subagent orquestación, modality discovery).

## Verificaciones (rama)

```bash
uv sync --dev --all-extras
uv run ruff format --check . && uv run ruff check .
uv run pyright
uv run pytest -q
```

Última verificación: `584 passed, 0 skipped` (con backends de test),
`pyright 0 errors`, `ruff clean`.
