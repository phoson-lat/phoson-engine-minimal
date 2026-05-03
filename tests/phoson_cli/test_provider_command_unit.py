from types import SimpleNamespace

import pytest

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
            openrouter_api_key="sk-or-test",
            openai_api_key="sk-openai-test",
            anthropic_api_key=None,
            ollama_base_url=None,
        )
        self.renderer = DummyRenderer()
        self.engine = SimpleNamespace(context=SimpleNamespace(extra={}))
        self.provider_calls: list[str] = []
        self.model_calls: list[str] = []

    def set_provider(self, provider: str) -> None:
        self.provider_calls.append(provider)
        self.config.provider = provider

    def set_model(self, model: str) -> None:
        self.model_calls.append(model)
        self.current_model = model
        self.config.model = model


@pytest.mark.asyncio
async def test_provider_command_lists_available_providers() -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    result = await handler.handle(Command(name="/provider", args="list"))

    assert result is True
    assert repl.renderer.infos[0] == "Available providers:"
    assert "* openrouter" in repl.renderer.infos[1]
    assert "  openai" in repl.renderer.infos[2]


@pytest.mark.asyncio
async def test_provider_command_opens_picker_switches_provider_and_model(
    monkeypatch,
) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)
    saved_configs: list[object] = []

    async def fake_pick_provider(providers, current_provider):
        return SimpleNamespace(provider="openai", cancelled=False)

    async def fake_list_available_models(config):
        return [SimpleNamespace(id="gpt-4.1-mini", provider="openai")]

    async def fake_pick_model(models, current_model):
        return SimpleNamespace(model_id="gpt-4.1-mini", cancelled=False)

    monkeypatch.setattr("phoson_cli.commands.pick_provider", fake_pick_provider)
    monkeypatch.setattr(
        "phoson_cli.commands.list_available_models",
        fake_list_available_models,
    )
    monkeypatch.setattr("phoson_cli.commands.pick_model", fake_pick_model)
    monkeypatch.setattr(
        "phoson_cli.commands.save_config", lambda config: saved_configs.append(config)
    )

    result = await handler.handle(Command(name="/provider", args=""))

    assert result is True
    assert repl.provider_calls == ["openai"]
    assert repl.model_calls == ["gpt-4.1-mini"]
    assert saved_configs
    assert (
        repl.renderer.infos[-1]
        == "Provider → openai  ·  Model → gpt-4.1-mini  ·  saved"
    )


@pytest.mark.asyncio
async def test_provider_command_switches_provider_directly(monkeypatch) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)
    saved_configs: list[object] = []

    async def fake_list_available_models(config):
        return [SimpleNamespace(id="gpt-4.1-mini", provider="openai")]

    async def fake_pick_model(models, current_model):
        return SimpleNamespace(model_id="gpt-4.1-mini", cancelled=False)

    monkeypatch.setattr(
        "phoson_cli.commands.list_available_models",
        fake_list_available_models,
    )
    monkeypatch.setattr("phoson_cli.commands.pick_model", fake_pick_model)
    monkeypatch.setattr(
        "phoson_cli.commands.save_config", lambda config: saved_configs.append(config)
    )

    result = await handler.handle(Command(name="/provider", args="openai"))

    assert result is True
    assert repl.provider_calls == ["openai"]
    assert repl.model_calls == ["gpt-4.1-mini"]
    assert saved_configs
    assert (
        repl.renderer.infos[-1]
        == "Provider → openai  ·  Model → gpt-4.1-mini  ·  saved"
    )


@pytest.mark.asyncio
async def test_provider_command_rejects_unconfigured_provider() -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)

    result = await handler.handle(Command(name="/provider", args="anthropic"))

    assert result is True
    assert repl.renderer.errors[-1] == "Provider not configured: anthropic"
