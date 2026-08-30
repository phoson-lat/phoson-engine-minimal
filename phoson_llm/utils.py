import re
import base64
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Error code emitted when a provider rejects the request because the
#: prompt exceeds the model's context window (HTTP 400 + message match).
#: The summarizer's emergency-compaction interceptor keys off this code
#: (IMPROVEMENTS.md I-91).
CONTEXT_LENGTH_ERROR_CODE = "context_length_exceeded"

#: Phrases providers use when the prompt is too long. Matched case-
#: insensitively against the error message. Kept deliberately broad:
#: OpenAI ("prompt is too long: 199999 tokens > 198000 maximum"),
#: Anthropic ("prompt is too long: 188039 maximum"), vLLM
#: ("This model's maximum context length is 8192 tokens"), OpenRouter
#: ("maximum context length"), Ollama ("context length exceeded"),
#: Groq/Gemini ("exceeds the model's maximum context").
_CONTEXT_LENGTH_PATTERNS: tuple[str, ...] = (
    "prompt is too long",
    "prompt too long",
    "context length",
    "context_length",
    "maximum context",
    "max context",
    "max_model_len",
    "too many tokens",
    "exceeds the model",
    "exceeds the limit",
    "input_tokens",
    "request too large",
)

#: Extracts the provider's stated context window from error messages
#: such as "This model's maximum context length is 8192 tokens" or
#: "prompt is too long: 199999 tokens > 198000 maximum".
_CONTEXT_WINDOW_RE = re.compile(
    r"(?:maximum context length is|context length is|context_length[=: ]+|"
    r"max_model_len[=: ]+|\d[\d,]* tokens > )\s*(\d[\d,]*)",
    re.IGNORECASE,
)


def is_context_length_error(status_code: int | None, message: str) -> bool:
    """Whether a provider error is a context-window overflow (I-91).

    Only HTTP 400 with a matching message qualifies — other 400s
    (bad schema, invalid model, …) must NOT trigger emergency
    compaction.

    Args:
        status_code: The HTTP status of the failed request, if known.
        message: The provider's error message.

    Returns:
        True when the error is identifiable as "context length exceeded".
    """
    if status_code is not None and status_code != 400:
        return False
    text = message.lower()
    return any(pattern in text for pattern in _CONTEXT_LENGTH_PATTERNS)


def extract_context_window(message: str) -> int | None:
    """Parse the context window (tokens) out of a provider error message.

    Used to calibrate the context-window resolver when the model's
    declared window is not in the static registry (I-91): the provider
    usually states the exact limit in the 400 body.

    Returns:
        The parsed window in tokens, or None when the message does not
        state one.
    """
    match = _CONTEXT_WINDOW_RE.search(message)
    if match is None:
        return None
    value = int(match.group(1).replace(",", ""))
    return value if value > 0 else None


def load_file_as_base64(path: str, media_type: str | None = None) -> str | None:
    """
    Reads a local file and encodes it to base64.

    Args:
        path (str): Path to the local file.
        media_type (str | None): Optional MIME type. If not provided, it is guessed.

    Returns:
        str | None: String formatted as 'data:<mime>;base64,<base64_data>' when the
        file can be read. ``None`` (with a logged warning) when the file does not
        exist or cannot be read — the caller should degrade the block to a visible
        text placeholder instead of crashing (I-119: session attachments under
        ``/tmp`` are wiped between runs, so persisted ``file://`` sources may be
        gone by the time a conversation is reloaded).
    """
    if not Path(path).is_file():
        logger.warning(
            "attachment file missing, degrading to text placeholder: %s", path
        )
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        logger.warning(
            "attachment file unreadable (%s: %s), degrading to text placeholder",
            type(exc).__name__,
            path,
        )
        return None
    b64 = base64.b64encode(data).decode("ascii")
    mime = media_type or guess_mime(path)
    return f"data:{mime};base64,{b64}"


def missing_attachment_placeholder(kind: str, path: str) -> str:
    """
    Builds the visible text that replaces a ``file://`` attachment whose file
    no longer exists (I-119).

    The placeholder keeps the file name so the model — and the user — can tell
    *what* was attached even though the content is gone.

    Args:
        kind (str): Human-readable block kind ("image", "audio", "document").
        path (str): The (missing) local path of the attachment.

    Returns:
        str: e.g. ``[image no longer available: shot-accepted.png]``.
    """
    name = Path(path).name or path
    return f"[{kind} no longer available: {name}]"


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
