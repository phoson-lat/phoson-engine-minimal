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
- `theme = "<name>"` — any theme from `/theme list` (tiers, palettes,
  your JSON themes); same values as `PHOSON_THEME`. `NO_COLOR` /
  `CLICOLOR=0` always produce plain output.
- `[theme_overrides]` — recolor individual theme tokens on top of the
  active theme (see [Themes](#themes)).
- `[keys]` — key binding remaps (see [keybindings.md](keybindings.md)).
- `[defaults]` — compaction knobs
  (see [compaction.md](compaction.md)).

## Retrying transient provider errors

A 429/529 (rate limit / overload) or a dropped connection that arrives
*before the first token* used to kill the whole turn. The chat client
built by `build_chat` is wrapped in `RetryingChat`, which re-attempts
transient failures with exponential backoff (1s, ×2, capped at 30s) and
jitter — but **only while no token has been emitted**. Once the stream
commits (first token / reasoning / tool call) it is never re-run, so
partial output is never duplicated.

| Setting | Default | Meaning |
|---|---|---|
| `llm_max_attempts` | `3` | Total tries per LLM call (initial + retries). `1` disables retries. |
| `PHOSON_LLM_MAX_ATTEMPTS` | `3` | Env override for the same. |

Each retry is logged (`phoson_cli.retry` at INFO: "LLM call failed before
the first token; retry N/M after backoff"). Non-retryable errors
(auth, invalid request) and errors *after* the first token are propagated
immediately, not retried.

## Themes

The first time you run `phoson-cli` without a saved theme, it asks your
terminal for its default background color (`COLORFGBG` env when present,
otherwise a ~150 ms OSC 11 probe that iTerm2, kitty, WezTerm, Alacritty,
ghostty, VS Code and friends answer) and offers to save the matching
tier — `light` or `dark` — as your default. If the terminal can't be
classified it just doesn't ask.

`/theme` opens a live-preview picker in both front ends: the banner and
the main tokens render in the selected theme's own colors. `/theme <name>`
switches directly, and `/theme list` lists every theme with your active
overrides. Switching applies immediately, with no restart needed.

### Built-in themes

| Theme | Look |
| --- | --- |
| `system` (default) | Your terminal's own colors |
| `dark` / `light` | Phoson's purple palette |
| `ansi` | 16 colors, safe over SSH |
| `no-color` | Plain text |
| `catppuccin-mocha` / `catppuccin-latte` | Soft pastels, dark / light |
| `dracula` | Purple and pink on dark |
| `nord` | Arctic blues |
| `gruvbox-dark` / `gruvbox-light` | Warm retro |
| `tokyo-night` | Neon blues |
| `solarized-dark` / `solarized-light` | Low-contrast classic |
| `rose-pine` | Muted rose and iris |
| `one-dark` | Atom's balanced dark |

The palette themes use the projects' published colors, so they look best
in a terminal set to the same palette. Some are low-contrast by design,
such as Solarized and the yellow of Catppuccin Latte; an override fixes
that (see below).

### Changing the accent color

```text
/theme accent #ff79c6     # or a name: magenta, plum3, …
/theme accent reset
```

The accent is used for the spinner, focus, prompt arrow, headings and the
selected completion. One color works on every theme: the tokens that
derive from it, such as the softer variant, the selection background and
the user-turn highlight, are tinted from each theme's own background. The
command saves `accent` under `[theme_overrides]` in `config.toml`.

### Overriding individual tokens

```toml
# ~/.phoson/config.toml
[theme_overrides]
accent = "#ff79c6"
warn = "#9a6700"
panel_bg = "on #1b1b2b"
```

Overrides apply on top of whichever theme is active, but never to
`no-color`. Values are Rich styles: colors (`#rrggbb`, `#rgb` or a name),
`bold`, and `on #rrggbb` backgrounds. A token you set explicitly wins over
the one derived from `accent`.

Problems never break startup. An unknown token, a non-string value or a
style the terminal libraries reject is skipped with a warning, and every
other override still applies. `/theme list` shows the active overrides.
After editing the file by hand, run `/theme reload`.

The tokens are the fields of `Theme` in `phoson_cli/theme.py`. The
easiest way to see them all is `/theme new` or `/theme export`.

### Your own themes

| Command | Effect |
| --- | --- |
| `/theme new <name> [--from <theme>]` | Writes `~/.phoson/themes/<name>.json` with every token of the current theme (or `--from`), ready to edit |
| `/theme reload` | Re-reads `~/.phoson/themes/` and `[theme_overrides]` without restarting |
| `/theme export [file]` | Writes the active theme, including your overrides, as JSON (default `./<theme>.phoson-theme.json`) to share it |

A JSON theme has a `name`, a `base` (any built-in theme, used for every
token the file leaves out) and any tokens it changes. Keys starting with
`_` are comments. A JSON theme cannot replace a tier (`system`, `dark`,
`light`, `ansi`, `no-color`), but it can replace a palette: your own
`nord.json` wins over the built-in `nord`.
