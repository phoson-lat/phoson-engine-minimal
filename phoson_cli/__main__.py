"""
Entry point for the Phoson CLI application.

Initializes the configuration and runs the interactive REPL.
"""

import asyncio

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import load_config


def main() -> None:
    """Run the Phoson CLI REPL."""
    config = load_config()
    repl = PhosonRepl(config)
    asyncio.run(repl.run())


if __name__ == "__main__":
    main()
