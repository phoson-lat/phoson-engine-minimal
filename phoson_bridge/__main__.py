"""Entry point for the ``phoson-bridge`` sidecar.

CRITICAL: stdout is the protocol channel. Any ``print`` from the engine or the
plugins would break it, so here we duplicate the original fd for the protocol
and reassign ``sys.stdout`` to stderr. From this point on, anything plugins
write "to stdout" goes to stderr (visible in logs, not in the framing).
"""

import os
import sys


def run() -> None:
    protocol_fd = os.dup(sys.stdout.fileno())
    protocol_out = os.fdopen(protocol_fd, "w", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr  # isolates stray prints from the framing

    # ABSOLUTE import: when frozen with PyInstaller the script runs as
    # `__main__` with no package, so a relative import would fail.
    from phoson_bridge.server import main

    main(protocol_out)


if __name__ == "__main__":
    run()
