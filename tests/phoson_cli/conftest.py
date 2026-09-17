"""Keep CLI state and provider configuration local to each test."""

import os

import pytest


@pytest.fixture
def isolated_cli_home(monkeypatch, tmp_path):
    # Commands and keybindings can save config even when sessions_dir is set.
    # Opt in both readers and writers, so neither relies on the caller's config.
    # Not autouse: PhosonConfig path defaults are currently bound at import time.
    home = tmp_path / "cli-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name, directory in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(name, str(home / directory))

    # Tests that exercise overrides must opt in, not inherit developer keys,
    # endpoints or PHOSON_* settings. Do not set TERM here: frontend-selection
    # tests need to declare their own terminal capabilities.
    for name in tuple(os.environ):
        if (
            name.startswith(("PHOSON_", "AWS_", "AZURE_OPENAI_"))
            or name.endswith("_API_KEY")
            or name
            in {
                "GITHUB_TOKEN",
                "OLLAMA_BASE_URL",
                "OMNIROUTE_BASE_URL",
                "VLLM_BASE_URL",
                "LMSTUDIO_BASE_URL",
            }
        ):
            monkeypatch.delenv(name)
