"""GitHub Models was retired by GitHub (2026-07-30): old configs still load,
using it explains why, and nothing offers or auto-enables it any more."""

import pytest

from phoson_cli.config import (
    RETIRED_PROVIDERS,
    PhosonConfig,
    build_chat,
    canonicalize_enabled_providers,
)


def test_github_is_retired() -> None:
    assert "github" in RETIRED_PROVIDERS


def test_existing_config_with_github_still_loads() -> None:
    assert canonicalize_enabled_providers(
        ["github", "openai"], active_provider="github"
    ) == ["github", "openai"]


def test_building_a_github_chat_explains_the_retirement() -> None:
    with pytest.raises(ValueError, match="retired by GitHub on 2026-07-30"):
        build_chat(PhosonConfig(provider="github"))


def test_github_is_not_offered_or_detected() -> None:
    import phoson_llm
    from phoson_cli.labels import provider_label
    from phoson_cli.models import KNOWN_PROVIDERS, ROUTER_PROVIDERS
    from phoson_llm.factory import build_chat as factory_build

    assert "github" not in KNOWN_PROVIDERS
    assert "github" not in ROUTER_PROVIDERS
    assert not hasattr(phoson_llm, "GitHubModelsChat")
    assert "retired" in provider_label("github")
    with pytest.raises(Exception):
        factory_build("github")


def test_github_token_env_no_longer_enables_a_provider(monkeypatch) -> None:
    from phoson_cli.config import load_config

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    config = load_config()
    assert not hasattr(config, "github_token")
