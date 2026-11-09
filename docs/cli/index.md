# CLI deep-dive documentation

One-line behavior summaries live in the
[README](../../README.md#interactive-cli); this folder holds the deep
dives.

- [reference.md](reference.md) — every command-line option and slash command (generated)
- [ui.md](ui.md) — full-screen TUI behavior (composer, pickers, activity line)
- [rewind.md](rewind.md) — double-Esc rewind and `Ctrl+Z` undo
- [keybindings.md](keybindings.md) — `[keys]` remaps, reasoning toggle
- [mouse-and-links.md](mouse-and-links.md) — drag-to-copy text selection, OSC 8 hyperlinks
- [prompt-caching.md](prompt-caching.md) — Anthropic / OpenRouter caching and metrics
- [permissions.md](permissions.md) — per-tool allow/ask/deny + allow-patterns
- [agents-md.md](agents-md.md) — project memory injection
- [skills.md](skills.md) — `SKILL.md` on-demand instruction packages
- [background-tasks.md](background-tasks.md) — sub-agents that run while the agent keeps working
- [browser.md](browser.md) — interactive browser use through the official Chrome DevTools MCP server
- [remote.md](remote.md) — `phoson-cli serve`: use this machine from Phoson Desktop or the mobile app (pairing, security, protocol)
- [extras.md](extras.md) — `/extras`: optional features (speech to text, Computer Use, …) installed on demand
- [models-config.md](models-config.md) — `models.json`, `config.toml`, themes
- [compaction.md](compaction.md) — auto-compaction and `[defaults]` knobs
