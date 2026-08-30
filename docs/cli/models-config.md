# Models and provider configuration files

## `~/.phoson/models.json` (optional)

Holds model overrides (context window, labels — user-defined models
appear in `/model`) and non-sensitive provider settings
(`default_model`, `base_url` for self-hosted/proxied endpoints). Model
listings are always fetched live — a bare `/model` shows one unified
picker of every configured provider (OpenRouter ordered by
`agentic_index`), and a provider whose fetch fails is marked `unavailable`
instead of silently degrading. API keys never live there; see
[docs/api/phoson_cli.md](../api/phoson_cli.md).

## `~/.phoson/config.toml`

- `model` / `provider` — persisted by `/model` and `/provider`
  (`/model` infers the provider from the chosen model and persists the
  pair; a provider without credentials is rejected).
- `theme = "dark|light|ansi|no-color"` — same tiers as
  `PHOSON_THEME`; `NO_COLOR` / `CLICOLOR=0` always produce plain output.
- `[keys]` — key binding remaps (see [keybindings.md](keybindings.md)).
- `[defaults]` — compaction knobs
  (see [compaction.md](compaction.md)).

## Themes (light/dark aware)

The first time you run `phoson-cli` without a saved theme, it asks your
terminal for its default background color (`COLORFGBG` env when present,
otherwise a ~150 ms OSC 11 probe that iTerm2, kitty, WezTerm, Alacritty,
ghostty, VS Code and friends answer) and offers to save the matching
tier — `light` or `dark` — as your default. If the terminal can't be
classified it just doesn't ask. `/theme` opens a live-preview picker
(the banner and every token rendered in the tier's own colors) in both
front ends; `/theme <tier>` sets it directly and `/theme list` lists the
four tiers. Switching applies immediately — no restart needed.
