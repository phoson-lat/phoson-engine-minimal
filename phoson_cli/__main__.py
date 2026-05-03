"""Entry point for the Phoson CLI application."""

import sys
import asyncio

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import load_config
from phoson_cli.installer import run_install_wizard


def main() -> None:
    """Run the Phoson CLI REPL or setup wizard."""
    config = load_config()

    if any(arg in {"--install", "--setup"} for arg in sys.argv[1:]):
        asyncio.run(run_install_wizard(config))
        return

    repl = PhosonRepl(config)
    asyncio.run(repl.run())


if __name__ == "__main__":
    main()
