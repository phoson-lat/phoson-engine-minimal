"""JSON-RPC bridge to the engine for GUI clients (Phoson Desktop, mobile).

- ``server``: the bridge itself. One process per workspace speaks JSON-RPC,
  one JSON object per line, on stdin/stdout (``phoson-cli bridge`` or
  ``phoson-bridge``). It hosts any number of sessions, each with its own
  ``SessionController``.
- ``gateway``: ``phoson-cli serve``. The same protocol over WebSocket for
  remote clients: pairing, device tokens, one bridge child per workspace.
- ``devices``: paired devices and pairing codes.
"""
