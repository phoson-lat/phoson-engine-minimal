"""Unit tests for the shared per-call timeout sanitizer (I-127).

``sanitize_timeout`` turns model-provided timeout overrides (which may be
strings, negatives, NaN or booleans despite the schema saying ``number``)
into ``(effective_seconds, note)``. No upper bound by design.
"""

import math

from phoson_cli.tools._timeouts import sanitize_timeout

DEFAULT = 30.0


class TestSanitizeTimeout:
    def test_valid_float_unchanged(self) -> None:
        assert sanitize_timeout(300.0, DEFAULT) == (300.0, None)

    def test_valid_int_coerced_to_float(self) -> None:
        value, note = sanitize_timeout(120, DEFAULT)
        assert value == 120.0
        assert note is None

    def test_numeric_string_coerced_without_note(self) -> None:
        """Provider quirk: numbers arrive as strings — accept silently."""
        assert sanitize_timeout("45", DEFAULT) == (45.0, None)

    def test_no_upper_bound(self) -> None:
        """Large values are honored as-is (long training/builds are OK)."""
        assert sanitize_timeout(14400, DEFAULT) == (14400.0, None)
        assert sanitize_timeout(1e7, DEFAULT)[1] is None

    def test_negative_falls_back_with_note(self) -> None:
        value, note = sanitize_timeout(-5, DEFAULT)
        assert value == DEFAULT
        assert note is not None and "invalid timeout" in note and "30s" in note

    def test_nan_falls_back_with_note(self) -> None:
        value, note = sanitize_timeout(float("nan"), DEFAULT)
        assert value == DEFAULT
        assert note is not None

    def test_bool_rejected(self) -> None:
        """``float(True) == 1.0`` — a bool is never a meaningful timeout."""
        for b in (True, False):
            value, note = sanitize_timeout(b, DEFAULT)
            assert value == DEFAULT
            assert note is not None

    def test_none_falls_back_with_note(self) -> None:
        value, note = sanitize_timeout(None, DEFAULT)
        assert value == DEFAULT
        assert note is not None

    def test_non_numeric_string_falls_back_with_note(self) -> None:
        value, note = sanitize_timeout("abc", DEFAULT)
        assert value == DEFAULT
        assert note is not None and "abc" in note

    def test_dict_falls_back_with_note(self) -> None:
        value, note = sanitize_timeout({"seconds": 5}, DEFAULT)
        assert value == DEFAULT
        assert note is not None

    # ---- allow_zero (sub-agent semantics: 0 == no timeout) ----

    def test_zero_allowed_when_allow_zero(self) -> None:
        assert sanitize_timeout(0, DEFAULT, allow_zero=True) == (0.0, None)

    def test_zero_rejected_when_not_allowed(self) -> None:
        """bash: an unbounded shell is the most likely hang -> default."""
        value, note = sanitize_timeout(0, DEFAULT)
        assert value == DEFAULT
        assert note is not None

    def test_zero_string_allowed_when_allow_zero(self) -> None:
        assert sanitize_timeout("0", DEFAULT, allow_zero=True) == (0.0, None)

    def test_negative_still_rejected_when_allow_zero(self) -> None:
        value, note = sanitize_timeout(-1, DEFAULT, allow_zero=True)
        assert value == DEFAULT
        assert note is not None

    def test_default_is_untouched(self) -> None:
        """The fallback is the caller's configured default, not a constant."""
        assert sanitize_timeout("bad", 120.5)[0] == 120.5


def test_nan_is_captured_by_comparisons_note() -> None:
    """Sanity: the NaN branch matters because `not nan > 0` is True-ish
    logic traps; verify NaN never slips through as a valid timeout."""
    value, note = sanitize_timeout(math.nan, DEFAULT, allow_zero=True)
    assert value == DEFAULT
    assert note is not None
