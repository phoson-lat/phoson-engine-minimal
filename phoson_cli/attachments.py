"""
Attachment manager for the CLI.

Handles multimodal files (images, audio, video, PDFs) attached by the user
before sending a message, converting them into appropriate ContentBlocks.
"""

from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import Sequence

from phoson_llm.schemas import (
    AudioBlock,
    ImageBlock,
    VideoBlock,
    DocumentBlock,
)

# ─── Supported File Extensions ──────────────────────────────────────────────

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}


@dataclass
class Attachment:
    """A file attached to the pending message."""

    path: Path
    block: ImageBlock | AudioBlock | VideoBlock | DocumentBlock

    def __str__(self) -> str:
        return f"📎 {self.path.name}"


@dataclass
class AttachmentManager:
    """
    Collection of files to be attached to the next message.

    Used by the REPL to manage multimodal inputs.
    """

    _pending: list[Attachment] = field(default_factory=list)

    def attach(self, path: str) -> None:
        """
        Attach a file, detecting its type by extension.

        Args:
            path: String path to the file.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file extension is not supported.
        """
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")

        suffix = p.suffix.lower()
        block: ImageBlock | AudioBlock | VideoBlock | DocumentBlock

        if suffix in IMAGE_EXTS:
            block = ImageBlock(
                source=f"file://{p}",
                media_type=_suffix_to_mime(suffix),
            )
        elif suffix in AUDIO_EXTS:
            block = AudioBlock(
                source=f"file://{p}",
                format=suffix[1:],  # remove dot
            )
        elif suffix in VIDEO_EXTS:
            block = VideoBlock(source=f"file://{p}")
        elif suffix == ".pdf":
            block = DocumentBlock(source=f"file://{p}")
        else:
            raise ValueError(
                f"Unsupported file type '{suffix}'. "
                f"Supported: images {sorted(IMAGE_EXTS)}, "
                f"audio {sorted(AUDIO_EXTS)}, video {sorted(VIDEO_EXTS)}, .pdf"
            )

        self._pending.append(Attachment(path=p, block=block))

    def flush(
        self,
    ) -> Sequence[ImageBlock | AudioBlock | VideoBlock | DocumentBlock]:
        """Return pending blocks and clear the manager."""
        blocks = [a.block for a in self._pending]
        self._pending.clear()
        return blocks

    def clear(self) -> None:
        """Clear all pending attachments without sending."""
        self._pending.clear()

    def list_pending(self) -> list[Attachment]:
        """Return the current list of pending attachments."""
        return list(self._pending)

    def __len__(self) -> int:
        return len(self._pending)

    def __bool__(self) -> bool:
        return bool(self._pending)


def _suffix_to_mime(suffix: str) -> str:
    """Map file extension to MIME type."""
    return {
        # Images
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
        # Audio
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        # Video
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
    }.get(suffix, "application/octet-stream")
