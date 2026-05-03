import base64
from pathlib import Path


def load_file_as_base64(path: str, media_type: str | None = None) -> str:
    """
    Lee un archivo local y lo codifica en base64.

    Args:
        path (str): Ruta al archivo local.
        media_type (str | None): Tipo MIME opcional. Si no se proporciona, se adivina.

    Returns:
        str: Cadena formateada como 'data:<mime>;base64,<base64_data>'.
    """
    with open(path, "rb") as f:
        data = f.read()
    b64 = base64.b64encode(data).decode("ascii")
    mime = media_type or guess_mime(path)
    return f"data:{mime};base64,{b64}"


def guess_mime(path: str) -> str:
    """
    Adivina el tipo MIME de un archivo basado en su extensión.

    Args:
        path (str): Ruta al archivo.

    Returns:
        str: Tipo MIME (ej. 'image/png').
    """
    ext = Path(path).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
        ".pdf": "application/pdf",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(ext, "application/octet-stream")


def map_error_code(status_code: int) -> str:
    """
    Mapea códigos de estado HTTP a códigos de error internos de Phoson.

    Args:
        status_code (int): Código de estado HTTP.

    Returns:
        str: Código de error interno (ej. 'rate_limit').
    """
    return {
        401: "auth",
        403: "permission",
        404: "not_found",
        429: "rate_limit",
        500: "server_error",
        503: "overloaded",
        529: "overloaded",
    }.get(status_code, "unknown")
