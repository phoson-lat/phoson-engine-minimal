"""MCP catalog: normalize the official registry and cache searches.

The registry returns **one entry per version** and mixes packages (npm, pypi,
docker) with remotes (streamable-http, sse). This test pins down the translation
the UI sees: latest version, the command the engine would launch, and only the
**names** of env vars/headers (never token values). It does not touch the
network: `_fetch` is replaced.

    python bridge/tests/test_mcp_catalog.py
"""

import sys
import urllib.error
from typing import Any
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoson_bridge import mcp_catalog as C  # noqa: E402

failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(name)


def _entry(
    name: str,
    version: str,
    *,
    packages: list[dict[str, Any]] | None = None,
    remotes: list[dict[str, Any]] | None = None,
    latest: bool = True,
    status: str = "active",
    title: str = "",
) -> dict[str, Any]:
    server: dict[str, Any] = {
        "name": name,
        "version": version,
        "description": f"{name} desc",
    }
    if title:
        server["title"] = title
    if packages:
        server["packages"] = packages
    if remotes:
        server["remotes"] = remotes
    return {
        "server": server,
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "isLatest": latest,
                "status": status,
            }
        },
    }


# 1) Deduplicates by name and keeps the latest published version.
raw = [
    _entry(
        "acme/thing",
        "1.0.0",
        latest=False,
        remotes=[{"type": "sse", "url": "https://a/mcp"}],
    ),
    _entry(
        "acme/thing",
        "1.2.0",
        remotes=[{"type": "streamable-http", "url": "https://b/mcp"}],
    ),
    _entry(
        "acme/old",
        "1.0.0",
        status="deleted",
        remotes=[{"type": "sse", "url": "https://c/mcp"}],
    ),
]
out = C.normalize(raw)
_check("una entrada por servidor", len(out) == 1, str([e["id"] for e in out]))
_check(
    "gana la versión marcada como latest",
    out[0]["version"] == "1.2.0",
    out[0]["version"],
)
_check(
    "los borrados del registro no se listan",
    all(e["id"] != "acme/old" for e in out),
    str([e["id"] for e in out]),
)
_check(
    "streamable-http se traduce a http",
    out[0]["transport"] == "http" and out[0]["url"] == "https://b/mcp",
    str(out[0]),
)

# 2) npm package → `npx -y package@version` + environment keys.
npm = C.normalize(
    [
        _entry(
            "io.github.acme/weather",
            "0.4.0",
            packages=[
                {
                    "registryType": "npm",
                    "identifier": "acme-weather-mcp",
                    "version": "0.4.0",
                    "runtimeHint": "npx",
                    "transport": {"type": "stdio"},
                    "runtimeArguments": [{"type": "positional", "value": "-y"}],
                    "packageArguments": [{"type": "positional", "value": "--city"}],
                    "environmentVariables": [
                        {"name": "ACME_KEY", "isRequired": True},
                        {"name": "ACME_REGION"},
                    ],
                }
            ],
        )
    ]
)[0]
_check(
    "npm arranca con npx -y y fija la versión",
    npm["command"] == "npx" and npm["args"][:2] == ["-y", "acme-weather-mcp@0.4.0"],
    f"{npm['command']} {npm['args']}",
)
_check(
    "los argumentos del paquete van detrás",
    npm["args"][-1] == "--city",
    str(npm["args"]),
)
_check(
    "env: nombres y solo la obligatoria en `required`",
    npm["envKeys"] == ["ACME_KEY", "ACME_REGION"] and npm["required"] == ["ACME_KEY"],
    str((npm["envKeys"], npm["required"])),
)
_check("el nombre sugerido sale del registro", npm["name"] == "weather", npm["name"])

# 3) pypi package → uvx; no `-y` (not applicable) and no `@` pin.
pypi = C.normalize(
    [
        _entry(
            "io.github.acme/git-tools",
            "2.0.0",
            packages=[
                {
                    "registryType": "pypi",
                    "identifier": "acme-git-mcp",
                    "version": "2.0.0",
                    "runtimeHint": "uvx",
                    "transport": {"type": "stdio"},
                }
            ],
        )
    ]
)[0]
_check(
    "pypi arranca con uvx",
    pypi["command"] == "uvx" and pypi["args"] == ["acme-git-mcp"],
    f"{pypi['command']} {pypi['args']}",
)

# 4) Docker image → `docker run -i --rm`.
oci = C.normalize(
    [
        _entry(
            "io.github.acme/box",
            "1.0.0",
            packages=[
                {
                    "registryType": "oci",
                    "identifier": "ghcr.io/acme/box:1.0.0",
                    "transport": {"type": "stdio"},
                }
            ],
        )
    ]
)[0]
_check(
    "oci arranca con docker run -i --rm",
    oci["command"] == "docker" and oci["args"][:3] == ["run", "-i", "--rm"],
    f"{oci['command']} {oci['args']}",
)

# 5) Remote: headers by name and never by value.
remote = C.normalize(
    [
        _entry(
            "ai.acme/remote",
            "1.0.0",
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://api.acme.ai/mcp",
                    "headers": [
                        {
                            "name": "Authorization",
                            "value": "Bearer {acme_key}",
                            "isRequired": True,
                        },
                        {"name": "X-Trace"},
                    ],
                }
            ],
        )
    ]
)[0]
_check(
    "remoto: transporte, URL y claves de cabecera",
    remote["transport"] == "http"
    and remote["url"] == "https://api.acme.ai/mcp"
    and remote["headerKeys"] == ["Authorization", "X-Trace"],
    str(remote),
)
_check(
    "remoto: la obligatoria se avisa y no se filtra ningún valor",
    remote["required"] == ["Authorization"]
    and "Bearer" not in str(remote)
    and "{acme_key}" not in str(remote),
    str(remote),
)

# 6) A remote with a templated URL is unusable: it is skipped.
templated = C.normalize(
    [
        _entry(
            "ai.acme/tenant",
            "1.0.0",
            remotes=[
                {"type": "streamable-http", "url": "https://{tenant}.acme.ai/mcp"}
            ],
        )
    ]
)
_check("se omite una URL con plantilla", templated == [], str(templated))

# 7) Suggested name: a useful tail, even when the tail is generic.
_check(
    "nombre desde la cola del identificador",
    C._suggest_name("com.pulsemcp/remote-filesystem") == "remote-filesystem",
    C._suggest_name("com.pulsemcp/remote-filesystem"),
)
_check(
    "cola genérica /mcp → parte útil del dominio",
    C._suggest_name("ac.inference.sh/mcp") == "inference",
    C._suggest_name("ac.inference.sh/mcp"),
)
_check(
    "sin nombre → nombre de reserva",
    C._suggest_name("/") == "mcp",
    C._suggest_name("/"),
)

# 8) A query that is too short does not go to the network.
C.clear_cache()
with patch.object(C, "_fetch", side_effect=AssertionError("no debería consultar")):
    short = C.search("a")
_check(
    "menos de 2 caracteres: vacío sin red",
    short["ok"] is True and short["entries"] == [] and short["cached"] is False,
    str(short),
)

# 9) The cache avoids repeating the same search.
C.clear_cache()
calls: list[str] = []


def _fake_fetch(query: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    calls.append(query)
    return [
        _entry("acme/thing", "1.0.0", remotes=[{"type": "sse", "url": "https://a/mcp"}])
    ]


with patch.object(C, "_fetch", _fake_fetch):
    first = C.search("thing")
    second = C.search("THING")  # the cache key ignores case
_check(
    "la segunda búsqueda sale de la caché",
    first["ok"] and second["cached"] is True and len(calls) == 1,
    str((calls, second["cached"])),
)

# 10) No network: a translatable result, not an exception.
C.clear_cache()
with patch.object(C, "_fetch", side_effect=urllib.error.URLError("sin red")):
    offline = C.search("github")
_check(
    "un fallo de red se reporta como ok=False/reason=network",
    offline["ok"] is False
    and offline["reason"] == "network"
    and offline["entries"] == [],
    str(offline),
)

print("-" * 40)
print("FAILED:", failures) if failures else print("todo OK")
sys.exit(1 if failures else 0)
