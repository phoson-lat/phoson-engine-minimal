"""mcps.json rules: don't lose tokens when editing and don't mix transports.

The sidecar never returns ``env``/``headers`` values (they are tokens), so the
UI edits blind: it sends the keys with an empty value except the ones the user
typed. These tests pin down that semantics —and the spec cleanup, which avoids
leaving a dangling `command` on a remote server.

It deliberately does not import `server.py`: that module needs the engine
installed.

    python bridge/tests/test_mcp_config.py
"""

import sys
from typing import Any
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoson_bridge.mcp_config import (  # noqa: E402
    clean_spec,
    sanitize_spec,
    merge_secret_map,
)

failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(name)


def _merge(
    spec: dict[str, Any], incoming: dict[str, Any], key: str = "env"
) -> dict[str, Any]:
    merge_secret_map(spec, incoming, key, f"{key}Remove")
    return spec


# 1) An empty value keeps the stored secret; a new empty key is not stored.
spec: dict[str, Any] = {"transport": "stdio", "env": {"TOKEN": "guardado"}}
_merge(spec, {"env": {"TOKEN": "", "NUEVA": ""}})
_check(
    "editar sin reescribir el token lo conserva",
    spec["env"] == {"TOKEN": "guardado"},
    str(spec["env"]),
)

# 2) Typing a value overwrites it.
_merge(spec, {"env": {"TOKEN": "nuevo"}})
_check(
    "un valor escrito pisa al guardado",
    spec["env"]["TOKEN"] == "nuevo",
    str(spec["env"]),
)

# 3) `envRemove` deletes what the user removed from the form.
_merge(spec, {"envRemove": ["TOKEN"]})
_check("borrar la clave quita el campo vacío", "env" not in spec, str(spec))

# 4) Keys are trimmed (a line with spaces does not create a duplicate).
spec = {"transport": "stdio", "env": {"TOKEN": "guardado"}}
_merge(spec, {"env": {" TOKEN ": "nuevo"}})
_check(
    "la clave se recorta antes de fusionar",
    spec["env"] == {"TOKEN": "nuevo"},
    str(spec),
)

# 5) Headers: same semantics (Authorization cannot be shown).
spec = {
    "transport": "http",
    "url": "https://a/mcp",
    "headers": {"Authorization": "Bearer x"},
}
merge_secret_map(
    spec,
    {"headers": {"Authorization": ""}, "headersRemove": []},
    "headers",
    "headersRemove",
)
_check(
    "una cabecera sin valor conserva el Bearer guardado",
    spec["headers"] == {"Authorization": "Bearer x"},
    str(spec["headers"]),
)
merge_secret_map(spec, {"headersRemove": ["Authorization"]}, "headers", "headersRemove")
_check("quitar la cabecera la elimina", "headers" not in spec, str(spec))

# 6) Cleanup per transport.
stdio: dict[str, Any] = {
    "transport": "stdio",
    "command": "npx",
    "url": "https://sobra/mcp",
    "headers": {"X": "1"},
}
sanitize_spec(stdio)
_check(
    "stdio se queda con el comando y pierde url/cabeceras",
    stdio == {"transport": "stdio", "command": "npx"},
    str(stdio),
)

remote: dict[str, Any] = {
    "transport": "http",
    "url": "https://a/mcp",
    "command": "npx",
    "args": ["-y", "x"],
    "headers": {"Authorization": "Bearer x"},
}
sanitize_spec(remote)
_check(
    "remoto se queda con url/cabeceras y pierde comando/args",
    remote
    == {
        "transport": "http",
        "url": "https://a/mcp",
        "headers": {"Authorization": "Bearer x"},
    },
    str(remote),
)

legacy: dict[str, Any] = {
    "url": "https://a/mcp",
    "headers": {"Authorization": "Bearer x"},
}
sanitize_spec(legacy)
_check(
    "una spec antigua con url y sin transport cuenta como remota",
    legacy == {"url": "https://a/mcp", "headers": {"Authorization": "Bearer x"}},
    str(legacy),
)

# 7) `clean_spec` drops empty fields before sanitizing and returns the spec.
spec = {"transport": "stdio", "command": "npx", "args": [], "url": "", "enabled": True}
cleaned = clean_spec(spec)
_check(
    "los campos vacíos no llegan a mcps.json",
    cleaned == {"transport": "stdio", "command": "npx", "enabled": True},
    str(cleaned),
)

# 8) Edit form round trip (what the UI does on save).
saved: dict[str, Any] = {
    "transport": "http",
    "url": "https://a/mcp",
    "enabled": True,
    "headers": {"Authorization": "Bearer guardado", "X-Trace": "1"},
    "env": {"REGION": "eu"},
}
incoming = {
    "transport": "http",
    "url": "https://a/mcp",
    "enabled": True,
    "headers": {"Authorization": "", "X-Trace": "2"},  # only the trace is rewritten
    "headersRemove": [],
    "env": {"REGION": ""},
    "envRemove": [],
}
merge_secret_map(saved, incoming, "env", "envRemove")
merge_secret_map(saved, incoming, "headers", "headersRemove")
clean_spec(saved)
_check(
    "guardar sin tocar un token lo conserva y actualiza el resto",
    saved["headers"] == {"Authorization": "Bearer guardado", "X-Trace": "2"}
    and saved["env"] == {"REGION": "eu"},
    str(saved),
)

print("-" * 40)
print("FAILED:", failures) if failures else print("todo OK")
sys.exit(1 if failures else 0)
