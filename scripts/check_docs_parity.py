#!/usr/bin/env python3
"""Verify docs↔GitHub parity.

Key rule (issue #146): any issue marked "✅ cerrado" / "✅ resuelto" /
"✅ shipped" in ROADMAP.md / IMPROVEMENTS.md must be CLOSED on GitHub, and
vice-versa for issues the docs list as open. The case that motivated this
gate: #138 was resolved in code and docs but still open on GitHub.

Usage:
    python scripts/check_docs_parity.py [--dry-run] [--repo-root PATH]

Exit codes:
    0 = ok (or nothing to check / dry-run)
    1 = drift found (blocking)
    2 = infrastructure error (GitHub API, etc.)

Robustness: if a doc does not exist or contains no recognizable issue
links, it is skipped (no crash, no false positive).
"""

import os
import re
import sys
import argparse
from pathlib import Path

import httpx

REPO = "phoson-lat/phoson-engine-minimal"
API_BASE = f"https://api.github.com/repos/{REPO}"
DOCS = ("ROADMAP.md", "IMPROVEMENTS.md")

# Markdown link whose label is "#N" and whose URL is a GitHub issue for the
# SAME number N:  [#138](https://github.com/<owner>/<repo>/issues/138)
# The owner/repo is generic so it does not break if the repo is renamed.
ISSUE_LINK = re.compile(
    r"\[#(\d+)\]\(https://github\.com/[\w.-]+/[\w.-]+/issues/(\d+)\)"
)


class InfrastructureError(RuntimeError):
    """GitHub API unreachable, HTTP != 200/404, or malformed response."""


def _parse_docs(text: str) -> dict[int, bool]:
    """Return ``{issue_number: is_marked_closed}`` from markdown text.

    An issue is "closed" when a line contains a link
    ``[#N](…/issues/N)`` and a ``✅`` marker. Lines without a recognizable
    issue link are ignored (no match → skip, no false positive).
    """
    state: dict[int, bool] = {}
    for line in text.splitlines():
        for m in ISSUE_LINK.finditer(line):
            label, url_num = int(m.group(1)), int(m.group(2))
            if label != url_num:
                # Label and URL disagree → untrustworthy link, skip it.
                continue
            state[label] = "✅" in line
    return state


def parse_roadmap(text: str) -> dict[int, bool]:
    """Parse ROADMAP.md → ``{issue_number: is_marked_closed}``."""
    return _parse_docs(text)


def parse_improvements(text: str) -> dict[int, bool]:
    """Parse IMPROVEMENTS.md → ``{issue_number: is_marked_closed}``."""
    return _parse_docs(text)


def merge_docs(*states: dict[int, bool]) -> dict[int, bool]:
    """Merge several states; an issue counts as closed if ANY doc marks it."""
    merged: dict[int, bool] = {}
    for state in states:
        for num, closed in state.items():
            merged[num] = merged.get(num, False) or closed
    return merged


def check_github(issues: dict[int, bool], token: str) -> dict[int, bool]:
    """Return ``{issue_number: is_closed}`` by querying the GitHub API.

    Issues that 404 (deleted/transferred) are skipped (they are not drift).
    Any other transport/HTTP error raises :class:`InfrastructureError`.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "phoson-docs-parity",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    result: dict[int, bool] = {}
    for num in issues:
        url = f"{API_BASE}/issues/{num}"
        try:
            resp = httpx.get(url, headers=headers, timeout=10)
        except httpx.HTTPError as exc:
            raise InfrastructureError(f"GET {url} failed: {exc}") from exc
        if resp.status_code == 404:
            continue  # deleted/transferred → skip, not drift
        if resp.status_code != 200:
            raise InfrastructureError(f"GET {url} → HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise InfrastructureError(f"GET {url} → invalid JSON: {exc}") from exc
        result[num] = payload.get("state") == "closed"
    return result


def reconcile(docs_state: dict[int, bool], github_state: dict[int, bool]) -> list[str]:
    """Return the list of mismatch messages (empty = in sync)."""
    mismatches: list[str] = []
    for num in sorted(set(docs_state) | set(github_state)):
        docs_closed = docs_state.get(num)
        gh_closed = github_state.get(num)
        if docs_closed is None or gh_closed is None:
            # Not present in both → nothing to compare (e.g. 404 on GitHub).
            continue
        if docs_closed and not gh_closed:
            mismatches.append(f"#{num}: docs mark ✅ closed but GitHub has it OPEN")
        elif not docs_closed and gh_closed:
            mismatches.append(f"#{num}: GitHub has it CLOSED but docs do not mark ✅")
    return mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check docs↔GitHub parity.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only parse the docs; skip verification against the GitHub API.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root where to look for the docs (default: cwd).",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root) if args.repo_root else Path.cwd()
    docs_state: dict[int, bool] = {}
    for name in DOCS:
        path = root / name
        if not path.is_file():
            print(f"[docs-parity] {name} not found — skipping (nothing to check)")
            continue
        state = _parse_docs(path.read_text(encoding="utf-8"))
        print(f"[docs-parity] {name}: {len(state)} issues referenced")
        docs_state = merge_docs(docs_state, state)

    if not docs_state:
        print("[docs-parity] no issue links found in the docs — ok")
        return 0

    token = os.environ.get("GITHUB_TOKEN", "")
    if args.dry_run:
        print("[docs-parity] --dry-run: skipping GitHub verification")
        return 0
    if not token:
        print(
            "[docs-parity] WARNING: GITHUB_TOKEN is not set — "
            "dry-run mode (skipping GitHub verification)"
        )
        return 0

    try:
        github_state = check_github(docs_state, token)
    except InfrastructureError as exc:
        print(f"[docs-parity] ERROR: {exc}", file=sys.stderr)
        return 2

    mismatches = reconcile(docs_state, github_state)
    if mismatches:
        print(f"[docs-parity] {len(mismatches)} drift item(s) found:")
        for msg in mismatches:
            print(f"  - {msg}")
        return 1

    print(f"[docs-parity] ok — {len(docs_state)} issues in sync with GitHub")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
