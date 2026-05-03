"""Entry point for the Phoson CLI application."""

import sys
import asyncio
import subprocess
from pathlib import Path

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import load_config
from phoson_cli.installer import run_install_wizard


async def self_update() -> None:
    """Upgrade phoson-cli to the latest version via uv."""
    print("Updating phoson-cli...")
    result = subprocess.run(
        ["uv", "tool", "upgrade", "phoson-engine-minimal"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("Update complete!")
    else:
        print(f"Update failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)


async def uninstall() -> None:
    """Remove phoson-cli and optionally config."""
    import shutil

    print("Uninstalling phoson-cli...")

    result = subprocess.run(
        ["uv", "tool", "uninstall", "phoson-engine-minimal"],
        capture_output=True,
        text=True,
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
        asyncio.run(self_update())
        return

    if "--uninstall" in args:
        asyncio.run(uninstall())
        return

    if any(arg in {"--install", "--setup"} for arg in args):
        config = load_config()
        asyncio.run(run_install_wizard(config))
        return

    config = load_config()

    # Check if API keys are configured - if not, run setup wizard
    has_api_key = (
        config.openrouter_api_key
        or config.openai_api_key
        or config.anthropic_api_key
        or config.ollama_base_url
    )

    if not has_api_key:
        print("No API keys configured. Running setup wizard...")
        asyncio.run(run_install_wizard(config))
        # Reload config after setup
        config = load_config()

    repl = PhosonRepl(config)
    asyncio.run(repl.run())


if __name__ == "__main__":
    main()
