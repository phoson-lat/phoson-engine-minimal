"""Startup regression guards for the CLI's import graph.

The cold-start win (``import phoson_cli.__main__`` ~1.3 s / 77 MB → ~0.35 s /
46 MB, v0.43.0) comes entirely from *not* importing the ``openai`` /
``anthropic`` SDKs on a plain CLI import. The deterministic guard is module
presence, checked in a clean subprocess (the main pytest process has already
imported those SDKs, so an in-process check would be a false positive).

A loose timing smoke test backs it up; the strict gate is the presence check,
which cannot flake.
"""

import sys
import time
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Cloud SDKs a plain CLI import must not drag in.
VENDOR_SDKS = ("openai", "anthropic")


def _run_in_clean_subprocess(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def _loaded_sdks(code: str) -> set[str]:
    """Run ``code`` in a fresh interpreter; return the vendor SDKs it loaded."""
    probe = (
        "import sys\n"
        f"{code}\n"
        f"print(','.join(m for m in sys.modules if m in {VENDOR_SDKS!r}))\n"
    )
    result = _run_in_clean_subprocess(probe)
    assert result.returncode == 0, result.stderr
    return {m for m in result.stdout.strip().split(",") if m}


class TestNoEagerProviderSdks:
    def test_cli_entrypoint_import_loads_no_provider_sdk(self) -> None:
        assert _loaded_sdks("import phoson_cli.__main__") == set()

    def test_cli_config_import_loads_no_provider_sdk(self) -> None:
        assert _loaded_sdks("import phoson_cli.config") == set()

    def test_building_a_local_provider_loads_no_cloud_sdk(self) -> None:
        # A local-runtime user (ollama/vLLM/LM Studio) never needs the cloud
        # SDKs: building the adapter must import only the provider's module.
        code = (
            "from phoson_cli.config import PhosonConfig, build_chat\n"
            "build_chat(PhosonConfig(provider='ollama', model='llama3'))\n"
        )
        assert _loaded_sdks(code) == set()

    def test_building_openai_loads_only_openai(self) -> None:
        code = (
            "from phoson_cli.config import PhosonConfig, build_chat\n"
            "build_chat(PhosonConfig(provider='openai', model='gpt-4o',"
            " openai_api_key='x'))\n"
        )
        assert _loaded_sdks(code) == {"openai"}


def test_cli_import_stays_under_a_generous_ceiling() -> None:
    """Smoke: a cold CLI import must not regress to the eager-SDK era.

    Best-of-three to absorb a loaded runner; the ceiling is ~10x the expected
    ~0.35 s, so it only catches catastrophic regressions. The strict,
    non-flaky gate is :class:`TestNoEagerProviderSdks`.
    """
    code = "import phoson_cli.__main__"
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        result = _run_in_clean_subprocess(code)
        timings.append(time.perf_counter() - start)
        assert result.returncode == 0, result.stderr
    assert min(timings) < 4.0, f"cold CLI import took {min(timings):.2f}s"
