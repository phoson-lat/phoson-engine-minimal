# Project memory (AGENTS.md)

Drop an `AGENTS.md` in the repository root (or any directory between the
root and your working directory) and its contents are injected into the
agent's system prompt on every turn — no plugin or database needed. A
global `~/.phoson/AGENTS.md` applies everywhere; `CLAUDE.md` is supported
as an alias; `@path/to/file.md` lines import other files; content is
capped at ~2000 tokens with a visible truncation marker and re-read every
turn. `/agents-md` lists what was loaded.

```markdown
# AGENTS.md

- Use ruff for lint/format and pytest for tests — never black.
- Commit messages follow Conventional Commits.
- Public APIs need type hints and docstrings.
@docs/style-guide.md
```
