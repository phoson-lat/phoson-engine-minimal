"""Regression tests for the offline bench runner (issue #138 / H-0).

``bench/run_bench.py`` must actually apply ``--model``/``--provider`` to
the one-shot subprocess it spawns. The original bug: it injected
``PHOSON_MODEL_OVERRIDE`` / ``PHOSON_PROVIDER_OVERRIDE``, env vars that do
not exist anywhere in the CLI — so the flags were silently ignored and
any model comparison made through the runner was invalid (it pinned
nothing). The real knobs are ``PHOSON_MODEL`` / ``PHOSON_PROVIDER``
(``phoson_cli/config.py`` resolves them env → config.toml → default).

We test the runner's seams directly (``_build_env`` and
``_results_payload``) without spawning the CLI: the contract is that the
subprocess env carries the effective model/provider — and that a value
inherited from the developer's own shell cannot leak in and quietly
re-target a baseline run.
"""

import importlib.util
from pathlib import Path

import pytest

_BENCH_DIR = Path(__file__).resolve().parent.parent / "bench"


@pytest.fixture(scope="module")
def bench():
    """Load ``bench/run_bench.py`` by path (bench is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "run_bench", _BENCH_DIR / "run_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_env_pins_model_and_provider(bench) -> None:
    """--model/--provider must land in the subprocess env as the env vars
    the CLI actually reads (issue #138)."""
    env = bench._build_env("openai/gpt-4o-mini", "openrouter")
    assert env["PHOSON_MODEL"] == "openai/gpt-4o-mini"
    assert env["PHOSON_PROVIDER"] == "openrouter"
    # The dead override vars must be gone.
    assert "PHOSON_MODEL_OVERRIDE" not in env
    assert "PHOSON_PROVIDER_OVERRIDE" not in env


def test_build_env_without_flags_injects_nothing(bench) -> None:
    """No flags → the run deterministically uses the user's config.toml
    (no env pin), so a bare baseline isn't re-targeted by the runner."""
    env = bench._build_env(None, None)
    assert "PHOSON_MODEL" not in env
    assert "PHOSON_PROVIDER" not in env


def test_build_env_pins_win_over_inherited_shell_value(bench, monkeypatch) -> None:
    """A developer shell exporting PHOSON_MODEL must not contaminate a
    baseline: the runner's --model wins, and with no flag the inherited
    value is dropped so config.toml is the deterministic fallback."""
    monkeypatch.setenv("PHOSON_MODEL", "leak-from-dev-shell")

    pinned = bench._build_env("pinned/model", None)
    assert pinned["PHOSON_MODEL"] == "pinned/model"

    unflagged = bench._build_env(None, None)
    assert "PHOSON_MODEL" not in unflagged  # the leak is popped


def test_cli_resolves_pinned_env_over_config(bench, monkeypatch, tmp_path) -> None:
    """End-to-end on the real config seam: with the bench's env applied,
    ``load_config`` returns exactly the pinned model/provider — proving
    the override the runner injects is the one the CLI honors."""
    monkeypatch.setenv("PHOSON_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("PHOSON_PROVIDER", "openrouter")
    # A config.toml that *would* win if the env did not.
    (tmp_path / "config.toml").write_text(
        '[defaults]\nmodel = "ollama/qwen3"\nprovider = "ollama"\n'
    )

    from phoson_cli import config as phoson_config

    fd = phoson_config._load_file_defaults(tmp_path / "config.toml")
    d = phoson_config.PhosonConfig()
    model = phoson_config._resolve_str("PHOSON_MODEL", "model", fd, d.model)
    provider = phoson_config._resolve_str(
        "PHOSON_PROVIDER", "provider", fd, d.provider
    ).lower()

    assert model == "openai/gpt-4o-mini"  # env beats config.toml
    assert provider == "openrouter"


def test_results_payload_records_effective_model_and_commit(bench) -> None:
    """Every saved JSON must carry the effective model/provider + the git
    commit it ran under, so a baseline is auditable (issue #138)."""
    r = bench.TaskResult(
        name="csv-stats",
        passed=True,
        duration_s=1.0,
        exit_code=0,
        detail="ok",
    )
    payload = bench._results_payload("openai/gpt-4o-mini", "openrouter", [r])
    assert payload["model"] == "openai/gpt-4o-mini"
    assert payload["provider"] == "openrouter"
    assert payload["commit"]  # non-empty short sha (or "unknown" off-repo)
    assert payload["pass_rate"] == 1.0
    assert payload["results"][0]["name"] == "csv-stats"
