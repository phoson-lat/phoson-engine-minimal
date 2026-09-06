"""Custom exceptions for phoson_agent.

All exceptions inherit from PhosonAgentError so library consumers can catch
the entire family with a single except clause.

Example:
    try:
        result = await engine.run(messages, config)
    except PhosonMaxIterationsError:
        # Agent did not converge in the iteration budget
        ...
    except PhosonAgentError:
        # Catch-all for any agent-related error
        ...
"""

from typing import Any


class PhosonAgentError(Exception):
    """Base class for all phoson_agent errors."""


class PhosonAgentRunningError(PhosonAgentError):
    """Raised when attempting to start an agent that is already running.

    AgentEngine instances are not designed for concurrent invocations from
    the same instance. Create a new instance per concurrent run.
    """


class PhosonMaxIterationsError(PhosonAgentError):
    """Raised when the agent exhausts its max_iterations budget.

    Attributes:
        max_iterations: The configured iteration limit.
    """

    def __init__(self, message: str, *, max_iterations: int) -> None:
        super().__init__(message)
        self.max_iterations = max_iterations


class PhosonToolError(PhosonAgentError):
    """Raised when a tool invocation fails inside the agent loop.

    Note: this is normally caught and surfaced as a tool result with
    error=True. It is only raised directly when the failure is unrecoverable.

    Attributes:
        tool_name: Name of the tool that failed.
        tool_call_id: Identifier of the tool call.
    """

    def __init__(
        self,
        message: str,
        *,
        tool_name: str,
        tool_call_id: str,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id


class PhosonToolReturnTypeError(PhosonAgentError, TypeError):
    """Raised when a tool handler returns an unsupported type.

    Tool handlers must return str, dict, or an awaitable of those types.
    """


# ── Doom loop errors (#142) ──────────────────────────────────────────────


class DoomLoopDetectedError(PhosonAgentError):
    """Raised when a doom loop is detected in ``abort`` mode (#142).

    A doom loop is the same tool call (name + normalized args) failing
    ``n`` times in a row. In ``abort`` mode the
    :class:`~phoson_agent.middleware.DoomLoopMiddleware` raises this from
    ``on_before_tool`` to refuse the next identical call. Inside the
    engine the tool runner converts it into an actionable error *result*
    (the run continues, the model sees why the call was refused); direct
    callers of the middleware can catch it to terminate the run.

    Attributes:
        tool_name: Name of the tool that was looping.
    """

    def __init__(self, message: str, *, tool_name: str = "") -> None:
        super().__init__(message)
        self.tool_name = tool_name


# ── Plugin errors ───────────────────────────────────────────────────────


class PhosonPluginError(PhosonAgentError):
    """Base class for plugin-related errors."""


class PhosonPluginLoadError(PhosonPluginError):
    """Raised when a plugin cannot be loaded.

    Wraps ImportError, AttributeError, FileNotFoundError, etc.
    """


class PhosonPluginConfigError(PhosonPluginError):
    """Raised when a plugin specification is malformed."""


class PhosonPluginCleanupError(PhosonPluginError):
    """Raised when one or more plugins fail during cleanup.

    Attributes:
        failures: List of (plugin_name, exception) tuples.
    """

    def __init__(
        self,
        message: str,
        *,
        failures: list[tuple[str, BaseException]],
    ) -> None:
        super().__init__(message)
        self.failures = failures


# ── Session errors ──────────────────────────────────────────────────────


class PhosonSessionError(PhosonAgentError):
    """Base class for session-related errors."""


class PhosonSessionNotFoundError(PhosonSessionError, FileNotFoundError):
    """Raised when a session cannot be located in storage.

    Inherits from FileNotFoundError so callers using stdlib idioms keep
    working. Prefer catching PhosonSessionNotFoundError when possible.

    Attributes:
        session_id: The identifier that was not found.
    """

    def __init__(self, message: str, *, session_id: str) -> None:
        super().__init__(message)
        self.session_id = session_id


class PhosonSessionCorruptError(PhosonSessionError):
    """Raised when a session file exists but cannot be deserialized."""


# ── Helpers ─────────────────────────────────────────────────────────────


def format_agent_error(message: str, *, code: str | None, **extra: Any) -> str:
    """Format an error message with optional code and extra context.

    Used internally to keep AgentErrorEvent messages consistent.
    """
    prefix = f"[{code}] " if code else ""
    suffix = ""
    if extra:
        parts = ", ".join(f"{k}={v}" for k, v in extra.items())
        suffix = f" ({parts})"
    return f"{prefix}{message}{suffix}"
