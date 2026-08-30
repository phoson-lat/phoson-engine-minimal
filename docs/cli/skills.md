# Skills (on-demand instructions)

A *skill* is a directory with a `SKILL.md` file — YAML frontmatter
(`name` + `description`) followed by Markdown instructions, optionally
next to bundled `scripts/` and `references/`. Unlike `AGENTS.md` (always
in the prompt) or a tool (schema in every request), a skill costs **one
line** while dormant: only its `name: description` is indexed in the
system prompt, and the agent pulls the full body in with the `skill`
tool when it decides the skill matches the task. On this repo's own
skill that is **157 tokens indexed vs 2399 loaded — 15× cheaper** until
it is actually needed, and because the body arrives as a *tool result*
(not in the system prompt), loading a skill mid-session never invalidates
the prompt cache.

```
.phoson/skills/code-reviewer/SKILL.md     # project skill (git-versionable)
~/.phoson/skills/my-workflow/SKILL.md     # available in every repo
```

```markdown
---
name: code-reviewer
description: Use when the user asks to review a diff, a PR or a file for
  bugs, security issues or style violations.
---

# Code reviewer

1. Run `git diff` to see the change.
2. Check for N+1 queries, missing error handling, unvalidated input.
3. Report findings grouped by severity.
```

Project skills shadow same-named global ones. `.agents/skills/` and
`.claude/skills/` are also read, so a repo already set up for another
agent harness works unchanged (same rationale as the `CLAUDE.md` alias).
`/skills` lists what was discovered and `/skills <name>` prints a skill's
full instructions. The `skill` tool only joins the registry when at
least one skill exists — no skills, no added schema on any request.
