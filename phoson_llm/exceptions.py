"""Custom exceptions for phoson_llm.

All exceptions inherit from PhosonLLMError so library consumers can catch
the entire family with a single except clause.

Example:
    try:
        await chat.complete(messages, config)
    except PhosonProviderError as exc:
        # Handle provider-specific errors (rate limits, auth, etc.)
        ...
    except PhosonLLMError:
        # Catch-all for any LLM-related error
        ...
"""


class PhosonLLMError(Exception):
    """Base class for all phoson_llm errors."""


class PhosonLLMProtocolError(PhosonLLMError):
    """Raised when the LLM stream violates the expected event protocol.

    For example, when the stream completes without emitting LLMDoneEvent
    or ErrorEvent.
    """


class PhosonProviderError(PhosonLLMError):
    """Raised when an LLM provider returns an error.

    Wraps provider-specific errors (rate limits, auth failures, server errors)
    behind a uniform interface.

    Attributes:
        code: Internal error code (e.g., 'rate_limit', 'auth', 'overloaded').
        retryable: Whether the operation can be safely retried.
        status_code: Original HTTP status code, if available.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
