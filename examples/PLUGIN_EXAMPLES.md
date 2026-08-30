# Community CLI Plugin Example

`complete_cli_plugin/` is a minimal installable package showing the complete
I-110 plugin contract from one `Plugin` subclass:

- an agent tool (`example_checklist`);
- a slash command (`/example-status`);
- custom tool-card icon/verb;
- the `example-neon` theme;
- a `TodoListBlock` and `ProgressBlock` through `plugin_ui`;
- a host-neutral selector and form through `plugin_ui`;
- an explicit async `aclose()` lifecycle hook.

Install it while developing from this repository:

```bash
phoson-cli plugin install ./examples/complete_cli_plugin
# or the compatibility alias
phoson-cli --install-plugin ./examples/complete_cli_plugin
```

Then select the contributed theme and use the command:

```text
/theme example-neon
/example-status
```

`/example-status` opens a selector and a small form in interactive hosts.
Fullscreen uses modal Floats and the classic REPL uses a numbered prompt plus
sequential fields. In one-shot/CI, interaction returns `unavailable` instead
of reading stdin; the command reports that state and continues safely.

A published plugin declares exactly one entry point in the existing
`phoson.plugins` group:

```toml
[project.entry-points."phoson.plugins"]
community-example = "phoson_plugin_community_example:create_plugin"
```

Plugins execute Python with the same permissions as `phoson-cli`; only install
sources you trust. Pin Git sources to a tag or commit, for example:

```bash
phoson-cli plugin install github:owner/repository@v1.2.0
```
