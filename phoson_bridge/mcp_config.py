"""Rules for `~/.phoson/mcps.json`: merge secrets and clean up the spec.

Lives apart from `server.py` for two reasons: the sidecar imports `phoson_cli`
(which is not available in every CI environment), and these are exactly the
operations that can break an existing configuration — losing a stored token or
leaving a server with fields from another transport.
"""

from typing import Any


def merge_secret_map(
    spec: dict[str, Any], incoming: dict[str, Any], key: str, remove_key: str
) -> None:
    """Merge ``env``/``headers`` from ``incoming`` into ``spec``.

    The UI never receives the values (they are tokens), so an edit arrives as a
    dict with the keys and empty values, except for the ones the user typed
    something into:

    - non-empty value → stored (created or overwritten);
    - key with empty value → **keeps** the stored value (new key: ignored, there
      is no point in storing ``TOKEN=""``);
    - ``remove_key`` (``envRemove`` / ``headersRemove``) → deletes those keys,
      which is how a secret is removed without seeing it.

    If no key remains, the field is dropped from the spec (clean mcps.json).
    """
    current = spec.get(key)
    merged = dict(current) if isinstance(current, dict) else {}
    for raw_key, value in (incoming.get(key) or {}).items():
        name = str(raw_key).strip()
        if not name:
            continue
        text = "" if value is None else str(value).strip()
        if text:
            merged[name] = text
    for raw_key in incoming.get(remove_key) or []:
        merged.pop(str(raw_key).strip(), None)
    if merged:
        spec[key] = merged
    else:
        spec.pop(key, None)


def sanitize_spec(spec: dict[str, Any]) -> None:
    """Keep only the fields of the spec's transport.

    ``transport`` wins; if missing (old specs) it is inferred from ``url``, as
    `_mcp_summary` does. A stdio server uses neither ``url`` nor ``headers``,
    and a remote one uses neither ``command`` nor ``args``.
    """
    transport = str(spec.get("transport") or ("sse" if spec.get("url") else "stdio"))
    if transport != "stdio":
        spec.pop("command", None)
        spec.pop("args", None)
    else:
        spec.pop("url", None)
        spec.pop("headers", None)


def clean_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Drop empty fields and sanitize the transport (before saving)."""
    for key in ("command", "url", "args"):
        if not spec.get(key):
            spec.pop(key, None)
    sanitize_spec(spec)
    return spec
