"""Declarative plugin manifest (``phoson-plugin.toml``).

A manifest describes a plugin *without running it*: identity, version,
compatibility, what it contributes, which capabilities it uses and the
schema of its configuration. Hosts read it to list, inspect, validate and
review a plugin before installing or loading it. See
``docs/research/plugin-manifest-spec.md`` for the full specification.

Location convention: the file lives at the root of the plugin's top-level
import package (``<package>/phoson-plugin.toml``) and ships inside the wheel
as package data. :func:`find_manifest_for_module` locates it through
``importlib.util.find_spec``, which resolves a top-level package's directory
**without executing** its ``__init__``.

The manifest is optional: plugins without one keep loading exactly as before.
When present, :class:`~phoson_agent.plugin_loader.PluginRegistry` checks
compatibility and validates/fills the plugin config before ``configure()``.
"""

import os
import re
import sys
import tomllib
import importlib.util
from typing import Any, Literal
from pathlib import Path
from dataclasses import field, dataclass

from packaging.version import Version, InvalidVersion
from packaging.specifiers import SpecifierSet, InvalidSpecifier

from phoson_agent.exceptions import PhosonPluginConfigError

#: File name of a plugin manifest, at the root of its import package.
MANIFEST_FILENAME = "phoson-plugin.toml"

#: Manifest format versions this engine understands.
SUPPORTED_SCHEMAS: frozenset[int] = frozenset({1})

#: Distribution name used for the ``[compat] phoson`` check.
ENGINE_DISTRIBUTION = "phoson-engine-minimal"

#: Plugin ids: lowercase, digits and dashes, starting with a letter.
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
#: ``module.path:attribute`` (attribute optional → module-level ``plugin``).
_ENTRY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r"(?::[A-Za-z_][A-Za-z0-9_]*)?$"
)
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

ConfigType = Literal["string", "integer", "number", "boolean", "array", "object"]
_CONFIG_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "number", "boolean", "array", "object"}
)
FilesystemAccess = Literal["none", "read", "write"]
_FILESYSTEM_LEVELS: tuple[str, ...] = ("none", "read", "write")

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "id",
        "name",
        "version",
        "description",
        "entry",
        "license",
        "homepage",
        "authors",
        "compat",
        "provides",
        "capabilities",
        "config",
    }
)


class PluginManifestError(PhosonPluginConfigError):
    """A manifest is missing, malformed or fails validation.

    ``problems`` lists every issue found (validation does not stop at the
    first one) so authors can fix a manifest in one pass.
    """

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        self.problems = list(problems or [])
        if self.problems:
            message = message + "\n" + "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(message)


@dataclass(frozen=True)
class ConfigField:
    """Schema of one configuration key (``[config.<key>]``)."""

    key: str
    type: ConfigType = "string"
    description: str = ""
    default: Any = None
    required: bool = False
    secret: bool = False
    env: str | None = None
    choices: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Capabilities:
    """What the plugin may do — shown to the user before install.

    Declarative and advisory: the engine does not sandbox plugins (a plugin
    is Python code running with the user's permissions). The declaration is
    for review and for policy defaults, never a security boundary.
    """

    network: bool = False
    shell: bool = False
    filesystem: FilesystemAccess = "none"


@dataclass(frozen=True)
class Provides:
    """What the plugin contributes to an engine/host."""

    tools: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    middlewares: bool = False
    theme: bool = False


@dataclass(frozen=True)
class PluginManifest:
    """A parsed and validated ``phoson-plugin.toml``."""

    id: str
    version: str
    entry: str
    schema: int = 1
    name: str = ""
    description: str = ""
    license: str = ""
    homepage: str = ""
    authors: tuple[str, ...] = ()
    phoson: str = ""
    python: str = ""
    provides: Provides = field(default_factory=Provides)
    capabilities: Capabilities = field(default_factory=Capabilities)
    config: tuple[ConfigField, ...] = ()
    path: Path | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def entry_module(self) -> str:
        return self.entry.split(":", 1)[0]

    @property
    def entry_attribute(self) -> str:
        """Attribute to load from :attr:`entry_module` (default ``plugin``)."""
        _module, _sep, attribute = self.entry.partition(":")
        return attribute or "plugin"

    # ── Compatibility ────────────────────────────────────────────────────

    def incompatibility(
        self, engine_version: str | None, python_version: str | None = None
    ) -> str | None:
        """Why this plugin cannot run on the given versions, or ``None``.

        An unknown ``engine_version`` (e.g. a frozen build without package
        metadata) skips the engine check rather than refusing the plugin.
        """
        if self.phoson and engine_version:
            try:
                if Version(engine_version) not in SpecifierSet(self.phoson):
                    return (
                        f"requires phoson {self.phoson}, "
                        f"this is phoson {engine_version}"
                    )
            except InvalidVersion:
                pass
        if self.python:
            info = sys.version_info
            current = python_version or f"{info.major}.{info.minor}.{info.micro}"
            if Version(current) not in SpecifierSet(self.python):
                return f"requires Python {self.python}, this is Python {current}"
        return None

    # ── Configuration ────────────────────────────────────────────────────

    def resolve_config(
        self,
        config: dict[str, Any] | None,
        *,
        environ: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Validate *config* against the schema and fill defaults.

        Precedence per key: explicit config value, then the field's ``env``
        variable, then its ``default``. Unknown keys are rejected so typos
        surface at load time instead of being silently ignored. With no
        ``[config]`` table the config is passed through untouched (the plugin
        validates it itself, as before manifests existed).

        Raises:
            PluginManifestError: listing every invalid/missing key.
        """
        provided = dict(config or {})
        if not self.config:
            return provided
        env = os.environ if environ is None else environ
        fields = {f.key: f for f in self.config}
        problems = [
            f"unknown config key {key!r} (expected one of: {', '.join(sorted(fields))})"
            for key in provided
            if key not in fields
        ]
        resolved: dict[str, Any] = {}
        for key, spec in fields.items():
            if key in provided:
                value = provided[key]
            elif spec.env and spec.env in env:
                try:
                    value = _coerce_env(env[spec.env], spec.type)
                except ValueError as exc:
                    problems.append(f"{key}: ${spec.env} {exc}")
                    continue
            elif spec.default is not None:
                value = spec.default
            elif spec.required:
                hint = f" (or set ${spec.env})" if spec.env else ""
                problems.append(f"{key}: required{hint}")
                continue
            else:
                continue
            error = _type_error(value, spec.type)
            if error:
                problems.append(f"{key}: {error}")
                continue
            if spec.choices and value not in spec.choices:
                problems.append(
                    f"{key}: {value!r} is not one of "
                    f"{', '.join(repr(c) for c in spec.choices)}"
                )
                continue
            resolved[key] = value
        if problems:
            raise PluginManifestError(
                f"Invalid configuration for plugin {self.id!r}:", problems
            )
        return resolved

    def summary_lines(self) -> list[str]:
        """Human-readable review lines (install confirmation, ``plugin info``)."""
        lines = [f"{self.display_name} ({self.id}) {self.version}"]
        if self.description:
            lines.append(f"  {self.description}")
        if self.phoson:
            lines.append(f"  requires phoson {self.phoson}")
        caps = self.capabilities
        risky = []
        if caps.shell:
            risky.append("runs shell commands")
        if caps.network:
            risky.append("network access")
        if caps.filesystem != "none":
            risky.append(f"filesystem {caps.filesystem}")
        lines.append(
            "  capabilities: " + (", ".join(risky) if risky else "none declared")
        )
        if self.provides.tools:
            lines.append("  tools: " + ", ".join(self.provides.tools))
        if self.provides.commands:
            lines.append("  commands: " + ", ".join(self.provides.commands))
        extras = [
            name
            for name, on in (
                ("middlewares", self.provides.middlewares),
                ("theme", self.provides.theme),
            )
            if on
        ]
        if extras:
            lines.append("  also: " + ", ".join(extras))
        if self.config:
            keys = ", ".join(f"{f.key}{'*' if f.required else ''}" for f in self.config)
            lines.append(f"  config: {keys}")
        return lines


# ── Parsing ──────────────────────────────────────────────────────────────────


def parse_manifest(data: dict[str, Any], *, path: Path | None = None) -> PluginManifest:
    """Validate a decoded manifest table.

    Raises:
        PluginManifestError: listing every problem found.
    """
    where = str(path) if path else "manifest"
    problems: list[str] = []

    def take(table: dict[str, Any], key: str, kind: type, default: Any) -> Any:
        value = table.get(key, default)
        if value is default:
            return default
        if kind is int and isinstance(value, bool):
            problems.append(f"{key}: expected integer")
            return default
        if not isinstance(value, kind):
            problems.append(f"{key}: expected {kind.__name__}")
            return default
        return value

    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            problems.append(f"unknown top-level key {key!r}")

    schema = take(data, "schema", int, None)
    if schema is None:
        problems.append("schema: required (use schema = 1)")
    elif schema not in SUPPORTED_SCHEMAS:
        problems.append(
            f"schema: {schema} is not supported by this phoson "
            f"(supported: {sorted(SUPPORTED_SCHEMAS)})"
        )

    plugin_id = take(data, "id", str, "")
    if not plugin_id:
        problems.append("id: required")
    elif not _ID_RE.match(plugin_id):
        problems.append(
            f"id: {plugin_id!r} must be lowercase letters, digits and single dashes"
        )

    version = take(data, "version", str, "")
    if not version:
        problems.append("version: required")
    else:
        try:
            Version(version)
        except InvalidVersion:
            problems.append(f"version: {version!r} is not a valid PEP 440 version")

    entry = take(data, "entry", str, "")
    if not entry:
        problems.append("entry: required (e.g. 'my_package:create_plugin')")
    elif not _ENTRY_RE.match(entry):
        problems.append(f"entry: {entry!r} must look like 'package.module:attribute'")

    authors = take(data, "authors", list, [])
    if not all(isinstance(a, str) for a in authors):
        problems.append("authors: expected a list of strings")
        authors = []

    compat = take(data, "compat", dict, {})
    phoson_spec = take(compat, "phoson", str, "")
    python_spec = take(compat, "python", str, "")
    for label, spec in (("compat.phoson", phoson_spec), ("compat.python", python_spec)):
        if spec:
            try:
                SpecifierSet(spec)
            except InvalidSpecifier:
                problems.append(f"{label}: {spec!r} is not a valid version specifier")
    for key in compat:
        if key not in {"phoson", "python"}:
            problems.append(f"compat: unknown key {key!r}")

    provides_raw = take(data, "provides", dict, {})
    tools = _string_list(provides_raw, "tools", problems)
    commands = _string_list(provides_raw, "commands", problems)
    for command in commands:
        if not command.startswith("/"):
            problems.append(f"provides.commands: {command!r} must start with '/'")
    provides = Provides(
        tools=tuple(tools),
        commands=tuple(commands),
        middlewares=bool(take(provides_raw, "middlewares", bool, False)),
        theme=bool(take(provides_raw, "theme", bool, False)),
    )
    for key in provides_raw:
        if key not in {"tools", "commands", "middlewares", "theme"}:
            problems.append(f"provides: unknown key {key!r}")

    caps_raw = take(data, "capabilities", dict, {})
    filesystem = take(caps_raw, "filesystem", str, "none")
    if filesystem not in _FILESYSTEM_LEVELS:
        problems.append(
            f"capabilities.filesystem: {filesystem!r} must be one of "
            f"{', '.join(_FILESYSTEM_LEVELS)}"
        )
        filesystem = "none"
    capabilities = Capabilities(
        network=bool(take(caps_raw, "network", bool, False)),
        shell=bool(take(caps_raw, "shell", bool, False)),
        filesystem=filesystem,  # type: ignore[arg-type]
    )
    for key in caps_raw:
        if key not in {"network", "shell", "filesystem"}:
            problems.append(f"capabilities: unknown key {key!r}")

    config_raw = take(data, "config", dict, {})
    config_fields: list[ConfigField] = []
    for key, raw in config_raw.items():
        parsed = _parse_config_field(key, raw, problems)
        if parsed is not None:
            config_fields.append(parsed)

    if problems:
        raise PluginManifestError(f"Invalid plugin manifest ({where}):", problems)

    return PluginManifest(
        # Validated above (an int in SUPPORTED_SCHEMAS); narrowed for typing.
        schema=schema if isinstance(schema, int) else 1,
        id=plugin_id,
        name=take(data, "name", str, ""),
        version=version,
        description=take(data, "description", str, ""),
        entry=entry,
        license=take(data, "license", str, ""),
        homepage=take(data, "homepage", str, ""),
        authors=tuple(authors),
        phoson=phoson_spec,
        python=python_spec,
        provides=provides,
        capabilities=capabilities,
        config=tuple(config_fields),
        path=path,
    )


def load_manifest(path: Path) -> PluginManifest:
    """Read and validate a manifest file.

    Raises:
        PluginManifestError: if the file is unreadable, not TOML or invalid.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginManifestError(f"Cannot read plugin manifest {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PluginManifestError(
            f"Invalid TOML in plugin manifest {path}: {exc}"
        ) from exc
    return parse_manifest(data, path=path)


def find_manifest_for_module(module: str) -> Path | None:
    """Locate ``phoson-plugin.toml`` for an importable module, without importing.

    Only the *top-level* package is resolved (``importlib.util.find_spec`` on
    a top-level name never executes code); the manifest must sit at its root.
    Returns ``None`` when the package is not found or ships no manifest.
    """
    top = module.split(".", 1)[0].replace("-", "_")
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    candidates: list[Path] = []
    for location in spec.submodule_search_locations or []:
        candidates.append(Path(location) / MANIFEST_FILENAME)
    if spec.origin and not spec.submodule_search_locations:
        candidates.append(Path(spec.origin).parent / MANIFEST_FILENAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def find_manifest_in_tree(root: Path, *, max_depth: int = 3) -> Path | None:
    """Find a manifest in a plugin *source* tree (repo checkout, scaffold).

    Searches ``root`` and packages below it (``src/<pkg>/`` layouts included)
    up to *max_depth* levels, skipping virtualenvs and VCS/build folders.
    """
    skip = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__"}
    frontier = [root]
    for _depth in range(max_depth + 1):
        next_frontier: list[Path] = []
        for directory in frontier:
            candidate = directory / MANIFEST_FILENAME
            if candidate.is_file():
                return candidate
            try:
                children = sorted(p for p in directory.iterdir() if p.is_dir())
            except OSError:
                continue
            next_frontier.extend(
                child
                for child in children
                if child.name not in skip and not child.name.startswith(".")
            )
        frontier = next_frontier
    return None


def engine_version() -> str | None:
    """Installed ``phoson-engine-minimal`` version, or ``None`` if unknown."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(ENGINE_DISTRIBUTION)
    except PackageNotFoundError:
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _string_list(table: dict[str, Any], key: str, problems: list[str]) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"provides.{key}: expected a list of strings")
        return []
    return value


def _parse_config_field(key: str, raw: Any, problems: list[str]) -> ConfigField | None:
    where = f"config.{key}"
    if not isinstance(raw, dict):
        problems.append(f"{where}: expected a table")
        return None
    allowed = {"type", "description", "default", "required", "secret", "env", "choices"}
    for unknown in set(raw) - allowed:
        problems.append(f"{where}: unknown key {unknown!r}")
    kind = raw.get("type", "string")
    if kind not in _CONFIG_TYPES:
        problems.append(
            f"{where}.type: {kind!r} must be one of {', '.join(sorted(_CONFIG_TYPES))}"
        )
        return None
    env = raw.get("env")
    if env is not None and (not isinstance(env, str) or not _ENV_RE.match(env)):
        problems.append(f"{where}.env: {env!r} must be an UPPER_SNAKE_CASE name")
        env = None
    choices = raw.get("choices", [])
    if not isinstance(choices, list):
        problems.append(f"{where}.choices: expected a list")
        choices = []
    default = raw.get("default")
    if default is not None:
        error = _type_error(default, kind)
        if error:
            problems.append(f"{where}.default: {error}")
        elif choices and default not in choices:
            problems.append(f"{where}.default: {default!r} is not one of its choices")
    for flag in ("required", "secret"):
        if flag in raw and not isinstance(raw[flag], bool):
            problems.append(f"{where}.{flag}: expected boolean")
    if raw.get("secret") and default is not None:
        problems.append(f"{where}: a secret must not ship a default value")
    return ConfigField(
        key=key,
        type=kind,
        description=str(raw.get("description", "")),
        default=default,
        required=bool(raw.get("required", False)),
        secret=bool(raw.get("secret", False)),
        env=env,
        choices=tuple(choices),
    )


def _type_error(value: Any, kind: str) -> str | None:
    ok = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }[kind]
    return None if ok else f"expected {kind}, got {type(value).__name__}"


def _coerce_env(raw: str, kind: str) -> Any:
    """Convert an environment variable to the field type."""
    if kind == "string":
        return raw
    if kind == "integer":
        try:
            return int(raw)
        except ValueError:
            raise ValueError("is not an integer") from None
    if kind == "number":
        try:
            return float(raw)
        except ValueError:
            raise ValueError("is not a number") from None
    if kind == "boolean":
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError("is not a boolean (true/false)")
    raise ValueError(f"cannot be read from the environment for type {kind}")


__all__ = [
    "ENGINE_DISTRIBUTION",
    "MANIFEST_FILENAME",
    "SUPPORTED_SCHEMAS",
    "Capabilities",
    "ConfigField",
    "PluginManifest",
    "PluginManifestError",
    "Provides",
    "engine_version",
    "find_manifest_for_module",
    "find_manifest_in_tree",
    "load_manifest",
    "parse_manifest",
]
