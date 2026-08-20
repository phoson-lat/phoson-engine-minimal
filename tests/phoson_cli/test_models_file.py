"""Tests for ~/.phoson/models.json — registry, provider config, list cache."""

import os
import json
import stat
import time
import warnings
from unittest.mock import patch

import pytest

from phoson_cli.models import (
    update_cache,
    cache_is_fresh,
    cached_options,
    option_to_dict,
    cache_fetched_at,
    load_models_file,
    save_models_file,
    provider_settings,
    user_model_overrides,
    apply_model_overrides,
    resolve_context_window,
)
from phoson_cli.model_selector import ModelOption


def _make_repl(tmp_path):
    from unittest.mock import MagicMock

    from phoson_cli.repl import PhosonRepl
    from phoson_cli.config import PhosonConfig

    with patch("phoson_cli.repl.build_chat") as mock_build:
        mock_build.return_value = MagicMock()
        config = PhosonConfig(provider="ollama", sessions_dir=tmp_path)
        return PhosonRepl(config)


def _option(id="m1", provider="openrouter", **kw) -> ModelOption:
    return ModelOption(
        id=id,
        label=id,
        provider=provider,
        description=kw.pop("description", ""),
        context_length=kw.pop("context_length", None),
        pricing=kw.pop("pricing", ""),
    )


# ── load / save ──────────────────────────────────────────────────────────────


def test_load_missing_file_returns_empty(tmp_path) -> None:
    assert load_models_file(tmp_path / "nope.json") == {}


def test_load_invalid_json_returns_empty_and_warns(tmp_path, capsys) -> None:
    p = tmp_path / "models.json"
    p.write_text("{not json")
    assert load_models_file(p) == {}
    assert "not valid JSON" in capsys.readouterr().out


def test_load_non_dict_returns_empty(tmp_path) -> None:
    p = tmp_path / "models.json"
    p.write_text("[1, 2, 3]")
    assert load_models_file(p) == {}


def test_save_and_load_roundtrip_with_0600(tmp_path) -> None:
    p = tmp_path / "models.json"
    data = {
        "models": {"qwen3.8-27b": {"context_window": 262144}},
        "providers": {"openrouter": {"default_model": "qwen3.8-27b"}},
    }
    save_models_file(data, p)
    assert load_models_file(p) == data
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600


# ── models section ───────────────────────────────────────────────────────────


def test_user_model_overrides_filters_malformed() -> None:
    data = {
        "models": {
            "ok": {"context_window": 1000},
            "not-a-dict": "oops",
            "also-ok": {"label": "X"},
        }
    }
    overrides = user_model_overrides(data)
    assert set(overrides) == {"ok", "also-ok"}


def test_apply_overrides_wins_and_appends_user_only() -> None:
    data = {
        "models": {
            "m1": {"context_window": 999999, "label": "M1 Custom"},
            "my-local-model": {
                "context_window": 32768,
                "description": "runs on my machine",
            },
        }
    }
    options = [
        _option("m1", context_length=8192, description="original"),
        _option("m2"),
    ]
    result = apply_model_overrides(data, options, "openrouter")
    by_id = {o.id: o for o in result}

    assert by_id["m1"].context_length == 999999
    assert by_id["m1"].label == "M1 Custom"
    assert by_id["m1"].description == "original"  # not overridden
    assert by_id["m2"].context_length is None  # untouched
    local = by_id["my-local-model"]
    assert local.context_length == 32768
    assert local.description == "runs on my machine"
    assert local.provider == "openrouter"


def test_apply_overrides_no_section_is_noop() -> None:
    options = [_option("m1")]
    assert apply_model_overrides({}, options, "p") is not options
    assert apply_model_overrides({}, options, "p") == options


def test_resolve_context_window_priority() -> None:
    data = {
        "models": {"m1": {"context_window": 555}},
        "cache": {
            "fetched_at": time.time(),
            "providers": {
                "openrouter": [
                    {"id": "m1", "context_length": 111},
                    {"id": "m2", "context_length": 222},
                ]
            },
        },
    }
    # User override wins over cache.
    assert resolve_context_window(data, "openrouter", "m1") == 555
    # Cache hit.
    assert resolve_context_window(data, "openrouter", "m2") == 222
    # Unknown → None (engine resolver takes over).
    assert resolve_context_window(data, "openrouter", "m3") is None
    # Cache without entry.
    assert resolve_context_window({}, "openrouter", "m2") is None


# ── providers section ────────────────────────────────────────────────────────


def test_provider_settings_allowlist_only() -> None:
    data = {
        "providers": {
            "openrouter": {
                "default_model": "qwen3.8-27b",
                "base_url": "https://proxy.example/v1",
                "api_key": "must-be-ignored",
                "weird": 42,
            }
        }
    }
    settings = provider_settings(data, "openrouter")
    assert settings == {
        "default_model": "qwen3.8-27b",
        "base_url": "https://proxy.example/v1",
    }
    assert provider_settings(data, "unknown") == {}


# ── cache section ────────────────────────────────────────────────────────────


def test_cache_freshness() -> None:
    data = {"cache": {"fetched_at": time.time() - 10}}
    assert cache_is_fresh(data, ttl=3600) is True
    assert cache_is_fresh(data, ttl=5) is False
    assert cache_is_fresh({}, ttl=3600) is False
    assert cache_fetched_at({}) == 0.0


def test_update_cache_preserves_other_providers() -> None:
    data = {
        "models": {"keep": {"context_window": 1}},
        "cache": {
            "fetched_at": time.time() - 9999,
            "providers": {"groq": [{"id": "g1"}]},
        },
    }
    updated = update_cache(data, {"openrouter": [{"id": "r1"}]})
    assert set(updated["cache"]["providers"]) == {"groq", "openrouter"}
    assert cache_fetched_at(updated) > cache_fetched_at(data)
    assert updated["models"] == {"keep": {"context_window": 1}}
    # Original untouched (copy semantics).
    assert "openrouter" not in data["cache"]["providers"]


def test_cached_options_roundtrip() -> None:
    options = [
        _option("r1", provider="openrouter", context_length=131072, pricing="$1/1M"),
        _option("r2", provider="openrouter"),
    ]
    data = update_cache({}, {"openrouter": [option_to_dict(o) for o in options]})
    rebuilt = cached_options(data, "openrouter")
    assert rebuilt == options
    assert cached_options(data, "unknown-provider") == []


def test_cached_options_skips_malformed_entries() -> None:
    data = {
        "cache": {
            "providers": {
                "p": [
                    {"id": "ok", "label": "OK"},
                    "garbage",
                    {"no-id": True},
                ]
            }
        }
    }
    rebuilt = cached_options(data, "p")
    assert [o.id for o in rebuilt] == ["ok"]


def test_option_to_dict_serializable() -> None:
    d = option_to_dict(_option("m", context_length=42, pricing="$1"))
    assert json.loads(json.dumps(d)) == d


# ── list_available_models: cache-first behavior ──────────────────────────────


@pytest.mark.asyncio
async def test_fresh_cache_skips_network(monkeypatch, tmp_path) -> None:
    from phoson_cli import model_selector
    from phoson_cli.config import PhosonConfig

    models_path = tmp_path / "models.json"
    now = time.time()
    save_models_file(
        {
            "cache": {
                "fetched_at": now,
                "providers": {"openrouter": [option_to_dict(_option("r1"))]},
            }
        },
        models_path,
    )
    monkeypatch.setattr(
        model_selector, "load_models_file", lambda: load_models_file(models_path)
    )

    called = False

    async def _boom(config):
        nonlocal called
        called = True
        return [_option(config.model, provider="openrouter")]

    monkeypatch.setattr(model_selector, "_fetch_provider_models", _boom)
    config = PhosonConfig(provider="openrouter", model="current")

    options = await model_selector.list_available_models(config)
    assert called is False
    ids = {o.id for o in options}
    assert "r1" in ids  # from cache
    assert [o.id for o in options][:1] == ["current"] or "current" in ids


@pytest.mark.asyncio
async def test_network_success_refreshes_cache(monkeypatch, tmp_path) -> None:
    from phoson_cli import model_selector
    from phoson_cli.config import PhosonConfig

    models_path = tmp_path / "models.json"
    data = {}
    monkeypatch.setattr(model_selector, "load_models_file", lambda: data)
    save_models_file(data, models_path)  # ensure the path exists as default
    saved: list[dict] = []
    monkeypatch.setattr(
        model_selector, "save_models_file", lambda d, p=None: saved.append(d)
    )

    async def _fetch(config):
        return [
            _option("r1", provider="openrouter", context_length=1000),
            _option("r2", provider="openrouter"),
        ]

    monkeypatch.setattr(model_selector, "_fetch_provider_models", _fetch)
    config = PhosonConfig(provider="openrouter", model="r1")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        options = await model_selector.list_available_models(config)

    assert [o.id for o in options] == ["r1", "r2"]
    assert len(saved) == 1
    assert "openrouter" in saved[0]["cache"]["providers"]


@pytest.mark.asyncio
async def test_network_fallback_uses_stale_cache(monkeypatch, tmp_path) -> None:
    from phoson_cli import model_selector
    from phoson_cli.config import PhosonConfig

    models_path = tmp_path / "models.json"
    stale = time.time() - 2 * 86400  # 2 days old (> 24h TTL)
    save_models_file(
        {
            "cache": {
                "fetched_at": stale,
                "providers": {"openrouter": [option_to_dict(_option("r9"))]},
            }
        },
        models_path,
    )
    monkeypatch.setattr(
        model_selector, "load_models_file", lambda: load_models_file(models_path)
    )

    async def _fetch(config):
        # The lister's single-item fallback on network failure.
        return [_option(config.model, provider="openrouter")]

    monkeypatch.setattr(model_selector, "_fetch_provider_models", _fetch)
    config = PhosonConfig(provider="openrouter", model="current")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        options = await model_selector.list_available_models(config)

    assert any("cached" in str(w.message) for w in caught)
    ids = {o.id for o in options}
    assert "r9" in ids  # from stale cache
    assert "current" in ids  # current model always selectable


@pytest.mark.asyncio
async def test_fallback_without_cache_keeps_single_model(monkeypatch) -> None:
    from phoson_cli import model_selector
    from phoson_cli.config import PhosonConfig

    monkeypatch.setattr(model_selector, "load_models_file", lambda: {})

    async def _fetch(config):
        return [_option(config.model, provider="ollama")]

    monkeypatch.setattr(model_selector, "_fetch_provider_models", _fetch)
    config = PhosonConfig(provider="ollama", model="llama3")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        options = await model_selector.list_available_models(config)
    assert [o.id for o in options] == ["llama3"]


# ── REPL integration ─────────────────────────────────────────────────────────


def _fake_stream(events):
    async def stream(path, config):
        for event in events:
            yield event

    return stream


def _make_repl_with_events(tmp_path, events):
    from unittest.mock import AsyncMock

    repl = _make_repl(tmp_path)
    repl._cw_resolver.resolve = AsyncMock(return_value=128_000)
    repl.engine.stream = _fake_stream(events)
    return repl


@pytest.mark.asyncio
async def test_repl_uses_models_json_context_window(tmp_path) -> None:
    from phoson_llm.schemas import Message
    from phoson_agent.models import (
        AgentDoneEvent,
        AgentRunResult,
        AgentStartEvent,
        AgentTokenEvent,
    )

    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentTokenEvent(content="a"),
        AgentDoneEvent(
            result=AgentRunResult(
                final_content="a",
                history=[
                    Message(role="user", content="q"),
                    Message(role="assistant", content="a"),
                ],
                input_messages=[Message(role="user", content="q")],
            )
        ),
    ]
    data = {"models": {"qwen3.8-27b": {"context_window": 262144}}}
    import phoson_cli.repl as repl_mod

    repl = _make_repl_with_events(tmp_path, events)
    repl.current_model = "openrouter/qwen3.8-27b"
    repl.config.provider = "openrouter"
    mock_resolve = repl._cw_resolver.resolve

    with patch.object(repl_mod, "load_models_file", return_value=data):
        await repl._run_agent("q")

    assert repl._context_window == 262144
    mock_resolve.assert_not_called()  # models.json override short-circuited


@pytest.mark.asyncio
async def test_repl_falls_back_to_engine_resolver_without_override(tmp_path) -> None:
    from phoson_llm.schemas import Message
    from phoson_agent.models import (
        AgentDoneEvent,
        AgentRunResult,
        AgentStartEvent,
        AgentTokenEvent,
    )

    events = [
        AgentStartEvent(model="m", message_count=1, max_iterations=50),
        AgentTokenEvent(content="a"),
        AgentDoneEvent(
            result=AgentRunResult(
                final_content="a",
                history=[
                    Message(role="user", content="q"),
                    Message(role="assistant", content="a"),
                ],
                input_messages=[Message(role="user", content="q")],
            )
        ),
    ]
    repl = _make_repl_with_events(tmp_path, events)
    repl.current_model = "some-model"
    repl.config.provider = "openrouter"

    import phoson_cli.repl as repl_mod

    with patch.object(repl_mod, "load_models_file", return_value={}):
        await repl._run_agent("q")

    assert repl._context_window == 128_000  # engine resolver value


def test_set_provider_uses_default_model(tmp_path) -> None:
    import phoson_cli.repl as repl_mod

    repl = _make_repl(tmp_path)
    data = {"providers": {"openrouter": {"default_model": "qwen3.8-27b"}}}
    with (
        patch.object(repl_mod, "load_models_file", return_value=data),
        patch.object(repl_mod, "build_chat", return_value=None),
    ):
        repl.set_provider("openrouter")
    assert repl.current_model == "qwen3.8-27b"
    assert repl.config.provider == "openrouter"


def test_set_provider_without_default_keeps_model(tmp_path) -> None:
    import phoson_cli.repl as repl_mod

    repl = _make_repl(tmp_path)
    with patch.object(repl_mod, "build_chat", return_value=None):
        repl.set_model("keep-me")
    with (
        patch.object(repl_mod, "load_models_file", return_value={}),
        patch.object(repl_mod, "build_chat", return_value=None),
    ):
        repl.set_provider("openrouter")
    assert repl.current_model == "keep-me"


# ── build_chat base_url wiring ───────────────────────────────────────────────


def test_build_chat_applies_models_json_base_url(tmp_path) -> None:
    import phoson_cli.config as config_mod

    models_path = tmp_path / "models.json"
    save_models_file(
        {"providers": {"groq": {"base_url": "https://proxy.example/v1"}}},
        models_path,
    )
    import phoson_cli.models as models_mod

    with patch.object(
        models_mod, "load_models_file", lambda: load_models_file(models_path)
    ):
        config = config_mod.PhosonConfig(provider="groq", groq_api_key="gsk_test")
        chat = config_mod.build_chat(config)
    assert chat._base_url == "https://proxy.example/v1"


def test_build_chat_without_base_url_uses_provider_default(tmp_path) -> None:
    import phoson_cli.config as config_mod
    import phoson_cli.models as models_mod

    with patch.object(models_mod, "load_models_file", return_value={}):
        config = config_mod.PhosonConfig(provider="groq", groq_api_key="gsk_test")
        chat = config_mod.build_chat(config)
    assert chat._base_url == "https://api.groq.com/openai/v1"
