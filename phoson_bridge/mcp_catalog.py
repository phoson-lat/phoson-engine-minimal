"""MCP server catalog: search in the **official registry**.

`allmcpservers.com` has no API of its own: it aggregates the *Official MCP
Registry* (``registry.modelcontextprotocol.io``) and Claude's connector
directory. Here we query the former —public, keyless and with open CORS— so the
app offers an always up-to-date catalog instead of a template list frozen into
the binary.

The registry returns **one entry per version**: `normalize` deduplicates by name
(keeping the latest), picks how the server runs (first stdio package, or its
remote) and translates the manifest into the command the engine would launch.
The UI only renders what comes out of here: this module **neither installs nor
saves anything**, and never returns token values —only the names of the
variables and headers the user must fill in.
"""

import re
import json
import time
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("phoson_bridge")

#: Search endpoint of the official registry (v0, stable since 2025).
REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"

#: In-memory cache TTL: the registry changes daily, not by the minute.
_CACHE_TTL = 600.0

#: Maximum number of cached queries (each is ~30 already-normalized entries).
_CACHE_MAX = 32

#: HTTP request cap: the UI waits with a spinner, not forever.
DEFAULT_TIMEOUT = 15.0

#: Entries per query (the catalog paginates client-side).
MAX_RESULTS = 60

#: Parts of a reversed name that add nothing to the suggested name.
_GENERIC_LABELS = {
    "com",
    "io",
    "org",
    "net",
    "dev",
    "ai",
    "app",
    "sh",
    "ac",
    "github",
    "gitlab",
    "mcp",
    "server",
}

#: Name tails that are not useful as a server identifier either.
_GENERIC_TAILS = {"", "mcp", "server", "mcp-server", "sse", "http"}

#: Registry remote transports → the ones the engine understands.
_REMOTE_TRANSPORT = {"streamable-http": "http", "http": "http", "sse": "sse"}

#: Cache ``key → (monotonic, entries)``. Lives as long as the sidecar.
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def search(
    query: str, limit: int = 30, *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Search ``query`` in the registry and return the already-normalized entries.

    The result always has the same shape (``ok``/``entries``/``reason``) so the
    UI does not have to distinguish between "no network" and "no results":
    ``ok=False`` with a short, translatable ``reason``.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return {"ok": True, "entries": [], "query": q, "reason": None, "cached": False}
    limit = max(1, min(int(limit or 30), MAX_RESULTS))

    key = f"{q.lower()}|{limit}"
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL:
        return {
            "ok": True,
            "entries": hit[1],
            "query": q,
            "reason": None,
            "cached": True,
        }

    try:
        # The registry returns one entry per VERSION: request extra so that
        # after deduplication `limit` distinct servers remain.
        raw = _fetch(q, min(limit * 3, MAX_RESULTS), timeout)
    except urllib.error.HTTPError as exc:
        log.warning("registro MCP respondió %s", exc.code)
        return _failed(q, "http", f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("registro MCP inalcanzable: %s", exc)
        return _failed(q, "network", str(exc))
    except ValueError as exc:  # includes JSONDecodeError
        log.warning("respuesta ilegible del registro MCP: %s", exc)
        return _failed(q, "badResponse", str(exc))

    entries = normalize(raw)[:limit]
    _remember(key, entries)
    return {"ok": True, "entries": entries, "query": q, "reason": None, "cached": False}


def _failed(query: str, reason: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "entries": [],
        "query": query,
        "reason": reason,
        "error": detail,
        "cached": False,
    }


def _fetch(query: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    """GET the registry, in its own thread (`asyncio.to_thread`), never on the loop."""
    params = urllib.parse.urlencode({"search": query, "limit": limit})
    request = urllib.request.Request(  # noqa: S310 — fixed URL (https) + quoted query
        f"{REGISTRY_URL}?{params}",
        headers={
            "Accept": "application/json",
            "User-Agent": "Phoson-Desktop (MCP catalog)",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    servers = payload.get("servers") if isinstance(payload, dict) else None
    return servers if isinstance(servers, list) else []


def _remember(key: str, entries: list[dict[str, Any]]) -> None:
    _cache[key] = (time.monotonic(), entries)
    while len(_cache) > _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)


def clear_cache() -> None:
    """Clear the cache (tests and "Retry" from the UI)."""
    _cache.clear()


# ── Normalization ─────────────────────────────────────────────────────────────


def _official_meta(entry: dict[str, Any]) -> dict[str, Any]:
    meta = entry.get("_meta")
    if not isinstance(meta, dict):
        return {}
    official = meta.get("io.modelcontextprotocol.registry/official")
    return official if isinstance(official, dict) else {}


def _version_key(version: Any) -> tuple[int, ...]:
    """Compare versions without packaging: ``1.2.10`` > ``1.2.9``."""
    parts = re.split(r"[.+-]", str(version or ""))
    return tuple(int(p) if p.isdigit() else 0 for p in parts)


def _pick_latest(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by server name, keeping its most recent version."""
    picked: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        server = entry.get("server")
        if not isinstance(server, dict):
            continue
        name = str(server.get("name") or "").strip()
        if not name:
            continue
        if _official_meta(entry).get("status") not in (None, "active"):
            continue
        current = picked.get(name)
        if current is None:
            picked[name] = entry
            continue
        latest = bool(_official_meta(entry).get("isLatest"))
        was_latest = bool(_official_meta(current).get("isLatest"))
        if latest != was_latest:
            if latest:
                picked[name] = entry
            continue
        if _version_key(server.get("version")) > _version_key(
            (current.get("server") or {}).get("version")
        ):
            picked[name] = entry
    return list(picked.values())


def _stdio_package(server: dict[str, Any]) -> dict[str, Any] | None:
    """First package that can be launched over stdio (npm > pypi > docker)."""
    best: tuple[int, dict[str, Any]] | None = None
    for package in server.get("packages") or []:
        if not isinstance(package, dict):
            continue
        transport = package.get("transport")
        kind = (
            str(transport.get("type") or "stdio")
            if isinstance(transport, dict)
            else "stdio"
        )
        if kind != "stdio":
            continue
        # `npx`/`uvx`/`docker` already come from `runtimeHint`; order only breaks ties.
        rank = {"npm": 0, "pypi": 1, "oci": 2}.get(
            str(package.get("registryType") or ""), 9
        )
        if best is None or rank < best[0]:
            best = (rank, package)
    return best[1] if best else None


def _remote(server: dict[str, Any]) -> dict[str, Any] | None:
    """First supported remote (streamable-http before sse, like the registry)."""
    remotes = [r for r in (server.get("remotes") or []) if isinstance(r, dict)]
    for wanted in ("streamable-http", "sse"):
        for remote in remotes:
            kind = str(remote.get("type") or "")
            mapped = _REMOTE_TRANSPORT.get(kind)
            if not mapped:
                continue
            if kind == wanted or (wanted == "streamable-http" and mapped == "http"):
                return remote
    return None


def _arg_list(values: Any) -> list[str]:
    """``[{"type":"positional","value":"-y"}]`` → ``["-y"]`` (and the named ones)."""
    out: list[str] = []
    for arg in values or []:
        if not isinstance(arg, dict) or arg.get("value") is None:
            continue
        value = str(arg.get("value"))
        if arg.get("type") == "named" and arg.get("name"):
            out.extend([f"--{arg['name']}", value])
        else:
            out.append(value)
    return out


def _stdio_command(package: dict[str, Any]) -> tuple[str, list[str]]:
    """Translate the package manifest into the command the engine would launch."""
    kind = str(package.get("registryType") or "")
    identifier = str(package.get("identifier") or "")
    version = str(package.get("version") or "")
    pinned = f"{identifier}@{version}" if kind == "npm" and version else identifier
    runtime = str(package.get("runtimeHint") or "")
    runtime_args = _arg_list(package.get("runtimeArguments"))
    package_args = _arg_list(package.get("packageArguments"))

    if kind == "oci":
        return runtime or "docker", [
            "run",
            "-i",
            "--rm",
            *runtime_args,
            pinned,
            *package_args,
        ]
    command = runtime or ("uvx" if kind == "pypi" else "npx")
    if command == "npx" and "-y" not in runtime_args:
        # `npx` without `-y` hangs waiting for confirmation in a TTY-less process.
        runtime_args = ["-y", *runtime_args]
    return command, [*runtime_args, pinned, *package_args]


def _suggest_name(registry_name: str) -> str:
    """``com.pulsemcp/remote-filesystem`` → ``remote-filesystem``.

    The name is the key in ``mcps.json``: it keeps the tail of the reversed name
    and, if that tail is generic (``/mcp``), the identifying part of the domain
    (``ac.inference.sh/mcp`` → ``inference``).
    """
    head, _, tail = registry_name.partition("/")
    candidate = tail.strip()
    if candidate.lower() in _GENERIC_TAILS:
        labels = [
            b for b in re.split(r"[.\-/]", head) if b.lower() not in _GENERIC_LABELS
        ]
        candidate = labels[-1] if labels else (tail or head)
    slug = re.sub(r"[^a-z0-9._-]+", "-", candidate.lower()).strip("-._")
    return slug or "mcp"


def _title(server: dict[str, Any]) -> str:
    title = str(server.get("title") or "").strip()
    if title:
        return title
    name = str(server.get("name") or "")
    tail = name.rpartition("/")[2] or name
    return tail.replace("-", " ").replace("_", " ").strip() or name


def normalize(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert registry entries into what the UI consumes.

    Each output is ``{id,name,title,description,transport,command,args,url,
    envKeys,headerKeys,required,source,repository,version}``. Servers with no
    usable stdio package or remote are skipped.
    """
    out: list[dict[str, Any]] = []
    for entry in _pick_latest(entries):
        server = entry.get("server") or {}
        package = _stdio_package(server)
        remote = _remote(server)
        if package is None and remote is None:
            continue

        env_vars = [
            v
            for v in (package or {}).get("environmentVariables") or []
            if isinstance(v, dict) and str(v.get("name") or "").strip()
        ]
        headers = [
            h
            for h in (remote or {}).get("headers") or []
            if isinstance(h, dict) and str(h.get("name") or "").strip()
        ]

        if package is not None:
            command, args = _stdio_command(package)
            transport, url = "stdio", ""
            source = str(package.get("registryType") or "")
        else:
            command, args = "", []
            transport = _REMOTE_TRANSPORT.get(
                str((remote or {}).get("type") or ""), "http"
            )
            url = str((remote or {}).get("url") or "")
            # Templated URLs (`https://{tenant}.mcp.foo`) cannot be used as is:
            # they are skipped instead of offering a broken server.
            if "{" in url:
                continue
            source = "remote"

        repository = server.get("repository")
        out.append(
            {
                "id": str(server.get("name") or ""),
                "name": _suggest_name(str(server.get("name") or "")),
                "title": _title(server),
                "description": str(server.get("description") or "").strip(),
                "transport": transport,
                "command": command,
                "args": args,
                "url": url,
                "envKeys": [str(v["name"]).strip() for v in env_vars],
                "headerKeys": [str(h["name"]).strip() for h in headers],
                # Only the required ones: the card flags what is left to fill in.
                "required": [
                    str(v["name"]).strip() for v in env_vars if v.get("isRequired")
                ]
                + [str(h["name"]).strip() for h in headers if h.get("isRequired")],
                "source": source,
                "repository": str(repository.get("url") or "")
                if isinstance(repository, dict)
                else "",
                "version": str(server.get("version") or ""),
            }
        )
    return out
