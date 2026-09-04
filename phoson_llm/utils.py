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
