# phoson_plugin_questions

A bundled Phoson plugin exposing a single **`questions`** tool: an
`AskUserQuestion`-style primitive that lets the agent ask the user one to four
multiple-choice questions in **one interaction** and read their answers.

Each question has:

- a short **`header`** (≈12 chars, rendered as a label),
- the **`question`** body,
- two to four **`options`**, each with a `label` and a `description`,
- an optional **`multiSelect`** flag,
- an implicit **"Other"** free-text fallback.

## Enabling

The plugin is opt-in:

```toml
# ~/.phoson/config.toml (or the project config)
[defaults]
enable_questions = true
# optional
questions_title = "Questions"
```

Environment equivalents: `PHOSON_ENABLE_QUESTIONS`, `PHOSON_QUESTIONS_TITLE`.

## How it works

The tool is host-agnostic and picks the best available path:

1. **Native `ask`** — if the host's `plugin_ui` exposes
   `PluginUiService.ask()` (the classic REPL and the full-screen front end do),
   the whole batch is asked in a single card with native multi-select.
2. **Fallback** — otherwise it composes `select`/`form` calls, so it works on
   older hosts (multi-select degrades to a single choice there).
3. **Non-interactive** — in one-shot/CI hosts the UI is `unavailable`; the tool
   reports that to the model and never reads stdin.

## Front-end keys

**Full-screen** (one card, wizard):

| Key | Action |
| --- | --- |
| `↑`/`↓` | Move between a question's options |
| `1`–`4` | Pick that option directly (`0` opens "Other") |
| `←`/`→` (or `Tab`/`Shift+Tab`) | Go to the previous / next question (selections are kept) |
| `Space` | Toggle a multi-select option |
| `Enter` | Commit the current question and advance (submits on the last one) |
| `s` | Skip the current question |
| `F2` | Submit immediately |
| `Esc` | Cancel the whole batch |

A breadcrumb (`▸current  ✓answered  ·pending`) shows progress across questions.

**Classic REPL** (sequential numbered prompts): type a number (or `1,3` for
multi-select), `0` for "Other", `s` to skip, `b` to go back, blank to skip;
`Esc`/Ctrl+C cancels.

## Tool schema

```jsonc
{
  "questions": [
    {
      "question": "Which database should we use?",
      "header": "DB",
      "multiSelect": false,
      "options": [
        { "label": "Postgres", "description": "robust, featureful" },
        { "label": "SQLite",   "description": "simple, embedded" }
      ]
    }
  ]
}
```

The result is returned to the model as a compact `header: answer` summary, with
`Other: <text>` for free-text answers.

## Configuration

| Key                  | Env                        | Default       | Meaning                       |
| -------------------- | -------------------------- | ------------- | ----------------------------- |
| `enable_questions`   | `PHOSON_ENABLE_QUESTIONS`  | `false`       | Enable the plugin             |
| `questions_title`    | `PHOSON_QUESTIONS_TITLE`   | `"Questions"` | Title of the questions card   |

The plugin additionally accepts `max_questions` (capped at 4) via its own
`configure()`.
