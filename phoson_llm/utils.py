import base64
from pathlib import Path


def load_file_as_base64(path: str, media_type: str | None = None) -> str:
    """
    Reads a local file and encodes it to base64.

    Args:
        path (str): Path to the local file.
        media_type (str | None): Optional MIME type. If not provided, it is guessed.

    Returns:
        str: String formatted as 'data:<mime>;base64,<base64_data>'.
    """
    with open(path, "rb") as f:
        data = f.read()
    b64 = base64.b64encode(data).decode("ascii")
    mime = media_type or guess_mime(path)
    return f"data:{mime};base64,{b64}"


def guess_mime(path: str) -> str:
    """
    Guesses the MIME type of a file based on its extension.

    Args:
        path (str): Path to the file.

    Returns:
        str: MIME type (e.g., 'image/png').
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
    Maps HTTP status codes to internal Phoson error codes.

    Args:
        status_code (int): HTTP status code.

    Returns:
        str: Internal error code (e.g., 'rate_limit').
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
