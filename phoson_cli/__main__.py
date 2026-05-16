"""Entry point for the Phoson CLI application."""

import sys
import shutil
import asyncio
import subprocess
from pathlib import Path

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import load_config
from phoson_cli.installer import run_install_wizard


def _has_configured_provider(config) -> bool:
    """Return whether the loaded config can build a provider without setup."""
    if config.provider.lower() in {"ollama", "bedrock", "aws", "vllm", "lmstudio"}:
        return True
    return any(
        (
            config.openrouter_api_key,
            config.openai_api_key,
            config.anthropic_api_key,
            config.github_token,
            config.nvidia_api_key,
            config.xai_api_key,
            config.groq_api_key,
            config.deepseek_api_key,
            config.together_api_key,
            config.perplexity_api_key,
            config.azure_openai_api_key,
            config.gemini_api_key,
            config.mistral_api_key,
            config.fireworks_api_key,
            config.cohere_api_key,
            config.vllm_base_url,
            config.vllm_api_key,
            config.lmstudio_base_url,
        )
    )


def self_update() -> None:
    """Upgrade phoson-cli to the latest version via uv."""
    print("Updating phoson-cli...")
    result = subprocess.run(
        ["uv", "tool", "upgrade", "phoson-engine-minimal"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        print("Update complete!")
    else:
        print(f"Update failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def uninstall() -> None:
    """Remove phoson-cli and optionally config."""
    print("Uninstalling phoson-cli...")

    result = subprocess.run(
        ["uv", "tool", "uninstall", "phoson-engine-minimal"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        print("Package uninstalled.")
    else:
        print(f"Failed to uninstall package: {result.stderr}", file=sys.stderr)

    config_dir = Path.home() / ".phoson"
    if config_dir.exists():
        response = input("Remove ~/.phoson config directory? [y/N] ")
        if response.lower() in {"y", "yes"}:
            shutil.rmtree(config_dir)
            print("Config directory removed.")


def main() -> None:
    """Run the Phoson CLI REPL or setup wizard."""
    args = sys.argv[1:]

    if "--self-update" in args:
        self_update()
        return

    if "--uninstall" in args:
        uninstall()
        return

    if any(arg in {"--install", "--setup"} for arg in args):
        config = load_config()
        asyncio.run(run_install_wizard(config))
        return

    config = load_config()

    config_path = Path.home() / ".phoson" / "config.toml"

    if not config_path.exists() and not _has_configured_provider(config):
        print("No API keys configured. Running setup wizard...")
        asyncio.run(run_install_wizard(config))
        # Reload config after setup
        config = load_config()

    repl = PhosonRepl(config)
    asyncio.run(repl.run())


if __name__ == "__main__":
    main()
