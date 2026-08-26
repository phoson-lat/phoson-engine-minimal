"""Offload of large tool outputs to disk (IMPROVEMENTS.md E1).

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
  ``~/.phoson/compacted/``); cleanup is a ``rm -rf`` away and the files
  are disposable by design.
"""

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

    def __post_init__(self) -> None:
        """Normalize the output directory."""
        self.output_dir = Path(self.output_dir)

    async def on_after_tool(
        self,
        call: ToolCallEvent,
        result: str,
        error: bool,
    ) -> str:
        """Rewrite oversized tool results as head/tail + file path."""
        return offload_output(
            result,
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            output_dir=self.output_dir,
            max_chars=self.max_chars,
            head_chars=self.head_chars,
            tail_chars=self.tail_chars,
            error=error,
        )
