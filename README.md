# phoson-engine-minimal

Minimal Python runtime for an agent engine.

## Local development

Install dependencies:

```bash
uv sync --dev
```

Install git hooks:

```bash
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type commit-msg
uv run pre-commit install --hook-type pre-push
```

Run checks locally:

```bash
uv run ruff format --check .
uv run ruff check .
uv run python -m compileall phoson_llm
uv run pytest -q
```

## Commit message format

Conventional Commits are enforced through a `commit-msg` hook.

Examples:

- `feat: add streaming chat abstraction`
- `fix: handle unknown model pricing fallback`
- `chore: update pre-commit hook versions`

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`.

## CI and security workflows

- `.github/workflows/ci.yml`: lint, smoke compile, and tests on pull requests and pushes to `main`.
- `.github/workflows/security.yml`: dependency audit and secret scan on pull requests, pushes to `main`, and weekly schedule.
