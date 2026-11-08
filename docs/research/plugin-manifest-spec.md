# Plugin manifest and install model (spec, schema 1)

Status: implemented in `phoson_agent/plugin_manifest.py` (format, discovery,
loader integration) and `phoson_cli/plugin_manager.py` /
`phoson_cli/plugin_dev.py` (install, uninstall, authoring tools).

## Why

Before this spec, everything about a plugin (its name, version, what it
adds, what it needs) lived in Python properties. Reading them meant
importing and instantiating the plugin, so:

- `plugin list` could show only a name;
- `plugin doctor` ran `initialize()`, which has side effects (claiming a peer
  name, opening connections);
- users approved an install without knowing what the plugin does;
- configuration errors surfaced only inside `configure()` at runtime;
- a plugin had up to four unrelated identities: the distribution name, the
  entry point name, `Plugin.name` and the import package;
- installs went into the CLI's own environment, so a plugin could replace
  the CLI's dependencies. `remove` could not delete anything, and the
  standalone binary could not install at all.

## The manifest: `phoson-plugin.toml`

### Location

The manifest lives at the root of the plugin's top-level import package and
ships in the wheel as package data:

```
src/phoson_plugin_acme_notes/
├── __init__.py
├── plugin.py
└── phoson-plugin.toml
```

Phoson finds it with `importlib.util.find_spec(<top-level package>)`. For a
top-level name this resolves the package directory **without executing**
its `__init__`. This works the same for entry-point, `package:`, `path:` and
in-tree plugins, and for the standalone binary (the official manifests are
bundled as data files). For a source tree (`plugin validate`),
`find_manifest_in_tree` also searches `src/` layouts.

hatchling ships it automatically. With setuptools, list it in
`[tool.setuptools.package-data]`; `plugin validate` warns about this.

### One identity

`id` is the plugin's single identity. It must equal:

- the `[project.entry-points."phoson.plugins"]` key (enforced at install and
  by `validate`);
- `Plugin.name` (the loader warns, and `doctor` and `validate --load` report
  a mismatch).

Ids are lowercase letters, digits and single dashes. The distribution and
package names stay free; the scaffold uses `phoson-plugin-<id>` /
`phoson_plugin_<id>`. Each package exposes exactly one plugin.

### Format reference

```toml
schema = 1                          # required; format version
id = "acme-notes"                   # required; see "One identity"
version = "0.1.0"                   # required; PEP 440, same as the package
entry = "phoson_plugin_acme_notes:create_plugin"   # required; module[:attr]
                                    # attr defaults to the module-level `plugin`
name = "Acme notes"                 # optional display name
description = "One line"            # optional
license = "MIT"                     # optional
homepage = "https://…"              # optional
authors = ["Name <mail>"]           # optional

[compat]
phoson = ">=0.58,<1"                # PEP 440 specifier on phoson-engine-minimal
python = ">=3.12"                   # optional

[provides]                          # what the plugin contributes (for review)
tools = ["acme_notes_search"]       # exact tool names; omit dynamic ones
commands = ["/acme"]                # slash commands, leading "/"
middlewares = false
theme = false

[capabilities]                      # declared, shown before install
network = false
shell = false
filesystem = "none"                 # none | read | write

[config.api_url]                    # one table per configuration key
type = "string"                     # string|integer|number|boolean|array|object
description = "Base URL"
default = "https://api.acme.dev"
[config.token]
type = "string"
secret = true                       # never shown; may not have a default
env = "ACME_TOKEN"                  # read from the environment when unset
required = true
[config.mode]
type = "string"
choices = ["fast", "safe"]
default = "safe"
```

Validation is strict and lists every problem in one pass: unknown keys, bad
types, an invalid id, version or specifier, a command without `/`, or a
secret with a default.

### Semantics at load time

When a loaded plugin has a manifest, `PluginRegistry.load`:

1. reads it **before importing the plugin**;
2. refuses an incompatible plugin (`[compat]`) without executing its code;
3. builds the config passed to `configure()`, per key: the explicit value,
   then `env`, then `default`. Unknown keys, wrong types, values outside
   `choices` and missing `required` keys raise `PluginManifestError`, listing
   them all. With no `[config]` table the config passes through untouched
   (the plugin validates it itself, as before);
4. sets `plugin.manifest` and warns when `Plugin.name != id`.

Plugins without a manifest load exactly as before. The manifest is optional
in schema 1; the CLI marks such plugins as undeclared in reviews.

Capabilities are **declarative**. Phoson does not sandbox plugins, which
are Python code running with the user's permissions. The declaration exists
for review and policy, not as a security boundary.

## Install model

### Isolated directories

Each community plugin is installed into its own directory,
`~/.phoson/plugins/<id>/` (override with `PHOSON_PLUGINS_DIR`):

```
uv pip install --target <staging> --python <host python> \
    --constraint <host pins> <requirement>
```

- **Host pins.** Every package importable by the CLI is pinned
  (`name==version`) as a constraint, so a plugin cannot resolve a different
  `anthropic`, `httpx`, … than the host runs. A conflict fails the install
  instead of breaking the CLI. Local, editable and VCS installs are not
  pinned, because an index cannot provide them.
- **Pruning.** After the install, copies of packages the host already has
  are deleted from the plugin directory. Plugin directories are *appended*
  to `sys.path`, so the host's copies always win and the duplicates would be
  dead weight. A plugin directory normally holds just the plugin and any
  dependencies of its own.
- **Activation.** At startup (`build_plugin_specs`) and in plugin commands,
  `activate_plugin_paths()` appends every locked plugin directory to
  `sys.path`, which makes their `phoson.plugins` entry points visible.
  Locations outside the plugins directory are ignored, so a tampered
  lockfile cannot point the CLI at arbitrary code or make `uninstall` delete
  arbitrary paths.
- **Standalone binary.** With no interpreter to hand to the installer, the
  binary uses `uv` with `--python-version <bundled X.Y>`. Without `uv` on
  PATH it provisions a pinned, checksum-verified `uv` into `~/.phoson/bin`
  (`phoson_cli/managed_uv.py`; `PHOSON_NO_MANAGED_UV=1` opts out). Regular installs fall back to `python -m pip --target` when `uv` is
  missing.

### Stage → review → commit

1. Confirm the *source* (building an sdist or a Git checkout runs that
   project's build backend). `--yes` skips this.
2. Install into a staging directory under the plugins directory.
3. Find the single `phoson.plugins` entry point among the new
   distributions and read its manifest from the staged package, without
   importing it. Check that the id matches and that the plugin is compatible.
4. Show the review (identity, description, compat, capabilities, provides,
   config keys, source, the version it replaces, warnings) and confirm.
5. Move staging into place, replacing a previous version only after the new
   one is in position. Enable `entrypoint:<id>` (an existing dict spec with
   its config is kept), and record the install in
   `~/.phoson/plugins.lock.toml`:

```toml
[[plugin]]
id = "acme-notes"
source = "github:acme/notes@v1.2.0"
requirement = "git+https://github.com/acme/notes.git@<sha>"
resolved_commit = "<sha>"
version = "1.2.0"
distribution = "phoson-plugin-acme-notes"
location = "/home/me/.phoson/plugins/acme-notes"
installed_at = "2026-11-07T21:23:40+00:00"
```

Declining at either prompt, or any failure, leaves nothing behind.

### Uninstall and update

- `uninstall <id>` (alias `remove`) deletes the plugin directory, its config
  specs (enabled and disabled) and its lock entry. Legacy installs made into
  the CLI environment by older versions (no `location`) are only removed from
  the configuration, with a note on how to drop the package.
- `update <id>` re-runs the install from the recorded **source**, so a Git
  tag or branch is re-resolved and can move to a newer commit. The old
  behaviour reinstalled the pinned requirement and could never move. Legacy
  installs keep the previous in-place upgrade.

## Authoring workflow

```bash
phoson-cli plugin new acme-notes            # scaffold: manifest, tool, command, tests
cd phoson-plugin-acme-notes && uv sync && uv run pytest
phoson-cli plugin validate .                # static: manifest + pyproject, no import
phoson-cli plugin validate . --load         # also import and compare with [provides]
phoson-cli plugin install .                 # isolated install with review
phoson-cli plugin doctor acme-notes         # load without initialize(); check drift
```

`validate` checks that:

- the manifest parses;
- it sits in the package its `entry` names;
- the package has `__init__.py`;
- `pyproject.toml` declares the matching entry point, and only that one;
- the versions agree;
- a `phoson-engine-minimal` dependency exists;
- the plugin is compatible with the running phoson.

`--load` also checks `Plugin.name` and that the tools and commands match
`[provides]`.

## Official plugins

All twelve in-tree plugins ship a manifest (`phoson_plugin_*/phoson-plugin.toml`).
None declares a `[config]` table, because their configuration is still mapped
from `PhosonConfig` fields by `phoson_cli/official_plugins.py`.
`tests/test_official_plugin_manifests.py` keeps the manifests in sync with
what the plugins provide, and checks that they load with the CLI's real
settings. The STT plugin's `Plugin.name` changed from `stt` to
`phoson-plugin-stt` to follow the single-identity rule.

## Future work

- Per-plugin config through the CLI (`plugin config <id> key value`), with
  secrets stored outside `config.toml`.
- Derive official plugin settings from their manifests instead of the
  hand-written `official_plugins.py` table.
- Let the permission gate default a plugin's tools to `ask` from its
  declared capabilities (`shell`, `filesystem = "write"`).
- A plugin index for search and install by id.
