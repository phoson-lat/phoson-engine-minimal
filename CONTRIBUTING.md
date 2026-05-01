# Contributing to phoson-engine-minimal

Thank you for your interest in contributing to Phoson! This document outlines the process for contributing to this project.

## Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment. We expect all contributors to:

- Be respectful and inclusive
- Communicate constructively
- Accept criticism gracefully
- Focus on what's best for the community

## How to Contribute

### Reporting Bugs

1. Check if the bug has already been reported
2. Create a new issue with:
   - Clear title and description
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (Python version, OS, etc.)

### Suggesting Features

1. Open a discussion first to gauge interest
2. Create an issue with:
   - Clear description of the feature
   - Use cases
   - Potential implementation approach

### Pull Requests

1. **Fork** the repository
2. **Clone** your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/phoson-engine-minimal.git
   cd phoson-engine-minimal
   ```

3. **Create a feature branch**:
   ```bash
   git checkout -b feature/your-feature-name
   # or
   git checkout -b fix/bug-description
   ```

4. **Make your changes** following our coding standards

5. **Run tests locally**:
   ```bash
   uv run ruff format .
   uv run ruff check .
   uv run pytest -q
   ```

6. **Commit your changes** using Conventional Commits:
   ```
   feat: add new feature
   fix: resolve bug
   docs: update documentation
   refactor: restructure code
   test: add tests
   chore: update dependencies
   ```

7. **Push to your fork**:
   ```bash
   git push origin feature/your-feature-name
   ```

8. **Open a Pull Request** against `main`

## Development Setup

```bash
# Install dependencies
uv sync --dev --locked

# Install git hooks
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type commit-msg
uv run pre-commit install --hook-type pre-push

# Run all checks
uv run ruff format --check .
uv run ruff check .
uv run python -m compileall phoson_llm phoson_agent phoson_cli
uv run pytest -q
```

## Coding Standards

- **Python 3.12+** required
- Use **Ruff** for formatting and linting
- Follow **Conventional Commits** for commit messages
- Write **type hints** for all functions
- Add **docstrings** to public APIs
- Include **tests** for new features

## Project Structure

```
phoson-engine-minimal/
├── phoson_llm/           # LLM adapters and normalization
├── phoson_agent/         # Agent orchestration
├── phoson_cli/           # Interactive CLI
├── tests/                # Test suite
└── .github/workflows/    # CI/CD
```

## Review Process

1. Maintainers will review your PR
2. Address any feedback promptly
3. Once approved, your PR will be merged

## Recognition

Contributors will be recognized in:
- GitHub contributors list
- Release notes (for significant contributions)

---

Questions? Open a discussion or reach out through GitHub Issues.