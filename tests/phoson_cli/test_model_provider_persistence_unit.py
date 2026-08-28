"""I-89 — ``/model`` must persist the provider alongside the model.

Covers:
- the pure ``model_provider_for`` helper (prefix inference, router
  exception, picker option authority, aliases);
- ``SessionController.set_model(model, provider=...)``;
- the ``/model`` command persisting a consistent ``(provider, model)``
  pair, refusing to save a pair whose provider has no credentials, and
  leaving the provider untouched when the model stays within a router.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoson_cli.theme import NO_COLOR
from phoson_cli.config import PhosonConfig, load_config, save_config
from phoson_cli.models import model_provider_for, normalize_provider
from phoson_cli.commands import Command, CommandHandler

# ── model_provider_for (pure helper) ─────────────────────────────────────────


def test_model_provider_for_prefers_option_provider() -> None:
    assert (
        model_provider_for("openai/gpt-4o", "anthropic", option_provider="openai")
        == "openai"
    )
    # The picker's provider field wins even over the id prefix.
    assert (
        model_provider_for("openai/gpt-4o", "anthropic", option_provider="groq")
        == "groq"
    )


def test_model_provider_for_custom_option_falls_back_to_prefix() -> None:
    # "custom" is the fallback tag for the current-model entry — it must
    # not be treated as a real provider.
    assert (
        model_provider_for("openai/gpt-4o", "anthropic", option_provider="custom")
        == "openai"
    )
    assert model_provider_for("gpt-4o", "anthropic", option_provider="custom") is None


def test_model_provider_for_uses_vendor_prefix() -> None:
    assert model_provider_for("openai/gpt-4o", "anthropic") == "openai"
    assert model_provider_for("anthropic/claude-sonnet-4-6", "openai") == "anthropic"
    # Same provider → no switch.
    assert model_provider_for("openai/gpt-4o", "openai") is None


def test_model_provider_for_router_prefix_does_not_switch() -> None:
    # Routers serve other vendors' ids (openai/gpt-4o via OpenRouter).
    assert model_provider_for("openai/gpt-4o", "openrouter") is None
    assert model_provider_for("anthropic/claude-sonnet-4-6", "github") is None


def test_model_provider_for_unknown_prefix_never_switches() -> None:
    # Local/unknown vendor prefixes must never trigger a provider switch.
    assert model_provider_for("qwen/qwen3.6-plus", "anthropic") is None
    assert model_provider_for("my-local-model", "ollama") is None
    assert model_provider_for("gpt-4o", "openai") is None


def test_model_provider_for_normalizes_aliases() -> None:
    assert model_provider_for("google/gemini-2.5-flash", "openai") == "gemini"
    assert model_provider_for("openai/gpt-4o", "Google") == "openai"
    assert normalize_provider("AWS") == "bedrock"
    assert normalize_provider("Grok") == "xai"


# ── SessionController.set_model(model, provider=...) ─────────────────────────


class _Sink:
    on_subagent_progress = None

    def set_session(self, session_id: str) -> None:
        self.session_id = session_id


async def test_controller_set_model_switches_provider(tmp_path) -> None:
    from phoson_cli.controller import SessionController

    config = PhosonConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-openai-test",
        sessions_dir=tmp_path,
    )
    fake_engine = SimpleNamespace(context=SimpleNamespace(extra={}), tools=[])
    with (
        patch(
            "phoson_cli.controller.build_chat",
            return_value=MagicMock(aclose=AsyncMock()),
        ),
        patch("phoson_cli.controller.AgentEngine", return_value=fake_engine),
        patch(
            "phoson_cli.controller.SessionController._refresh_context_window",
            new=AsyncMock(),
        ),
    ):
        controller = SessionController(config, _Sink())
        await controller.set_model("gpt-4o", provider="openai")

        assert controller.config.provider == "openai"
        assert controller.config.model == "gpt-4o"
        assert controller.current_model == "gpt-4o"

        # Same provider (alias included) → no provider mutation.
        await controller.set_model("gpt-4.1-mini", provider="OpenAI")
        assert controller.config.provider == "openai"


# ── /model command: persistence ──────────────────────────────────────────────


class _DummyRenderer:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)


class _DummyRepl:
    """Command-handler test double that records set_model calls."""

    def __init__(self, config: PhosonConfig) -> None:
        self.config = config
        self.current_model = config.model
        self.subagent_model = config.subagent_model or config.model
        self.theme = NO_COLOR
        self.renderer = _DummyRenderer()
        self.engine = SimpleNamespace(context=SimpleNamespace(extra={}))
        self.model_calls: list[tuple[str, str | None]] = []

    async def set_model(self, model: str, provider: str | None = None) -> None:
        self.model_calls.append((model, provider))
        if provider is not None:
            self.config.provider = provider
        self.current_model = model
        self.config.model = model


def _handler(repl: _DummyRepl) -> CommandHandler:
    return CommandHandler(repl)


def _mock_listings(monkeypatch, **provider_to_ids: list[str]) -> None:
    """Patch ``list_models_for_providers`` with a fixed id → provider map.

    Used by the explicit-``/model <id>`` tests below (I-113 follow-up):
    since the command now always resolves the id's *real* provider via a
    live listing lookup (never the ambiguous vendor/-prefix heuristic
    alone — see the regression this guards against), tests must control
    that lookup instead of hitting the network.
    """
    from phoson_cli.model_selector import ProviderListing

    async def fake_list_models_for_providers(cfg, providers):
        return [
            ProviderListing(
                provider=provider,
                options=[SimpleNamespace(id=i, provider=provider) for i in ids],
            )
            for provider, ids in provider_to_ids.items()
        ]

    monkeypatch.setattr(
        "phoson_cli.commands.list_models_for_providers",
        fake_list_models_for_providers,
    )


@pytest.mark.asyncio
async def test_model_command_persists_provider_and_model(tmp_path, monkeypatch) -> None:
    """Explicit /model <id> of another provider switches + saves both."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-openai-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)
    _mock_listings(
        monkeypatch,
        anthropic=["claude-sonnet-4-6"],
        openai=["openai/gpt-4o"],
    )

    result = await _handler(repl).handle(Command(name="/model", args="openai/gpt-4o"))

    assert result is True
    assert repl.model_calls == [("openai/gpt-4o", "openai")]
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openai"' in text
    assert 'model = "openai/gpt-4o"' in text

    # Acceptance criterion: a restart reproduces the same (provider, model).
    reloaded = load_config()
    assert reloaded.provider == "openai"
    assert reloaded.model == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_model_command_refuses_unconfigured_provider(
    tmp_path, monkeypatch
) -> None:
    """A model whose provider has no credentials must not be saved."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)
    # openai isn't configured (no credentials) — its listing can't be
    # queried live either, so this only proves the id resolves to a
    # provider the credential check then rejects.
    _mock_listings(
        monkeypatch,
        anthropic=["claude-sonnet-4-6"],
        openai=["openai/gpt-4o"],
    )

    result = await _handler(repl).handle(Command(name="/model", args="openai/gpt-4o"))

    assert result is True
    assert repl.model_calls == []  # runtime untouched
    assert any("no credentials" in e for e in repl.renderer.errors)
    assert not (tmp_path / ".phoson" / "config.toml").exists()


@pytest.mark.asyncio
async def test_model_command_router_prefix_keeps_provider(
    tmp_path, monkeypatch
) -> None:
    """A vendor-prefixed id that OpenRouter itself lists (not OpenAI, not
    a separately-configured OpenAI credential) keeps OpenRouter active —
    the live listing lookup, not the vendor/ prefix, drives the switch."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="openrouter",
        model="qwen/qwen3.6-plus",
        openrouter_api_key="sk-or-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)
    _mock_listings(
        monkeypatch,
        openrouter=["qwen/qwen3.6-plus", "openai/gpt-4o"],
    )

    result = await _handler(repl).handle(Command(name="/model", args="openai/gpt-4o"))

    assert result is True
    assert repl.model_calls == [("openai/gpt-4o", None)]
    assert config.provider == "openrouter"
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openrouter"' in text
    assert 'model = "openai/gpt-4o"' in text


@pytest.mark.asyncio
async def test_model_command_router_vendor_id_not_a_real_provider_match(
    tmp_path, monkeypatch
) -> None:
    """Regression: a router-served id whose vendor/ prefix *looks* like a
    real, separately-configured provider name must NOT be attributed to
    that provider — it must resolve to whichever provider's live listing
    actually contains it (here: openrouter, not a real "anthropic" API).

    This is the exact bug reported against I-113: with vllm active,
    picking "anthropic/claude-opus-5" (an OpenRouter catalog entry, not
    a real Anthropic-API model) incorrectly rejected the switch with
    "belongs to provider anthropic, which has no credentials configured"
    because the vendor/ prefix heuristic assumed "anthropic" was the
    real backend instead of asking who actually lists the id.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="vllm",
        model="Qwen3.8-27B-FP8",
        vllm_base_url="http://localhost:8383/v1",
        openrouter_api_key="sk-or-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)
    _mock_listings(
        monkeypatch,
        vllm=["Qwen3.8-27B-FP8"],
        openrouter=["anthropic/claude-opus-5"],
    )

    result = await _handler(repl).handle(
        Command(name="/model", args="anthropic/claude-opus-5")
    )

    assert result is True
    assert not repl.renderer.errors, repl.renderer.errors
    assert repl.model_calls == [("anthropic/claude-opus-5", "openrouter")]
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openrouter"' in text
    assert 'model = "anthropic/claude-opus-5"' in text


@pytest.mark.asyncio
async def test_model_command_picker_option_provider_is_authoritative(
    tmp_path, monkeypatch
) -> None:
    """Picker selection: the option's provider field drives the switch."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        groq_api_key="gsk-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)

    from phoson_cli.model_selector import ProviderListing

    async def fake_list_models_for_providers(cfg, providers):
        return [
            ProviderListing(
                provider="anthropic",
                options=[
                    SimpleNamespace(id="claude-sonnet-4-6", provider="anthropic"),
                ],
            ),
            ProviderListing(
                provider="groq",
                options=[
                    SimpleNamespace(id="llama-3.3-70b", provider="groq"),
                ],
            ),
        ]

    async def fake_pick_model(models, current_model, page_size=12, theme=None, **kw):
        return SimpleNamespace(
            model_id="llama-3.3-70b", provider="groq", cancelled=False
        )

    monkeypatch.setattr(
        "phoson_cli.commands.list_models_for_providers",
        fake_list_models_for_providers,
    )
    monkeypatch.setattr("phoson_cli.commands.pick_model", fake_pick_model)

    result = await _handler(repl).handle(Command(name="/model", args=""))

    assert result is True
    assert repl.model_calls == [("llama-3.3-70b", "groq")]
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "groq"' in text
    assert 'model = "llama-3.3-70b"' in text


@pytest.mark.asyncio
async def test_model_command_same_provider_saves_model_only(
    tmp_path, monkeypatch
) -> None:
    """No provider change → provider line left exactly as it was."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="openai",
        model="gpt-4o",
        openai_api_key="sk-openai-test",
        sessions_dir=tmp_path / "sessions",
    )
    # Pre-existing file with a provider line the save must not rewrite.
    save_config(config)
    repl = _DummyRepl(config)

    result = await _handler(repl).handle(Command(name="/model", args="gpt-4.1-mini"))

    assert result is True
    assert repl.model_calls == [("gpt-4.1-mini", None)]
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openai"' in text
    assert 'model = "gpt-4.1-mini"' in text


@pytest.mark.asyncio
async def test_model_command_env_provider_becomes_self_containing_file(
    tmp_path, monkeypatch
) -> None:
    """Issue point 2: provider from env → the saved file must carry it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PHOSON_PROVIDER", "anthropic")
    config = PhosonConfig(
        provider="anthropic",  # resolved from env at load time
        model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-openai-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)
    _mock_listings(
        monkeypatch,
        anthropic=["claude-sonnet-4-6"],
        openai=["openai/gpt-4o"],
    )

    await _handler(repl).handle(Command(name="/model", args="openai/gpt-4o"))

    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openai"' in text
    assert 'model = "openai/gpt-4o"' in text

    # Next launch WITHOUT the env var must reproduce the selection.
    monkeypatch.delenv("PHOSON_PROVIDER")
    reloaded = load_config()
    assert reloaded.provider == "openai"
    assert reloaded.model == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_model_command_explicit_unknown_prefix_resolves_via_listing(
    tmp_path, monkeypatch
) -> None:
    """I-113 follow-up regression: an explicit ``/model <id>`` whose
    vendor/ prefix isn't itself a known provider (e.g. an OpenRouter
    catalog entry like "qwen/qwen3.8-27b" — "qwen" isn't a backend
    phoson talks to directly) must still switch the *actual* provider by
    looking the id up across every configured provider's live listing,
    instead of silently keeping the active one (here: vllm) while only
    the "model" string gets saved.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="vllm",
        model="Qwen3.8-27B-FP8",
        vllm_base_url="http://localhost:8383/v1",
        openrouter_api_key="sk-or-test",
        sessions_dir=tmp_path / "sessions",
    )
    repl = _DummyRepl(config)

    from phoson_cli.model_selector import ProviderListing

    async def fake_list_models_for_providers(cfg, providers):
        return [
            ProviderListing(
                provider="vllm",
                options=[SimpleNamespace(id="Qwen3.8-27B-FP8", provider="vllm")],
            ),
            ProviderListing(
                provider="openrouter",
                options=[SimpleNamespace(id="qwen/qwen3.8-27b", provider="openrouter")],
            ),
        ]

    monkeypatch.setattr(
        "phoson_cli.commands.list_models_for_providers",
        fake_list_models_for_providers,
    )

    result = await _handler(repl).handle(
        Command(name="/model", args="qwen/qwen3.8-27b")
    )

    assert result is True
    assert repl.model_calls == [("qwen/qwen3.8-27b", "openrouter")]
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "openrouter"' in text
    assert 'model = "qwen/qwen3.8-27b"' in text


@pytest.mark.asyncio
async def test_model_command_explicit_same_provider_skips_listing_lookup(
    monkeypatch,
) -> None:
    """No ``/`` in the id, or a prefix that already resolves for free
    (known provider / router-kept), must NOT pay for the extra
    multi-provider fetch."""
    config = PhosonConfig(
        provider="openai",
        model="gpt-4o",
        openai_api_key="sk-openai-test",
    )
    repl = _DummyRepl(config)

    async def fail_if_called(cfg, providers):
        raise AssertionError("list_models_for_providers must not be called")

    monkeypatch.setattr("phoson_cli.commands.list_models_for_providers", fail_if_called)
    monkeypatch.setattr("phoson_cli.commands.save_config", lambda *a, **k: None)

    result = await _handler(repl).handle(Command(name="/model", args="gpt-4.1-mini"))

    assert result is True
    assert repl.model_calls == [("gpt-4.1-mini", None)]


@pytest.mark.asyncio
async def test_provider_command_refreshes_enabled_providers(
    tmp_path, monkeypatch
) -> None:
    """Narrow /provider save keeps enabled_providers consistent (issue pt 3)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = PhosonConfig(
        provider="openai",
        model="gpt-4o",
        openai_api_key="sk-openai-test",
        anthropic_api_key="sk-ant-test",
        sessions_dir=tmp_path / "sessions",
    )
    save_config(config)
    repl = _DummyRepl(config)

    async def fake_set_provider(provider: str) -> None:
        config.provider = provider

    repl.set_provider = fake_set_provider  # type: ignore[method-assign]

    result = await _handler(repl).handle(Command(name="/provider", args="anthropic"))

    assert result is True
    text = (tmp_path / ".phoson" / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "anthropic"' in text
    # Both credentials present → both listed, in the file, in sync.
    assert 'enabled_providers = "openai,anthropic"' in text
