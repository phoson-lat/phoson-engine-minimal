"""
Attachment manager para el CLI.

Maneja archivos multimodales (imágenes, audio, video, PDFs) que el usuario
adjunta antes de enviar un mensaje, convirtiéndolos en ContentBlocks.
"""

from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import Sequence

from phoson_llm.schemas import (
    AudioBlock,
    ImageBlock,
    VideoBlock,
    ContentBlock,
    DocumentBlock,
)

# ─── Tipos de archivos soportados ────────────────────────────────────────────


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}


# ─── Attachment ──────────────────────────────────────────────────────────────


@dataclass
class Attachment:
    """Un archivo adjunto pendiente de incluir en el próximo mensaje."""

    path: Path
    block: ImageBlock | AudioBlock | VideoBlock | DocumentBlock

    def __str__(self) -> str:
        return f"📎 {self.path.name}"


# ─── AttachmentManager ────────────────────────────────────────────────────────


@dataclass
class AttachmentManager:
    """
    Colección de archivos pendientes de adjuntar al próximo mensaje.

    Uso típico en el REPL:

        repl.attachments.attach("screenshot.png")
        repl.attachments.attach("audio.wav")
        blocks = repl.attachments.flush()
        message = Message(role="user", content=list(blocks))
    """

    _pending: list[Attachment] = field(default_factory=list)

    # ── Gestionar attachments ────────────────────────────────────────────────

    def attach(self, path: str) -> None:
        """
        Adjunta un archivo. Detecta el tipo por extensión.

        Raises:
            FileNotFoundError: si el archivo no existe.
            ValueError: si la extensión no es soportada.
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
                format=suffix[1:],  # sin el punto
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

    def flush(self) -> Sequence[ContentBlock]:
        """Retorna los blocks pendientes y los limpia."""
        blocks = [a.block for a in self._pending]
        self._pending.clear()
        return blocks

    def clear(self) -> None:
        """Limpia todos los attachments pendientes sin enviarlos."""
        self._pending.clear()

    def list_pending(self) -> list[Attachment]:
        """Retorna la lista actual de attachments pendientes."""
        return list(self._pending)

    def __len__(self) -> int:
        return len(self._pending)

    def __bool__(self) -> bool:
        return bool(self._pending)


# ─── Helper ───────────────────────────────────────────────────────────────────


def _suffix_to_mime(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
    }.get(suffix, "application/octet-stream")
