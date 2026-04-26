import asyncio

from phoson_cli.repl import PhosonRepl
from phoson_cli.config import load_config


def main() -> None:
    config = load_config()
    repl = PhosonRepl(config)
    asyncio.run(repl.run())


if __name__ == "__main__":
    main()
