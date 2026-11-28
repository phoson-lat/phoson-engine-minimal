import os
import re
import base64
import logging
from typing import TYPE_CHECKING, Any
from pathlib import Path

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

#: Error code emitted when a provider rejects the request because the
#: prompt exceeds the model's context window (HTTP 400 + message match).
#: The summarizer's emergency-compaction interceptor keys off this code
#: (IMPROVEMENTS.md I-91).
CONTEXT_LENGTH_ERROR_CODE = "context_length_exceeded"

#: Error code emitted when a provider rejects the request because a
#: ``tool_result`` references a ``tool_use`` that is no longer in the
#: conversation (HTTP 400 + message match). A compaction that cuts a
#: tool pair (F-10 / #176) produces this; it is *not* a context-length
#: error, so the emergency-compaction rescue must not swallow it.
TOOL_PAIRING_ERROR_CODE = "tool_result_without_tool_use"

#: Phrases providers use when a ``tool_result`` is orphaned. Matched
#: case-insensitively against the error message. The check also requires
#: the literal ``tool_result`` so a message that merely mentions
#: ``tool_use`` (e.g. a schema error) is not misclassified. Anthropic:
#: "tool_result ... without tool_use", "does not correspond to a
#: tool_use"; OpenAI-family: "orphan tool_result", "tool_use_id not found".
_TOOL_PAIRING_MISMATCH: tuple[str, ...] = (
    "without tool_use",
    "does not correspond",
    "not correspond",
    "orphan",
    "no matching",
    "not found",
    "missing tool_use",
)


def is_tool_pairing_error(message: str) -> bool:
    """Whether a provider error is an orphaned ``tool_result`` (F-10 / #176).

    A compaction that cuts a tool pair leaves a ``tool_result`` whose
    ``tool_use`` was dropped; the provider answers with a 400 that is
    *not* a context-length error. Detecting it lets the caller surface an
    explicit "history is malformed" message instead of a cryptic 400.

    Args:
        message: The provider's error message.

    Returns:
        True when the message reads as a tool_use/tool_result mismatch.
    """
    text = message.lower()
    if "tool_result" not in text:
        return False
    return any(phrase in text for phrase in _TOOL_PAIRING_MISMATCH)


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
#: such as "This model's maximum context length is 8192 tokens" or
#: "prompt is too long: 199999 tokens > 198000 maximum".
#: Extracts the provider's stated context window from error messages
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
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


_MIME_BY_EXT: dict[str, str] = {
    # Images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    # Video
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    # Audio
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    # Documents
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    # Text-like documents
    ".txt": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


# ─── Document helpers ────────────────────────────────────────────────────────
#
# A ``DocumentBlock`` can carry any file type. Adapters use these helpers to
# decide, per block, whether to send it natively (PDF / office binary) or to
# inline its text (text-like files on providers without a text-document
# input), so the decision is identical across providers.

#: Non-``text/*`` MIME types whose content is plain text.
_TEXTUAL_APPLICATION_MIMES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
        "application/x-sh",
    }
)

#: Cap on how many characters of a text document are inlined into a request.
#: ~25k tokens at 4 chars/token; beyond it a head/tail preview is kept.
MAX_DOCUMENT_TEXT_CHARS = 100_000
_DOCUMENT_TEXT_HEAD_CHARS = 80_000
_DOCUMENT_TEXT_TAIL_CHARS = 10_000

PDF_MIME = "application/pdf"


def _source_path(source: str) -> str:
    """The path component of a ``file://`` or URL source (for name/ext)."""
    if source.startswith("file://"):
        return source[7:]
    if source.startswith("data:"):
        return ""
    from urllib.parse import urlparse

    return urlparse(source).path


def document_media_type(block: Any) -> str:
    """The MIME type of a ``DocumentBlock``.

    Explicit ``media_type`` wins, then the ``data:`` header, then the
    extension. An extension-less source defaults to PDF (the block's
    historical meaning); an unknown extension is ``application/octet-stream``.
    """
    if block.media_type:
        return str(block.media_type)
    source: str = block.source
    if source.startswith("data:"):
        header = source[5:].split(",", 1)[0]
        mime = header.split(";", 1)[0]
        return mime or PDF_MIME
    path = _source_path(source)
    if not Path(path).suffix:
        return PDF_MIME
    return guess_mime(path)


def document_filename(block: Any) -> str:
    """The display filename of a ``DocumentBlock`` (``document`` fallback)."""
    if block.filename:
        return str(block.filename)
    name = Path(_source_path(block.source)).name
    if name:
        return name
    ext = {v: k for k, v in _MIME_BY_EXT.items()}.get(document_media_type(block), "")
    return f"document{ext}"


def is_text_media_type(mime: str) -> bool:
    """Whether *mime* denotes a plain-text format that can be inlined."""
    return mime.startswith("text/") or mime in _TEXTUAL_APPLICATION_MIMES


def _cap_document_text(text: str) -> str:
    if len(text) <= MAX_DOCUMENT_TEXT_CHARS:
        return text
    head = text[:_DOCUMENT_TEXT_HEAD_CHARS]
    tail = text[-_DOCUMENT_TEXT_TAIL_CHARS:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n...[{omitted} chars truncated]...\n{tail}"


def read_document_text(block: Any) -> str | None:
    """The (capped) text content of a text-like ``DocumentBlock``.

    Reads ``file://`` and ``data:`` sources. Returns ``None`` when the
    source is remote (nothing to read locally) or the file is missing —
    callers decide how to degrade (see :func:`document_as_text`).
    """
    source: str = block.source
    if source.startswith("data:"):
        header, _, payload = source.partition(",")
        if ";base64" in header:
            try:
                raw = base64.b64decode(payload)
            except ValueError:
                return None
            text = raw.decode("utf-8", errors="replace")
        else:
            from urllib.parse import unquote

            text = unquote(payload)
        return _cap_document_text(text)
    if not source.startswith("file://"):
        return None
    path = source[7:]
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning(
            "document file missing or unreadable, degrading to placeholder: %s", path
        )
        return None
    return _cap_document_text(text)


def document_as_text(block: Any) -> str:
    """A text rendering of a ``DocumentBlock`` for providers that cannot take
    the file natively.

    Text-like files are inlined under a ``[File: name]`` header; a missing
    local file becomes the I-119 placeholder; anything else (binary types,
    remote URLs) becomes a visible ``[document attached: …]`` note so the
    model knows a file was referenced even though it cannot read it.
    """
    name = document_filename(block)
    mime = document_media_type(block)
    if block.source.startswith("file://") and not Path(block.source[7:]).is_file():
        return missing_attachment_placeholder("document", block.source[7:])
    if is_text_media_type(mime):
        text = read_document_text(block)
        if text is not None:
            return f"[File: {name}]\n{text}"
    location = (
        "" if block.source.startswith(("file://", "data:")) else f" {block.source}"
    )
    return (
        "[document attached but not readable by this provider: "
        f"{name} ({mime}){location}]"
    )


def load_document_bytes(block: Any) -> bytes | None:
    """Raw bytes of a local (``file://``) or inline (``data:``) document.

    ``None`` for remote URLs or a missing/unreadable file.
    """
    source: str = block.source
    if source.startswith("data:"):
        header, _, payload = source.partition(",")
        try:
            return (
                base64.b64decode(payload) if ";base64" in header else payload.encode()
            )
        except ValueError:
            return None
    if not source.startswith("file://"):
        return None
    try:
        return Path(source[7:]).read_bytes()
    except OSError:
        logger.warning(
            "document file missing or unreadable, degrading to placeholder: %s",
            source[7:],
        )
        return None


def document_data_url(block: Any) -> str | None:
    """``data:<mime>;base64,…`` for a local/inline document, else ``None``."""
    if block.source.startswith("data:"):
        return str(block.source)
    data = load_document_bytes(block)
    if data is None:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{document_media_type(block)};base64,{b64}"


# ─── stop_reason normalization (F-13 / #178) ────────────────────────────────
#
# Each provider names its end-of-turn reason differently. Phoson normalizes
# them to a small, stable vocabulary so the agent loop and the UI can reason
# about truncation without per-adapter branching:
#
#     end_turn    normal completion (incl. Anthropic ``stop_sequence``)
#     max_tokens  the response hit its token budget and was cut off
#     tool_use    the turn ended because it issued tool call(s)
#     refusal     the provider refused to answer (content policy)
#     pause_turn  the model paused mid-turn (Anthropic server tools) and will
#                 resume — currently mapped to a terminal pause
#     other       a provider value we do not recognize (better than "other"
#                 than silently treating a truncation as a normal end)

_STOP_REASON_END: str = "end_turn"
_STOP_REASON_MAX: str = "max_tokens"
_STOP_REASON_TOOL: str = "tool_use"
_STOP_REASON_REFUSAL: str = "refusal"
_STOP_REASON_PAUSE: str = "pause_turn"
_STOP_REASON_OTHER: str = "other"

#: Canonical vocabulary (documented order for stable iteration in tests/docs).
STOP_REASONS: tuple[str, ...] = (
    _STOP_REASON_END,
    _STOP_REASON_MAX,
    _STOP_REASON_TOOL,
    _STOP_REASON_REFUSAL,
    _STOP_REASON_PAUSE,
    _STOP_REASON_OTHER,
)

# OpenAI-compatible ``finish_reason`` (OpenAI, OpenRouter, Azure, Groq, ...).
# The legacy ``function_call`` value predates ``tool_calls`` and is treated as
# a tool turn. ``length`` is the truncation signal.
_OPENAI_COMPAT_STOP: dict[str, str] = {
    "stop": _STOP_REASON_END,
    "length": _STOP_REASON_MAX,
    "tool_calls": _STOP_REASON_TOOL,
    "function_call": _STOP_REASON_TOOL,
    "content_filter": _STOP_REASON_REFUSAL,
}

# Anthropic ``stop_reason``. ``stop_sequence`` is a normal stop; ``refusal``
# and (future) ``pause_turn`` are first-class.
_ANTHROPIC_STOP: dict[str, str] = {
    "end_turn": _STOP_REASON_END,
    "stop_sequence": _STOP_REASON_END,
    "max_tokens": _STOP_REASON_MAX,
    "tool_use": _STOP_REASON_TOOL,
    "refusal": _STOP_REASON_REFUSAL,
    "pause_turn": _STOP_REASON_PAUSE,
}

# Ollama ``done_reason`` on the final streaming message.
_OLLAMA_STOP: dict[str, str] = {
    "stop": _STOP_REASON_END,
    "length": _STOP_REASON_MAX,
    "tool_calls": _STOP_REASON_TOOL,
}

# Bedrock Converse ``stop_reason`` (top-level of the response).
_BEDROCK_STOP: dict[str, str] = {
    "end_turn": _STOP_REASON_END,
    "stop_sequence": _STOP_REASON_END,
    "max_tokens": _STOP_REASON_MAX,
    "tool_use": _STOP_REASON_TOOL,
}

# Google Gemini ``FinishReason`` (enum name, e.g. ``"STOP"`` / ``"MAX_TOKENS"``).
# Safety/recitation values are refusal-class: the provider deliberately cut
# the answer, so surface it as a refusal rather than a normal end.
_GEMINI_STOP: dict[str, str] = {
    "STOP": _STOP_REASON_END,
    "MAX_TOKENS": _STOP_REASON_MAX,
    "SAFETY": _STOP_REASON_REFUSAL,
    "RECITATION": _STOP_REASON_REFUSAL,
    "PROHIBITED_CONTENT": _STOP_REASON_REFUSAL,
    "SPII": _STOP_REASON_REFUSAL,
    "IMAGE_SAFETY": _STOP_REASON_REFUSAL,
    "MALFORMED_FUNCTION_CALL": _STOP_REASON_OTHER,
}


def normalize_stop_reason(
    provider_reason: object, *, provider: str = "openai_compat"
) -> str | None:
    """Map a provider-specific stop/finish reason to Phoson's vocabulary.

    See :data:`STOP_REASONS` for the canonical values. Returns ``None`` when
    the provider gave no reason (stream ended without a finish signal) so the
    caller can leave ``LLMDoneEvent.stop_reason`` unset rather than invent a
    value. Unknown reasons normalize to ``"other"`` — never to ``end_turn`` —
    so a truncated/abnormal stop is not mistaken for a clean completion
    (F-13: a ``max_tokens`` truncation must stay distinguishable).

    Args:
        provider_reason: The raw provider value (``finish_reason`` /
            ``stop_reason`` / ``done_reason`` / Gemini ``FinishReason``).
            ``None`` and empty/whitespace strings yield ``None``.
        provider: Which adapter produced the value. Selects the lookup table
            so a value that means different things per provider is normalized
            correctly (e.g. ``"length"`` is truncation for OpenAI-compat and
            Ollama).
    """
    if provider_reason is None:
        return None
    if not isinstance(provider_reason, str):
        # Gemini passes an enum whose ``.name`` is a string; accept both the
        # enum object and its string name.
        name = getattr(provider_reason, "name", None)
        if not isinstance(name, str) or not name:
            return _STOP_REASON_OTHER
        provider_reason = name
    key = provider_reason.strip()
    if not key:
        return None

    if provider == "anthropic":
        table = _ANTHROPIC_STOP
    elif provider == "ollama":
        table = _OLLAMA_STOP
    elif provider == "bedrock":
        table = _BEDROCK_STOP
    elif provider == "google":
        table = _GEMINI_STOP
        key = key.upper()
    else:  # "openai_compat" (default)
        table = _OPENAI_COMPAT_STOP

    return table.get(key, _STOP_REASON_OTHER)


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


# ─── lone-surrogate scrubbing (#270) ─────────────────────────────────────────
#
# ``bytes.decode(errors="surrogateescape")`` maps invalid UTF-8 bytes to the
# lone surrogates ``U+DC80``–``U+DCFF``. Such code points are *not* valid
# UTF-8: when a payload carrying one is serialized with
# ``json.dumps(..., ensure_ascii=False).encode("utf-8")`` (what the OpenAI SDK
# does), the encode raises ``UnicodeEncodeError: ... surrogates not allowed``
# and the whole turn dies. Tool output, subprocess logs and raw file reads are
# the usual source, and once the text is in the message history every
# subsequent turn keeps crashing until it is scrubbed.


def scrub_surrogates(value: Any) -> Any:
    """Recursively replace lone surrogates so the value is UTF-8 encodable.

    Walks ``str``/``dict``/``list``/``tuple`` and rewrites any text that is
    not valid UTF-8 to its lossy ``errors="replace"`` form (each lone
    surrogate becomes ``U+FFFD``). Valid strings are returned unchanged via a
    single ``encode`` fast path, so the common case is cheap. Non-string,
    non-container scalars (int, float, bool, None, ...) pass through as-is.

    Args:
        value: Any JSON-ish value.

    Returns:
        A value of the same shape with lone surrogates removed from all text.
    """
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {scrub_surrogates(k): scrub_surrogates(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_surrogates(v) for v in value]
    return value


def _scrub_text(text: str) -> str:
    """Return ``text`` without lone surrogates (identity when already clean)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")
    return text


# ─── SDK client limits ───────────────────────────────────────────────────────

#: Seconds without a single byte from the provider before a request (or a
#: stream between chunks) is abandoned. The SDK defaults (600 s, retried
#: twice) let one stalled stream freeze a turn for ~30 min (R8).
#: ``PHOSON_LLM_READ_TIMEOUT`` raises it for models that think silently.
DEFAULT_LLM_READ_TIMEOUT_S = 300.0
#: SDK-level retries. ``RetryingChat`` already retries before the first
#: token, so the SDK only covers a single dropped connection.
SDK_MAX_RETRIES = 1


def sdk_timeout() -> "httpx.Timeout":
    """Timeouts for the vendor SDK clients (Anthropic, OpenAI-compatible)."""
    import httpx

    raw = os.environ.get("PHOSON_LLM_READ_TIMEOUT", "")
    try:
        read = float(raw) if raw else DEFAULT_LLM_READ_TIMEOUT_S
    except ValueError:
        read = DEFAULT_LLM_READ_TIMEOUT_S
    if read <= 0:
        read = DEFAULT_LLM_READ_TIMEOUT_S
    return httpx.Timeout(read, connect=15.0, write=60.0, pool=30.0)
