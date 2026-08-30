"""Shared validation for model-provided per-call timeout overrides.

The ``bash`` tool and the ``agent``/``agents`` sub-agent tools all expose
an optional ``timeout`` parameter to the LLM. Models are good at following
a numeric schema but not perfect: they occasionally send strings,
negative numbers or ``None``. ``sanitize_timeout`` turns any such input
into a safe value plus a short note that the tool prepends to its result,
so the model learns from the correction on the very next call.

**No upper bound by design.** Long-running jobs (training a model, big
builds, ``pip install``) are legitimate and the owner explicitly wants the
agent to be able to let them run as long as needed. The safety net against
a hung command is the user cancelling the run in flight (Esc), not a cap.
"""

import math

__all__ = ["sanitize_timeout"]


def sanitize_timeout(
    value: object,
    default: float,
    *,
    allow_zero: bool = False,
) -> tuple[float, str | None]:
    """Validate a model-provided timeout override.

    Args:
        value: The raw value from the tool call (may be a string, bool,
            ``None`` or a non-numeric despite the schema saying ``number``).
        default: Fallback in seconds used when the value is unusable.
        allow_zero: When True, ``0`` is accepted and means "no timeout"
            (the sub-agent tools support it; ``bash`` does not, because an
            unbounded shell is the most likely hang).

    Returns:
        ``(effective_seconds, note)``. ``note`` is a short line for the
        tool result, non-None only when the input had to be corrected.
        Numeric strings (``"120"``) are coerced to ``float`` without a
        note — that is a normal, expected provider quirk.
    """
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not a timeout")
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (
            default,
            f"Note: invalid timeout {value!r}; using default {default:g}s.",
        )
    if math.isnan(seconds):
        return default, f"Note: invalid timeout {value!r}; using default {default:g}s."
    if seconds < 0 or (seconds == 0 and not allow_zero):
        if seconds == 0:
            shown = "0 (bash requires a positive timeout)"
        else:
            shown = f"{seconds:g}s"
        return (
            default,
            f"Note: invalid timeout {shown}; using default {default:g}s.",
        )
    return seconds, None
