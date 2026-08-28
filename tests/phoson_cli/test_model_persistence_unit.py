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
        )
        self.theme = NO_COLOR
        self.renderer = DummyRenderer()
        self.engine = SimpleNamespace(context=SimpleNamespace(extra={}))
        self.model_calls: list[str] = []

    async def set_model(self, model: str) -> None:
        self.model_calls.append(model)
        self.current_model = model
        self.config.model = model


@pytest.mark.asyncio
async def test_model_command_persists_selected_model(monkeypatch) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)
    saved_configs: list[object] = []

    from phoson_cli.model_selector import ProviderListing

    async def fake_list_models_for_providers(config, providers):
        return [
            ProviderListing(
                provider="openrouter",
                options=[
                    SimpleNamespace(id="google/gemini-2.5-flash", provider="openrouter")
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
    monkeypatch.setattr(
        "phoson_cli.commands.save_config",
        lambda config, **kwargs: saved_configs.append(config),
    )

    result = await handler.handle(Command(name="/model", args=""))

    assert result is True
    assert repl.model_calls == ["google/gemini-2.5-flash"]
    assert saved_configs


@pytest.mark.asyncio
async def test_subagent_model_command_persists_selected_model(monkeypatch) -> None:
    repl = DummyRepl()
    handler = CommandHandler(repl)
    saved_configs: list[object] = []

    from phoson_cli.model_selector import ProviderListing

    async def fake_list_models_for_providers(config, providers):
        return [
            ProviderListing(
                provider="openrouter",
                options=[
                    SimpleNamespace(
                        id="anthropic/claude-3.5-haiku", provider="openrouter"
                    )
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
    monkeypatch.setattr(
        "phoson_cli.commands.save_config",
        lambda config, **kwargs: saved_configs.append(config),
    )

    result = await handler.handle(Command(name="/subagent-model", args=""))

    assert result is True
    assert repl.config.subagent_model == "anthropic/claude-3.5-haiku"
    assert repl.engine.context.extra["default_model"] == "anthropic/claude-3.5-haiku"
    assert saved_configs
