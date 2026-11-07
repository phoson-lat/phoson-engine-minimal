"""Plugin authoring tools: ``plugin new`` (scaffold) and ``plugin validate``.

Both work on a plugin *source tree* (a repository checkout), not on an
installed plugin. ``validate`` reads the manifest and ``pyproject.toml``
without importing the plugin; ``validate --load`` additionally imports it
(no ``initialize()``) and compares what it really provides with what the
manifest declares.
"""

import re
import sys
import tomllib
from pathlib import Path
from dataclasses import field, dataclass

from phoson_agent import Plugin, PluginManifest, PluginManifestError, load_manifest
from phoson_agent.plugin_loader import _resolve_plugin, _sys_path_guard
from phoson_agent.plugin_manifest import (
    MANIFEST_FILENAME,
    engine_version,
    find_manifest_in_tree,
)

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_ENTRY_POINT_GROUP = "phoson.plugins"


class PluginDevError(Exception):
    """A user-actionable authoring failure (bad id, existing directory...)."""


# ── Scaffold ─────────────────────────────────────────────────────────────────


def _names(plugin_id: str) -> tuple[str, str, str, str]:
    """``(distribution, package, class name, tool name)`` for *plugin_id*."""
    base = plugin_id.removeprefix("phoson-plugin-")
    distribution = f"phoson-plugin-{base}"
    package = distribution.replace("-", "_")
    class_name = "".join(part.capitalize() for part in base.split("-")) + "Plugin"
    tool = base.replace("-", "_") + "_hello"
    return distribution, package, class_name, tool


def _engine_floor() -> str:
    version = engine_version() or "0.58"
    major, minor, *_rest = (version.split(".") + ["0", "0"])[:3]
    return f"{major}.{minor}"


def scaffold_plugin(plugin_id: str, parent: Path) -> Path:
    """Create a ready-to-install plugin project for *plugin_id* under *parent*.

    The project contains a manifest, a ``pyproject.toml`` with the
    ``phoson.plugins`` entry point, one example tool and slash command, a
    README with the development loop and a test. Returns the project root.
    """
    if not _ID_RE.match(plugin_id):
        raise PluginDevError(
            f"Invalid plugin id {plugin_id!r}: use lowercase letters, digits and "
            "single dashes (e.g. 'acme-notes')."
        )
    distribution, package, class_name, tool = _names(plugin_id)
    root = parent / distribution
    if root.exists():
        raise PluginDevError(f"{root} already exists")
    floor = _engine_floor()
    command = f"/{plugin_id.removeprefix('phoson-plugin-')}"
    values = {
        "id": plugin_id,
        "distribution": distribution,
        "package": package,
        "class_name": class_name,
        "tool": tool,
        "command": command,
        "floor": floor,
    }
    files = {
        "pyproject.toml": _PYPROJECT,
        "README.md": _README,
        ".gitignore": "__pycache__/\n*.egg-info/\n.venv/\ndist/\nbuild/\n",
        f"src/{package}/__init__.py": _INIT,
        f"src/{package}/plugin.py": _PLUGIN,
        f"src/{package}/{MANIFEST_FILENAME}": _MANIFEST,
        "tests/test_plugin.py": _TEST,
    }
    for relative, template in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(**values), encoding="utf-8")
    return root


_PYPROJECT = """\
[project]
name = "{distribution}"
version = "0.1.0"
description = "A Phoson plugin"
readme = "README.md"
requires-python = ">=3.12"
dependencies = ["phoson-engine-minimal>={floor}"]

# The entry point name is the plugin id: it must equal Plugin.name and the
# manifest's `id`.
[project.entry-points."phoson.plugins"]
{id} = "{package}:create_plugin"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

# hatchling ships every file inside the package, phoson-plugin.toml included.
[tool.hatch.build.targets.wheel]
packages = ["src/{package}"]

[dependency-groups]
dev = ["pytest>=8"]
"""

_README = """\
# {distribution}

A [Phoson](https://github.com/phoson-lat/phoson-engine-minimal) plugin.

## Develop

```bash
uv sync                              # creates .venv with phoson + pytest
uv run pytest                        # unit tests
phoson-cli plugin validate .         # manifest + packaging checks (no import)
phoson-cli plugin validate . --load  # also import it and compare with the manifest
phoson-cli plugin install .          # install into ~/.phoson/plugins/{id}
phoson-cli plugin uninstall {id}
```

## What it provides

- Tool `{tool}`: returns a greeting (configurable with `greeting`).
- Slash command `{command}`: shows the plugin status.

Edit `src/{package}/phoson-plugin.toml` whenever you add tools, commands,
configuration keys or capabilities: users review it before installing.

## Configuration

```toml
# ~/.phoson/config.toml
[[defaults.plugins]]
name = "entrypoint:{id}"
config = {{ greeting = "Hola" }}
```
"""

_INIT = """\
from .plugin import {class_name}, create_plugin

__all__ = ["{class_name}", "create_plugin"]
"""

_PLUGIN = """\
\"\"\"{distribution}: a Phoson plugin.\"\"\"

from typing import Any

from phoson_agent import (
    Plugin,
    AgentTool,
    CliCommandSpec,
    CliCommandContext,
    CliCommandInvocation,
    tool,
)


class {class_name}(Plugin):
    def __init__(self) -> None:
        self.greeting = "Hello"

    @property
    def name(self) -> str:
        # Must equal the manifest `id` and the entry point name.
        return "{id}"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "A Phoson plugin"

    def configure(self, config: dict[str, Any]) -> None:
        # The manifest's [config] schema has already validated `config` and
        # filled in defaults/environment values.
        self.greeting = config.get("greeting", self.greeting)

    def get_tools(self) -> list[AgentTool]:
        greeting = self.greeting

        @tool(parallel_safe=True)
        def {tool}(name: str) -> str:
            \"\"\"Greet someone by name.\"\"\"
            return f"{{greeting}}, {{name}}!"

        return [{tool}]

    def get_commands(self) -> list[CliCommandSpec]:
        return [
            CliCommandSpec(
                names=("{command}",),
                help="Show the {id} plugin status",
                handler="handle_status",
                category="Plugins",
            )
        ]

    async def handle_status(
        self, command: CliCommandInvocation, context: CliCommandContext
    ) -> bool:
        context.notify("info", f"{id} is active (greeting: {{self.greeting}})")
        return True


def create_plugin() -> {class_name}:
    return {class_name}()
"""

_MANIFEST = """\
# Phoson plugin manifest. Phoson reads it WITHOUT importing the plugin to list,
# review (before install), validate and configure it.
# Spec: https://github.com/phoson-lat/phoson-engine-minimal/blob/main/docs/research/plugin-manifest-spec.md
schema = 1
id = "{id}"
name = "{distribution}"
version = "0.1.0"                     # keep in sync with pyproject.toml
description = "A Phoson plugin"
entry = "{package}:create_plugin"

[compat]
phoson = ">={floor}"

[provides]
tools = ["{tool}"]
commands = ["{command}"]

# Declare honestly: users see this before installing.
[capabilities]
network = false
shell = false
filesystem = "none"                   # none | read | write

[config.greeting]
type = "string"
description = "Greeting used by the {tool} tool"
default = "Hello"
"""

_TEST = """\
from pathlib import Path

from phoson_agent import load_manifest

from {package} import create_plugin

MANIFEST = Path(__file__).parents[1] / "src" / "{package}" / "phoson-plugin.toml"


def test_manifest_matches_the_plugin() -> None:
    manifest = load_manifest(MANIFEST)
    plugin = create_plugin()
    plugin.configure(manifest.resolve_config({{}}))
    assert plugin.name == manifest.id
    assert [t.name for t in plugin.get_tools()] == list(manifest.provides.tools)
    assert [c.names[0] for c in plugin.get_commands()] == list(
        manifest.provides.commands
    )


def test_tool_uses_configured_greeting() -> None:
    plugin = create_plugin()
    plugin.configure({{"greeting": "Hola"}})
    (greet,) = plugin.get_tools()
    assert greet.handler({{"name": "Ana"}}, None) == "Hola, Ana!"
"""


# ── Validate ─────────────────────────────────────────────────────────────────


@dataclass
class ValidationReport:
    """Result of :func:`validate_plugin_source`."""

    root: Path
    manifest: PluginManifest | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_plugin_source(root: Path, *, load: bool = False) -> ValidationReport:
    """Check a plugin source tree before publishing or installing it.

    Without importing the plugin: the manifest parses; it sits at the root of
    the package its ``entry`` names (where an installed copy will look for
    it); ``pyproject.toml`` declares the matching ``phoson.plugins`` entry
    point, version and phoson dependency. With ``load=True`` the plugin is
    also imported and instantiated (``initialize()`` is not called) to check
    ``Plugin.name`` and that the declared tools/commands match.
    """
    root = root.expanduser().resolve()
    report = ValidationReport(root=root)
    path = find_manifest_in_tree(root)
    if path is None:
        report.errors.append(
            f"no {MANIFEST_FILENAME} found under {root} (run `phoson-cli plugin new` "
            "for a template)"
        )
        return report
    try:
        manifest = load_manifest(path)
    except PluginManifestError as exc:
        report.errors.append(str(exc))
        return report
    report.manifest = manifest

    package_dir = path.parent
    top_package = manifest.entry_module.split(".", 1)[0]
    if package_dir.name != top_package:
        report.errors.append(
            f"{MANIFEST_FILENAME} must be in the '{top_package}' package directory "
            f"named by entry, found it in '{package_dir.name}'"
        )
    if not (package_dir / "__init__.py").is_file():
        report.errors.append(f"{package_dir} is not a package (no __init__.py)")

    _check_pyproject(root, manifest, report)
    reason = manifest.incompatibility(engine_version())
    if reason:
        report.warnings.append(f"cannot be installed here: {reason}")

    if load and report.ok:
        _check_loaded(package_dir.parent, manifest, report)
    return report


def _check_pyproject(
    root: Path, manifest: PluginManifest, report: ValidationReport
) -> None:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        report.warnings.append(
            "no pyproject.toml: the plugin can be loaded with a path: spec but "
            "cannot be installed with `phoson-cli plugin install`"
        )
        return
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        report.errors.append(f"pyproject.toml is not valid TOML: {exc}")
        return
    project = data.get("project", {})
    group = project.get("entry-points", {}).get(_ENTRY_POINT_GROUP, {})
    if manifest.id not in group:
        report.errors.append(
            "pyproject.toml must declare "
            f'[project.entry-points."{_ENTRY_POINT_GROUP}"] '
            f'{manifest.id} = "{manifest.entry}"'
        )
    elif group[manifest.id] != manifest.entry:
        report.errors.append(
            f"entry point {manifest.id} = {group[manifest.id]!r} differs from the "
            f"manifest entry {manifest.entry!r}"
        )
    others = sorted(set(group) - {manifest.id})
    if others:
        report.errors.append(
            f"one plugin per package: extra '{_ENTRY_POINT_GROUP}' entry points: "
            + ", ".join(others)
        )
    version = project.get("version")
    if isinstance(version, str) and version != manifest.version:
        report.warnings.append(
            f"manifest version {manifest.version} differs from pyproject "
            f"version {version}"
        )
    dependencies = project.get("dependencies", [])
    if not any(
        isinstance(dep, str)
        and dep.replace("_", "-").startswith("phoson-engine-minimal")
        for dep in dependencies
    ):
        report.warnings.append(
            "pyproject.toml does not depend on phoson-engine-minimal; declare the "
            "minimum version you need"
        )
    backend = data.get("build-system", {}).get("build-backend", "")
    if backend.startswith("setuptools"):
        report.warnings.append(
            "setuptools does not ship non-Python files by default: add "
            f"{MANIFEST_FILENAME} to [tool.setuptools.package-data]"
        )


def _check_loaded(
    src: Path, manifest: PluginManifest, report: ValidationReport
) -> None:
    import importlib

    from phoson_cli.plugin_manager import provides_mismatches

    module_name = manifest.entry_module
    stale = [
        m
        for m in sys.modules
        if m == module_name.split(".")[0]
        or m.startswith(module_name.split(".")[0] + ".")
    ]
    for name in stale:
        del sys.modules[name]
    try:
        with _sys_path_guard(str(src)):
            module = importlib.import_module(module_name)
            plugin: Plugin = _resolve_plugin(
                getattr(module, manifest.entry_attribute), manifest.entry
            )
            plugin.manifest = manifest
            plugin.configure(manifest.resolve_config({}))
            report.errors.extend(provides_mismatches(plugin))
    except Exception as exc:  # noqa: BLE001 - reported to the author
        report.errors.append(f"loading the plugin failed: {type(exc).__name__}: {exc}")


__all__ = [
    "PluginDevError",
    "ValidationReport",
    "scaffold_plugin",
    "validate_plugin_source",
]
