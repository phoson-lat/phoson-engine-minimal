"""Vision tool: lets the agent actually look at an image file.

Unlike ``read_file`` (text-only), this hands the LLM the image itself as
a content block, not a text description of it — the model can inspect
screenshots, diagrams, or photos directly instead of guessing from the
filename or a caption.
"""

from pathlib import Path

from phoson_agent.tool import tool
from phoson_llm.schemas import ImageBlock
from phoson_agent.models import ImageToolResult
from phoson_cli.attachments import IMAGE_EXTS, _suffix_to_mime

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _view_image(path: str) -> str | ImageToolResult:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return f"File not found: {path}"
    if not p.is_file():
        return f"Not a file: {path}"

    suffix = p.suffix.lower()
    if suffix not in IMAGE_EXTS:
        return f"Unsupported image type '{suffix}'. Supported: {sorted(IMAGE_EXTS)}"

    size = p.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return (
            f"Image too large ({size / 1_048_576:.1f}MB, "
            f"max {MAX_IMAGE_BYTES / 1_048_576:.0f}MB): {path}"
        )

    return ImageToolResult(
        text=f"Viewing image: {p.name} ({size} bytes)",
        image=ImageBlock(source=f"file://{p}", media_type=_suffix_to_mime(suffix)),
    )


@tool
def view_image(path: str) -> str | ImageToolResult:
    """View an image file's actual contents (screenshot, diagram, photo)."""
    return _view_image(path)
