from types import SimpleNamespace

import pytest

from phoson_cli.theme import NO_COLOR
from phoson_cli.commands import Command, CommandHandler


class DummyRenderer:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)


class DummyRepl:
    def __init__(self) -> None:
        self.current_model = "openai/gpt-4.1-mini"
        self.subagent_model = "openai/gpt-4.1-mini"
        self.config = SimpleNamespace(
            provider="openrouter",
            model=self.current_model,
            subagent_model=self.subagent_model,
            openrouter_api_key="sk-or-test",
            openai_api_key=None,
            anthropic_api_key=None,
            ollama_base_url=None,
            sessions_dir="~/.phoson/sessions",
            max_iterations=50,
            safe_mode=False,
            enable_mcp=False,
            mcp_config_file="~/.phoson/mcps.json",
        )
        self.theme = NO_COLOR
        self.renderer = DummyRenderer()
        self.set_model_calls: list[str] = []
        self.engine = SimpleNamespace(context=SimpleNamespace(extra={}))

    async def set_model(self, model: str) -> None:
        self.set_model_calls.append(model)
        self.current_model = model
        self.config.model = model


@pytest.mark.asyncio
async def test_model_command_lists_available_models(monkeypatch) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    from phoson_cli.model_selector import ProviderListing

    async def fake_list_models_for_providers(config, providers):
        return [
            ProviderListing(
                provider="openrouter",
                options=[
                    SimpleNamespace(id="openai/gpt-4.1-mini", provider="openrouter"),
                    SimpleNamespace(
                        id="google/gemini-2.5-flash", provider="openrouter"
                    ),
                ],
            )
        ]

    monkeypatch.setattr(
        "phoson_cli.commands.list_models_for_providers",
        fake_list_models_for_providers,
    )

    result = await handler.handle(Command(name="/model", args="list"))

    assert result is True
    assert repl.renderer.infos[0] == "Available models:"
    assert "openrouter" in repl.renderer.infos[1]
    assert "* openai/gpt-4.1-mini" in repl.renderer.infos[2]
    assert "google/gemini-2.5-flash" in repl.renderer.infos[3]


@pytest.mark.asyncio
async def test_model_command_opens_picker_and_switches_model(monkeypatch) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    from phoson_cli.model_selector import ProviderListing

    async def fake_list_models_for_providers(config, providers):
        return [
            ProviderListing(
                provider="openrouter",
                options=[
                    SimpleNamespace(id="openai/gpt-4.1-mini", provider="openrouter"),
                ],
            )
        ]

    async def fake_pick_model(models, current_model, page_size=12, theme=None, **kw):
        return SimpleNamespace(
            model_id="google/gemini-2.5-flash",
            provider="openrouter",
            cancelled=False,
        )

    monkeypatch.setattr(
        "phoson_cli.commands.list_models_for_providers",
        fake_list_models_for_providers,
    )
    monkeypatch.setattr("phoson_cli.commands.pick_model", fake_pick_model)

    result = await handler.handle(Command(name="/model", args=""))

    assert result is True
    assert repl.set_model_calls == ["google/gemini-2.5-flash"]
    assert repl.renderer.infos[-1] == "Model → google/gemini-2.5-flash  ·  saved"


@pytest.mark.asyncio
async def test_model_command_switches_model_directly(monkeypatch) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    # I-113 follow-up: an explicit vendor/-prefixed /model now always
    # confirms the provider via a live listing lookup (the vendor/
    # prefix alone is ambiguous — see the router-vendor-id regression in
    # test_model_provider_persistence_unit.py) — mock it so this stays a
    # fast, deterministic unit test instead of hitting the network.
    async def fake_list_models_for_providers(config, providers):
        return [
            SimpleNamespace(
                provider="openrouter",
                available=True,
                options=[
                    SimpleNamespace(id="google/gemini-2.5-flash", provider="openrouter")
                ],
            )
        ]

    monkeypatch.setattr(
        "phoson_cli.commands.list_models_for_providers",
        fake_list_models_for_providers,
    )

    result = await handler.handle(
        Command(name="/model", args="google/gemini-2.5-flash")
    )

    assert result is True
    assert repl.set_model_calls == ["google/gemini-2.5-flash"]
    assert repl.renderer.infos[-1] == "Model → google/gemini-2.5-flash  ·  saved"


@pytest.mark.asyncio
async def test_subagent_model_command_opens_picker_and_switches_model(
    monkeypatch,
) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    from phoson_cli.model_selector import ProviderListing

    async def fake_list_models_for_providers(config, providers):
        return [
            ProviderListing(
                provider="openrouter",
                options=[
                    SimpleNamespace(id="openai/gpt-4.1-mini", provider="openrouter")
                ],
            )
        ]

    async def fake_pick_model(models, current_model, page_size=12, theme=None, **kw):
        return SimpleNamespace(
            model_id="anthropic/claude-3.5-haiku",
            provider="openrouter",
            cancelled=False,
        )

    monkeypatch.setattr(
        "phoson_cli.commands.list_models_for_providers",
        fake_list_models_for_providers,
    )
    monkeypatch.setattr("phoson_cli.commands.pick_model", fake_pick_model)

    result = await handler.handle(Command(name="/subagent-model", args=""))

    assert result is True
    assert repl.subagent_model == "anthropic/claude-3.5-haiku"
    assert repl.config.subagent_model == "anthropic/claude-3.5-haiku"
    assert repl.engine.context.extra["default_model"] == "anthropic/claude-3.5-haiku"
    assert (
        repl.renderer.infos[-1]
        == "Sub-agent model → anthropic/claude-3.5-haiku  ·  saved"
    )


@pytest.mark.asyncio
async def test_subagent_model_command_lists_available_models(monkeypatch) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    async def fake_list_available_models(config):
        return [
            SimpleNamespace(id="openai/gpt-4.1-mini", provider="openrouter"),
            SimpleNamespace(id="anthropic/claude-3.5-haiku", provider="openrouter"),
        ]

    monkeypatch.setattr(
        "phoson_cli.commands.list_available_models",
        fake_list_available_models,
    )

    result = await handler.handle(Command(name="/subagent-model", args="list"))

    assert result is True
    assert repl.renderer.infos[0] == "Available sub-agent models:"
    assert "* openai/gpt-4.1-mini [openrouter]" in repl.renderer.infos[1]
    assert "  anthropic/claude-3.5-haiku [openrouter]" in repl.renderer.infos[2]
