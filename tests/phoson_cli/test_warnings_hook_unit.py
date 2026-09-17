"""Unit tests for ``phoson_cli.warnings_hook`` (I-112).

Covers the two hooks and their guards:
1. ``warnings.showwarning`` → the CLI notice printer (stdout, never stderr).
2. root ``logging.Handler`` → ``phoson_*`` ``WARNING+`` → same notice.
Plus: the full-screen mute flag, multi-line sanitizing, and idempotent
install/restore.
"""

import logging
import warnings

import pytest

import phoson_cli.warnings_hook as wh


@pytest.fixture(autouse=True)
def _clean_hook_state():
    """Every test starts with the default printer and the flag off.

    Each test is responsible for its own ``install()``/``restore()`` pairing
    (all wrapped in ``try/finally``); this fixture only re-asserts the two
    module-level mutables so a test cannot leak a custom printer or mute flag.
    """
    wh.reset_notice_printer()
    wh.set_fullscreen_active(False)
    yield
    wh.set_fullscreen_active(False)
    wh.reset_notice_printer()


def _install_and_warn(message: str, *, category=UserWarning) -> None:
    """Install the hook, fire one warning, then restore."""
    restore = wh.install()
    try:
        warnings.warn(message, category, stacklevel=2)
    finally:
        restore()


# ── 1. Hook installed → nothing on stderr, one notice on stdout ─────────────


def test_warning_is_notice_not_stderr(capsys) -> None:
    _install_and_warn("boom happened")
    out, err = capsys.readouterr()
    assert err == ""
    assert "UserWarning: boom happened" in out
    assert out.count("UserWarning: boom happened") == 1
    assert ".py:" not in out


def test_subclass_category_name_is_preserved(capsys) -> None:
    class _Custom(UserWarning):
        pass

    _install_and_warn("special thing", category=_Custom)
    out, err = capsys.readouterr()
    assert err == ""
    assert "_Custom: special thing" in out


# ── 2. Full-screen mute: both hooks are no-ops while active ──────────────────


def test_fullscreen_mutes_warnings(capsys) -> None:
    restore = wh.install()
    try:
        wh.set_fullscreen_active(True)
        warnings.warn("inside alt-screen")
    finally:
        wh.set_fullscreen_active(False)
        restore()
    out, err = capsys.readouterr()
    # Muted: neither stdout notice nor raw stderr.
    assert "inside alt-screen" not in out
    assert "inside alt-screen" not in err


def test_fullscreen_mutes_logging_handler(capsys) -> None:
    restore = wh.install()
    try:
        wh.set_fullscreen_active(True)
        logging.getLogger("phoson_agent.plugins.context_window").warning(
            "inside alt-screen log"
        )
    finally:
        wh.set_fullscreen_active(False)
        restore()
    out, err = capsys.readouterr()
    assert "inside alt-screen log" not in out
    assert "inside alt-screen log" not in err


# ── 3. Sanitize: multi-line message → single notice line ─────────────────────


def test_multiline_message_collapse_to_one_line(capsys) -> None:
    _install_and_warn("line one\n  line two\nline three")
    out, err = capsys.readouterr()
    assert err == ""
    assert "line one line two line three" in out
    # No stray newlines inside the notice payload.
    assert "\nline two" not in out


# ── 5. Logging handler: phoson_* WARNING+ → notice; others skipped ──────────


def test_phoson_logger_warning_becomes_notice(capsys) -> None:
    restore = wh.install()
    try:
        logging.getLogger("phoson_agent.plugins.context_window").warning(
            "Ollama context window lookup failed for %r", "x"
        )
    finally:
        restore()
    out, err = capsys.readouterr()
    assert err == ""
    assert "Ollama context window lookup failed for 'x'" in out


def test_sub_warning_level_is_skipped(capsys) -> None:
    restore = wh.install()
    try:
        logging.getLogger("phoson_cli.repl").info("not a warning")
    finally:
        restore()
    out, err = capsys.readouterr()
    assert "not a warning" not in out
    assert err == ""


def test_third_party_logger_is_left_alone(capsys) -> None:
    restore = wh.install()
    try:
        # A non-phoson logger must NOT be turned into a CLI notice.
        logging.getLogger("some_third_party").warning("third party warn")
    finally:
        restore()
    out, err = capsys.readouterr()
    # Our handler skipped it; no raw stderr from lastResort either, because
    # the test environment's logging setup handles it — but crucially it is
    # NOT emitted by our notice channel.
    assert "third party warn" not in out


# ── 6. Restore is idempotent and unwinds both hooks ─────────────────────────


def test_restore_is_idempotent(capsys) -> None:
    restore = wh.install()
    assert warnings.showwarning is wh._hooked_showwarning
    restore()
    assert warnings.showwarning is not wh._hooked_showwarning
    # Second call is a safe no-op (no crash, state stays unwound).
    restore()
    assert warnings.showwarning is not wh._hooked_showwarning


def test_double_install_returns_noop_restore() -> None:
    restore1 = wh.install()
    restore2 = wh.install()  # already installed → no-op restore
    try:
        assert wh._installed
    finally:
        restore2()
        restore1()
    assert not wh._installed


# ── notice_printer is mutable (classic REPL points it at print_warn) ─────────


def test_custom_printer_is_used(capsys) -> None:
    calls: list[str] = []
    original = wh.notice_printer
    wh.notice_printer = lambda line: calls.append(line)
    try:
        _install_and_warn("routed via custom printer")
    finally:
        wh.notice_printer = original
    assert calls == ["UserWarning: routed via custom printer"]


def test_reset_notice_printer_restores_default() -> None:
    wh.notice_printer = lambda line: None
    wh.reset_notice_printer()
    assert wh.notice_printer is wh._default_notice_printer


def test_install_restores_preconfigured_printer() -> None:
    def original(line: str) -> None:
        pass

    def active(line: str) -> None:
        pass

    wh.notice_printer = original
    restore = wh.install()
    wh.notice_printer = active

    restore()

    assert wh.notice_printer is original


def test_nested_install_restores_each_printer_scope() -> None:
    def before(line: str) -> None:
        pass

    def outer(line: str) -> None:
        pass

    def inner(line: str) -> None:
        pass

    wh.notice_printer = before
    restore_outer = wh.install()
    wh.notice_printer = outer
    restore_inner = wh.install()
    wh.notice_printer = inner

    restore_inner()
    assert wh.notice_printer is outer
    assert wh._installed
    restore_outer()
    assert wh.notice_printer is before
    assert not wh._installed


def test_nested_fullscreen_routes_restore_outer_printer() -> None:
    outer: list[str] = []
    inner: list[str] = []
    restore = wh.install()
    try:
        wh.set_fullscreen_active(True, outer.append)
        wh.set_fullscreen_active(True, inner.append)
        warnings.warn("inner warning")
        wh.set_fullscreen_active(False)
        warnings.warn("outer warning")
        wh.set_fullscreen_active(False)
    finally:
        restore()

    assert any("inner warning" in line for line in inner)
    assert any("outer warning" in line for line in outer)


def test_capture_warnings_scope_preserves_preexisting_capture() -> None:
    logging.captureWarnings(True)
    try:
        restore = wh.capture_warnings()
        restore()
        assert getattr(logging, "_warnings_showwarning", None) is not None
    finally:
        logging.captureWarnings(False)


def test_fullscreen_captured_warning_redacts_path_and_source_line() -> None:
    notices: list[str] = []
    handler = wh._PhosonNoticeHandler()
    wh.set_fullscreen_active(True, notices.append)
    try:
        handler.emit(
            logging.LogRecord(
                "py.warnings",
                logging.WARNING,
                "/secret/internal.py",
                42,
                "/secret/internal.py:42: UserWarning: degraded\n  source()\n",
                (),
                None,
            )
        )
    finally:
        wh.set_fullscreen_active(False)

    assert notices == ["UserWarning: degraded"]
    assert "/secret" not in notices[0]
    assert "source()" not in notices[0]


def test_fullscreen_captured_custom_category_redacts_path() -> None:
    class Degraded(UserWarning):
        pass

    notices: list[str] = []
    handler = wh._PhosonNoticeHandler()
    wh.set_fullscreen_active(True, notices.append)
    try:
        handler.emit(
            logging.LogRecord(
                "py.warnings",
                logging.WARNING,
                "/secret/internal.py",
                9,
                "/secret/internal.py:9: Degraded: fallback used\n  source()\n",
                (),
                None,
            )
        )
    finally:
        wh.set_fullscreen_active(False)

    assert notices == ["Degraded: fallback used"]
    assert Degraded.__name__ in notices[0]
    assert "/secret" not in notices[0]


def test_nested_install_out_of_order_restore_fully_unwinds() -> None:
    previous_showwarning = warnings.showwarning
    previous_handlers = list(logging.getLogger().handlers)
    restore_outer = wh.install()
    restore_inner = wh.install()

    restore_outer()
    assert wh._installed
    restore_outer()
    restore_inner()
    restore_inner()

    assert not wh._installed
    assert warnings.showwarning is previous_showwarning
    assert logging.getLogger().handlers == previous_handlers
