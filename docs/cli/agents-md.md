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

## Import confinement

`@` imports are **restricted to the file's own tree** so a project file
cannot drag untrusted content into the system prompt (which is sent to the
model provider). A project `AGENTS.md`/`CLAUDE.md` may only import files
inside the repository root; the global `~/.phoson/AGENTS.md` may only import
files inside `~/.phoson/`. Absolute paths, `..` traversal, and symlinks that
escape the tree are refused and replaced with a visible marker:

```
[import refused: outside repo: /etc/passwd]
```

Only the global user file may use a leading `~` in an import; project files
cannot. The per-import result still counts toward the same ~2000-token budget.
