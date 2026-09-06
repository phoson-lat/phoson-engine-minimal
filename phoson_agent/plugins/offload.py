"""Offload of large tool outputs to disk (IMPROVEMENTS.md E1, F-51).

Pattern after Claude Code: when a tool result exceeds a configurable
size, the full output is written to a file and the context only keeps
a head/tail preview plus the file path. The model can ``read_file`` the
path whenever it needs the full content again, so information is never
lost — it just stops living (expensively) in the context window.

This is deliberately a *middleware*, not logic inside the tools (repo
principle #2): the tool itself is unaware of offloading, which keeps the
philosophy plugin-first and lets Phoson-Core reuse the piece.

Notes:
- Only plain text is offloaded. Results carrying images are left
  untouched — the image block is separate and the text stub is small.
- The file is written lazily, in :meth:`on_after_tool`, which runs after
  the tool handler but before the result is appended to the history.
- Offloaded files accumulate under ``output_dir`` (default
  ``~/.phoson/compacted/``) and are disposable by design. The
  middleware enforces a :class:`RetentionPolicy` (age TTL + total-size
  quota) automatically: every ``check_every_n_writes`` offloads,
  :func:`cleanup_offload_dir` deletes the oldest files first. The
  policy is configurable (``PHOSON_OFFLOAD_TTL_DAYS`` /
  ``PHOSON_OFFLOAD_MAX_MB``) and can be switched off per knob by
  setting it to ``0``.
"""

import time
import hashlib
from pathlib import Path
from dataclasses import dataclass

from phoson_llm.schemas import ToolCallEvent
from phoson_agent.middleware import AgentMiddleware

#: Default offload trigger: results larger than 24 KB.
DEFAULT_MAX_CHARS = 24_000
#: How much of the head/tail to keep in context (characters each).
DEFAULT_HEAD_CHARS = 1_500
DEFAULT_TAIL_CHARS = 500

DEFAULT_OUTPUT_DIR = Path("~/.phoson/compacted").expanduser()


@dataclass
class RetentionPolicy:
    """Retention policy for offloaded files (F-51).

    Args:
        max_age_days: TTL in days. Files older than this are deleted.
            ``0`` disables the age-based cleanup.
        max_total_mb: Total size quota in MB. When the directory exceeds
            it, the oldest files are deleted until it is met. ``0``
            disables the quota.
        check_every_n_writes: The middleware runs the cleanup every N
            offloads, not on every write (stat/IO overhead). ``0``
            disables the automatic check (call
            :func:`cleanup_offload_dir` manually).
    """

    max_age_days: int = 7
    max_total_mb: float = 500.0
    check_every_n_writes: int = 50


#: Retention applied by default (7-day TTL, 500 MB quota, check every
#: 50 offloads).
DEFAULT_RETENTION = RetentionPolicy()


def cleanup_offload_dir(
    output_dir: Path,
    policy: RetentionPolicy = DEFAULT_RETENTION,
) -> int:
    """Delete offload files that violate *policy*, oldest first (F-51).

    Two independent rules, applied in this order:

    1. **Age (TTL).** Files whose ``st_mtime`` is older than
       ``max_age_days`` are deleted (skipped when ``max_age_days == 0``).
    2. **Quota.** If the remaining files still exceed ``max_total_mb``,
       the oldest of them are deleted until the quota is met (skipped
       when ``max_total_mb == 0``).

    Safety: only plain files directly inside *output_dir* are ever
    touched — never subdirectories, never anything outside it. All
    failures are swallowed (cleanup is best-effort; it must never break
    a run).

    Returns:
        Number of files actually deleted.
    """
    dir_path = Path(output_dir)
    if not dir_path.is_dir():
        return 0

    # Snapshot size/mtime once per file: deterministic within one run
    # and avoids re-statting files that are about to be unlinked.
    entries: list[tuple[float, int, Path]] = []
    for path in dir_path.iterdir():
        try:
            if not path.is_file():
                continue  # subdirs (and symlinks to dirs) are never touched
            stat = path.stat()
        except OSError:
            continue  # vanished or unreadable — skip
        entries.append((stat.st_mtime, stat.st_size, path))

    if not entries:
        return 0

    now = time.time()

    # 1. Age-based TTL (max_age_days == 0 → disabled).
    ttl_deleted: set[Path] = set()
    if policy.max_age_days > 0:
        cutoff = now - policy.max_age_days * 86_400
        for mtime, _, path in entries:
            if mtime < cutoff:
                ttl_deleted.add(path)

    # 2. Quota: delete oldest-first until under max_total_mb (0 → off).
    #    The quota is evaluated on the bytes that survive the TTL pass,
    #    so files already condemned by age don't count against it.
    to_delete: list[Path] = []
    if policy.max_total_mb > 0:
        quota_bytes = policy.max_total_mb * 1024 * 1024
        remaining = sorted(
            (e for e in entries if e[2] not in ttl_deleted), key=lambda e: e[0]
        )
        total_bytes = sum(size for _, size, _ in remaining)
        for mtime, size, path in remaining:
            if total_bytes <= quota_bytes:
                break
            to_delete.append(path)
            total_bytes -= size
    to_delete.extend(ttl_deleted)

    deleted = 0
    for path in to_delete:
        try:
            path.unlink()
            deleted += 1
        except OSError:
            pass  # best-effort: a vanished file is not an error
    return deleted


def build_offload_stub(
    *,
    tool_name: str,
    tool_call_id: str,
    original_chars: int,
    path: str,
    head: str,
    tail: str,
    error: bool,
) -> str:
    """Render the placeholder text that replaces an offloaded result.

    Pure function so the exact shape of the stub is unit-testable and
    stable for the model to learn.
    """
    lines = [
        f"[Large {tool_name} output offloaded to disk: {original_chars} chars]",
        f"Full output: {path}",
        "Use read_file on that path to retrieve the full content.",
    ]
    if head:
        lines.append(f"--- head ({len(head)} chars) ---")
        lines.append(head)
    if tail:
        lines.append(f"--- tail ({len(tail)} chars) ---")
        lines.append(tail)
    lines.append("---")
    if error:
        lines.append("Result marked as an error by the tool.")
    return "\n".join(lines)


def offload_output(
    text: str,
    *,
    tool_name: str,
    tool_call_id: str,
    output_dir: Path,
    max_chars: int,
    head_chars: int,
    tail_chars: int,
    error: bool,
) -> str:
    """Offload *text* to disk and return the context stub.

    Returns *text* unchanged when it is at or under ``max_chars`` or
    when the file cannot be written (offloading is a best-effort
    optimization; never break the run because of it).
    """
    if len(text) <= max_chars:
        return text

    digest = hashlib.sha256(
        f"{tool_call_id}:{tool_name}:{text[:1000]}".encode()
    ).hexdigest()[:16]
    safe_tool = "".join(c if c.isalnum() or c in "-_." else "_" for c in tool_name)[:32]
    head = text[:head_chars]
    tail = text[-tail_chars:] if len(text) > head_chars + tail_chars else ""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{safe_tool}_{tool_call_id}_{digest}.txt"
        path.write_text(text, encoding="utf-8")
    except OSError:
        # Offloading is best-effort; never break the run because of it.
        return text

    return build_offload_stub(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        original_chars=len(text),
        path=str(path),
        head=head,
        tail=tail,
        error=error,
    )


@dataclass
class OffloadMiddleware(AgentMiddleware):
    """Offloads oversized tool outputs to disk, keeping head/tail + path.

    Retention (F-51): every ``retention.check_every_n_writes`` offloads
    the middleware runs :func:`cleanup_offload_dir` on ``output_dir``
    (TTL + quota, oldest first). Pass ``retention=None`` to keep the
    :data:`DEFAULT_RETENTION` policy.

    Usage:
        offload = OffloadMiddleware(
            max_chars=24_000,
            output_dir=Path("~/.phoson/compacted").expanduser(),
        )
        engine = AgentEngine(
            chat=chat, tools=tools, middlewares=[offload, summarizer],
        )
    """

    max_chars: int = DEFAULT_MAX_CHARS
    head_chars: int = DEFAULT_HEAD_CHARS
    tail_chars: int = DEFAULT_TAIL_CHARS
    output_dir: Path = DEFAULT_OUTPUT_DIR
    retention: RetentionPolicy | None = None

    def __post_init__(self) -> None:
        """Normalize the output directory and resolve the retention policy."""
        self.output_dir = Path(self.output_dir)
        self._retention = self.retention or DEFAULT_RETENTION
        self._write_count = 0

    async def on_after_tool(
        self,
        call: ToolCallEvent,
        result: str,
        error: bool,
    ) -> str:
        """Rewrite oversized tool results as head/tail + file path."""
        out = offload_output(
            result,
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            output_dir=self.output_dir,
            max_chars=self.max_chars,
            head_chars=self.head_chars,
            tail_chars=self.tail_chars,
            error=error,
        )
        # Only a real offload counts as a write: small results and
        # best-effort write failures return the original text unchanged.
        if out != result:
            self._write_count += 1
            n = self._retention.check_every_n_writes
            if n > 0 and self._write_count % n == 0:
                cleanup_offload_dir(self.output_dir, self._retention)
        return out
