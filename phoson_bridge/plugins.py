"""Engine plugin catalog for the "Plugins" section of Settings.

The engine resolves plugins through **two coexisting paths**:

* ``[defaults].plugins`` — the list of specs (``phoson-plugin-x``, inline tables
  with ``config``, ``entrypoint:``/``path:``) written by the ``phoson-cli``
  wizard and :mod:`phoson_cli.plugin_manager`. The engine loads it first.
* The ``enable_*`` flags (``enable_mcp``, ``enable_monitors``, …): the matching
  builder returns a *pre-configured* instance of the plugin.

They are not additive: with the same spec in the list **and** its flag on, the
plugin is loaded twice (the engine does not deduplicate), so this module never
turns both on at once. The list is the mechanism for **enabling** a plugin that
has no persistable flag, and the flag is the mechanism for the six that do
(``enable_mcp``, ``enable_monitors``, ``enable_bgjobs``, ``enable_computeruse``,
``enable_swarm``, ``enable_questions``); disabling clears exactly what enabling
set.

Two flags that the engine resolves but ``save_config`` does **not** persist
(``enable_ssh``/``enable_otel``: only read from the file and the environment)
are treated as external state: if they are on, the plugin is already enabled by
a source the UI cannot write, so the toggle is locked with the reason instead of
lying (the catalog reports it in ``lockReason``).

Dictation and peers are not managed here: ``phoson-plugin-stt`` has its own
Settings section (and the bridge imports the plugin directly, so the list does
not decide whether dictation is available) and ``phoson-plugin-peers`` is
enabled via ``peer_name``.

Nothing in this module builds the engine: it operates on the session's live
config (``SessionManager.config_for``) and persists with ``save_config``, just
like ``config.set`` during onboarding.
"""

import os
import sys
import shutil
import tomllib
import importlib.util
from typing import Any, cast
from pathlib import Path

#: spec → ``(config field, environment variable, persisted by save_config?)``.
#: The last two flags (ssh, otel) are only read from the file/environment: the engine
#: resolves them but ``save_config`` does not write them, so their toggle cannot
#: turn them off.
_FLAGS: dict[str, tuple[str, str, bool]] = {
    "phoson-plugin-mcp": ("enable_mcp", "PHOSON_ENABLE_MCP", True),
    "phoson-plugin-monitor": ("enable_monitors", "PHOSON_ENABLE_MONITORS", True),
    "phoson-plugin-bgjobs": ("enable_bgjobs", "PHOSON_ENABLE_BGJOBS", True),
    "phoson-plugin-computeruse": (
        "enable_computeruse",
        "PHOSON_ENABLE_COMPUTERUSE",
        True,
    ),
    "phoson-plugin-swarm": ("enable_swarm", "PHOSON_ENABLE_SWARM", True),
    "phoson-plugin-questions": ("enable_questions", "PHOSON_ENABLE_QUESTIONS", True),
    "phoson-plugin-ssh": ("enable_ssh", "PHOSON_ENABLE_SSH", False),
    "phoson-plugin-otel": ("enable_otel", "PHOSON_ENABLE_OTEL", False),
}

#: Specs the UI only **displays**: their state is decided elsewhere (reason
#: code, not a sentence — the frontend translates it).
_READ_ONLY: dict[str, str] = {
    "phoson-plugin-stt": "dictation",
    "phoson-plugin-peers": "peerName",
}


def config_path() -> Path:
    """Path of the ``config.toml`` shared with the CLI."""
    return Path("~/.phoson/config.toml").expanduser()


def _file_defaults() -> dict[str, Any]:
    """``[defaults]`` section of ``config.toml`` (``{}`` if missing or broken)."""
    try:
        raw = tomllib.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = raw.get("defaults")
    return section if isinstance(section, dict) else {}


def _labels() -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """Name/description of each official plugin, as defined by the engine.

    The bridge does not invent the catalog: it reuses ``phoson_cli.labels`` (the
    same source as the wizard). Without an importable engine the catalog is
    empty and only the specs already in ``config.toml`` are listed.
    """
    try:
        from phoson_cli.labels import PLUGIN_LABELS, PLUGIN_REQUIRED_EXTRAS
    except Exception:  # noqa: BLE001 — optional catalog, never breaks Settings
        return {}, {}
    return dict(PLUGIN_LABELS), dict(PLUGIN_REQUIRED_EXTRAS)


def _spec_name(spec: Any) -> str:
    """Identifier of a spec as written in ``config.toml``."""
    if isinstance(spec, dict):
        return str(spec.get("name", ""))
    return str(spec or "")


def _matches(spec: Any, plugin_id: str) -> bool:
    """Does the spec match ``plugin_id``? (same rule as ``plugin_manager``)."""
    name = _spec_name(spec)
    return name == plugin_id or name == f"entrypoint:{plugin_id}"


def _listed(cfg: Any, plugin_id: str) -> bool:
    """Is the spec active in ``[defaults].plugins``?"""
    return any(
        _matches(spec, plugin_id) for spec in (getattr(cfg, "plugins", None) or [])
    )


def _flag_on(cfg: Any, field: str) -> bool:
    return bool(getattr(cfg, field, False)) if field else False


def _flag_origin(env_var: str, field: str, defaults: dict[str, Any]) -> str:
    """Where an active flag comes from: ``env:VAR``, ``file:field`` or ``""``.

    Lets the toggle lock name the exact source to edit by hand (the engine does
    not record where booleans come from).
    """
    if os.environ.get(env_var):
        return f"env:{env_var}"
    if field and field in defaults:
        return f"file:{field}"
    return f"file:{field}" if field else ""


def _missing_extra(spec: str, extras: dict[str, tuple[str, str]]) -> str:
    """Optional ``[extra]`` missing for the plugin to import (``""`` if present).

    Uses ``importlib.util.find_spec`` like the wizard: the extra is what is
    actually missing from the environment (e.g. ``asyncssh`` for ``[ssh]``).
    """
    requirement = extras.get(spec)
    if requirement is None:
        return ""
    module, extra = requirement
    try:
        found = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return extra
    return "" if found is not None else extra


def catalog(cfg: Any) -> list[dict[str, Any]]:
    """Catalog for the UI: the engine's official plugins plus the added ones.

    Each entry carries its state (**enabled or not**), which mechanism governs
    it, whether its toggle is locked and why, and the missing optional extra.
    Specs outside the official catalog (community, ``path:…``, inline tables,
    ``entrypoint:…``) are listed too: if they are in ``config.toml`` they can be
    disabled from here.
    """
    labels, extras = _labels()
    defaults = _file_defaults()
    entries = [
        _entry(cfg, spec, name, description, extras, defaults, official=True)
        for spec, (name, description) in labels.items()
    ]

    known = set(labels)
    seen: set[str] = set()
    for spec in [
        *(getattr(cfg, "plugins", None) or []),
        *(getattr(cfg, "disabled_plugins", None) or []),
    ]:
        plugin_id = _spec_name(spec)
        if not plugin_id or plugin_id in known or plugin_id in seen:
            continue
        seen.add(plugin_id)
        entries.append(
            _entry(cfg, plugin_id, plugin_id, "", extras, defaults, official=False)
        )
    return entries


def _entry(
    cfg: Any,
    plugin_id: str,
    name: str,
    description: str,
    extras: dict[str, tuple[str, str]],
    defaults: dict[str, Any],
    *,
    official: bool,
) -> dict[str, Any]:
    field, env_var, persistable = _FLAGS.get(plugin_id, ("", "", False))
    flag_on = _flag_on(cfg, field)
    listed = _listed(cfg, plugin_id)
    read_only = _READ_ONLY.get(plugin_id, "")

    lock_reason = read_only
    if not lock_reason and flag_on and not persistable:
        # Enabled by a source the UI cannot write (file or environment): its
        # toggle cannot turn it off, so it is locked and the source is named.
        lock_reason = _flag_origin(env_var, field, defaults)

    return {
        "id": plugin_id,
        "name": name,
        "description": description,
        "enabled": listed or flag_on,
        # Enabling mechanism: a config flag or the spec list.
        "mechanism": "flag" if (field and persistable) else "list",
        "flag": field,
        "listed": listed,
        # `phoson_cli.labels` describes the engine's own plugins; the remaining
        # specs come from `plugins`/`disabled_plugins` (installed by the user).
        "official": official,
        "locked": bool(lock_reason),
        "lockReason": lock_reason,
        # Missing optional extra: the UI warns when the plugin is enabled.
        "missingExtra": _missing_extra(plugin_id, extras),
    }


def _unpark(cfg: Any, plugin_id: str) -> bool:
    """Take the spec out of ``disabled_plugins`` without enabling it.

    ``True`` if it removed it.
    """
    parked = getattr(cfg, "disabled_plugins", None) or []
    kept = [spec for spec in parked if not _matches(spec, plugin_id)]
    if len(kept) == len(parked):
        return False
    cfg.disabled_plugins = kept
    return True


def _enable_spec(cfg: Any, plugin_id: str) -> bool:
    """Enable a spec in ``plugins``: it leaves ``disabled_plugins`` if it was there.

    Restoring the saved spec (string, ``path:`` or inline table with its
    ``config``) preserves the plugin's configuration; if it was never there, the
    bare spec is added (which is what the wizard writes). Returns ``True`` when
    it also had to be taken out of ``disabled_plugins``.
    """
    parked = [
        spec
        for spec in (getattr(cfg, "disabled_plugins", None) or [])
        if _matches(spec, plugin_id)
    ]
    unparked = _unpark(cfg, plugin_id)
    specs = list(cfg.plugins or [])
    if not any(_matches(spec, plugin_id) for spec in specs):
        specs.extend(parked or [plugin_id])
    cfg.plugins = specs
    return unparked


def _park_spec(cfg: Any, plugin_id: str) -> None:
    """Disable a spec: move it to ``disabled_plugins`` (same semantics as
    ``plugin_manager.disable_plugin``, so it can be re-enabled as is)."""
    parked = [spec for spec in (cfg.plugins or []) if _matches(spec, plugin_id)]
    cfg.plugins = [
        spec for spec in (cfg.plugins or []) if not _matches(spec, plugin_id)
    ]
    disabled = list(cfg.disabled_plugins or [])
    for spec in parked:
        if not any(_matches(existing, plugin_id) for existing in disabled):
            disabled.append(spec)
    cfg.disabled_plugins = disabled


def set_enabled(cfg: Any, plugin_id: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable a plugin and persist the change to ``config.toml``.

    Returns ``{"ok": True, "path":…, "fields":[…]}` with the touched fields (the
    caller mirrors them into the base config and reloads the engine) or
    ``{"ok": False, "reason": "locked", "detail":…}`` when another source pins
    the state and the toggle cannot change it.
    """
    from phoson_cli.config import save_config

    plugin_id = str(plugin_id or "").strip()
    if not plugin_id:
        raise ValueError("la spec del plugin no puede estar vacía")

    read_only = _READ_ONLY.get(plugin_id)
    if read_only:
        return {"ok": False, "reason": "locked", "detail": read_only}

    field, env_var, persistable = _FLAGS.get(plugin_id, ("", "", False))
    flag_on = _flag_on(cfg, field)
    listed = _listed(cfg, plugin_id)
    fields: set[str] = set()

    if enabled:
        if listed or flag_on:
            return {"ok": True, "fields": []}  # already enabled: idempotent
        if field and persistable:
            # The flag is enough (and turning it on with the spec in the list
            # would load it twice, so they are mutually exclusive by design).
            setattr(cfg, field, True)
            fields.add(field)
            # A previously parked spec would lie in `plugin list` (it would say
            # "disabled" with the plugin loaded), so it is discarded.
            if _unpark(cfg, plugin_id):
                fields.add("disabled_plugins")
        else:
            # The spec list is the mechanism when the plugin has no flag
            # (memory, checkpoint, third-party mcp, path:…). `disabled_plugins`
            # is only written if there really was a parked spec to take out.
            if _enable_spec(cfg, plugin_id):
                fields.add("disabled_plugins")
            fields.add("plugins")
    else:
        if not listed and not flag_on:
            return {"ok": True, "fields": []}  # already disabled: idempotent
        if flag_on and not persistable:
            return {
                "ok": False,
                "reason": "locked",
                "detail": _flag_origin(env_var, field, _file_defaults()),
            }
        if flag_on:
            setattr(cfg, field, False)
            fields.add(field)
        if listed:
            _park_spec(cfg, plugin_id)
            fields |= {"plugins", "disabled_plugins"}

    path = save_config(cfg, only_fields=fields)
    return {"ok": True, "path": str(path), "fields": sorted(fields)}


def installable() -> tuple[bool, str]:
    """Can this sidecar install plugins? ``(yes, reason)``.

    The engine's ``install_plugin`` runs ``uv pip install --python
    sys.executable`` and then re-queries the entry points in a **fresh
    interpreter**: in the frozen sidecar (PyInstaller) there is neither ``uv``
    nor a re-executable ``sys.executable``, so installing from the UI is not
    possible there and we say so instead of trying and failing halfway.
    """
    if getattr(sys, "frozen", False):
        return False, "frozen"
    if shutil.which("uv") is None:
        return False, "noUv"
    return True, ""


def install(cfg: Any, source: str) -> str:
    """Install a plugin from ``source`` and enable it (``install_plugin``).

    A plugin is Python code running with the user's permissions: the caller
    **must** confirm first (the handler requires ``confirmed``).
    """
    from phoson_cli.plugin_manager import install_plugin

    # Without a ``confirm`` callback the install never declines (no ``None``).
    return cast(str, install_plugin(str(source).strip(), cfg))
