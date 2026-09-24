"""Tests for the OSC 2 terminal title (session title + working marker)."""

from types import SimpleNamespace

import pytest

from phoson_cli import terminal_title as tt

# ─── format_title ────────────────────────────────────────────────────────────


def test_format_title_falls_back_to_brand():
    assert tt.format_title(None, False) == "phoson-cli"
    assert tt.format_title("", False) == "phoson-cli"
    assert tt.format_title("   ", False) == "phoson-cli"


def test_format_title_uses_session_title():
    assert tt.format_title("Refactor auth", False) == "Refactor auth"


def test_format_title_prefixes_asterisk_while_working():
    assert tt.format_title("Refactor auth", True) == "* Refactor auth"
    assert tt.format_title(None, True) == "* phoson-cli"


def test_format_title_collapses_whitespace():
    assert tt.format_title("  a\n b\tc  ", False) == "a b c"


# ─── set_title / clear_title ─────────────────────────────────────────────────


def _fake_app(record):
    class _Out:
        def set_title(self, text):
            record.append(("set", text))

        def clear_title(self):
            record.append(("clear", None))

    class _App:
        output = _Out()

    return _App()


def test_set_title_uses_running_application_output(monkeypatch):
    record: list = []
    monkeypatch.setattr("prompt_toolkit.application.get_app", lambda: _fake_app(record))
    tt.set_title("Refactor X")
    assert record == [("set", "Refactor X")]


def test_set_title_sanitizes_control_characters(monkeypatch):
    record: list = []
    monkeypatch.setattr("prompt_toolkit.application.get_app", lambda: _fake_app(record))
    tt.set_title("bad\x1b]2;evil\x07\r")
    assert record == [("set", "bad]2;evil")]


def test_set_title_falls_back_to_osc_when_no_app(monkeypatch):
    def _raise():
        raise RuntimeError("no running app")

    monkeypatch.setattr("prompt_toolkit.application.get_app", _raise)

    class _Stream:
        def __init__(self):
            self.data = ""

        def isatty(self):
            return True

        def write(self, text):
            self.data += text

        def flush(self):
            pass

    stream = _Stream()
    monkeypatch.setattr(tt.sys, "stdout", stream)
    tt.set_title("Hello")
    assert stream.data == "\x1b]2;Hello\x07"


def test_set_title_skips_non_tty(monkeypatch):
    def _raise():
        raise RuntimeError("no running app")

    monkeypatch.setattr("prompt_toolkit.application.get_app", _raise)

    class _Stream:
        def __init__(self):
            self.data = ""

        def isatty(self):
            return False

        def write(self, text):  # pragma: no cover - must not be called
            self.data += text

        def flush(self):  # pragma: no cover
            pass

    stream = _Stream()
    monkeypatch.setattr(tt.sys, "stdout", stream)
    tt.set_title("Hello")
    assert stream.data == ""


# ─── PhosonRepl.refresh_terminal_title ───────────────────────────────────────


@pytest.fixture
def _record_set_title(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(tt, "set_title", calls.append)
    return calls


def _fake_repl():
    return SimpleNamespace(
        _controller=SimpleNamespace(is_running=False),
        tree=SimpleNamespace(title=None),
        _terminal_title=None,
    )


def test_refresh_terminal_title_uses_brand_then_dedups(_record_set_title):
    from phoson_cli.repl import PhosonRepl

    repl = _fake_repl()
    PhosonRepl.refresh_terminal_title(repl)
    PhosonRepl.refresh_terminal_title(repl)
    assert _record_set_title == ["phoson-cli"]


def test_refresh_terminal_title_follows_session_title(_record_set_title):
    from phoson_cli.repl import PhosonRepl

    repl = _fake_repl()
    PhosonRepl.refresh_terminal_title(repl)
    repl.tree.title = "Refactor"
    PhosonRepl.refresh_terminal_title(repl)
    assert _record_set_title == ["phoson-cli", "Refactor"]


def test_refresh_terminal_title_marks_working(_record_set_title):
    from phoson_cli.repl import PhosonRepl

    repl = _fake_repl()
    repl.tree.title = "Refactor"
    PhosonRepl.refresh_terminal_title(repl)
    PhosonRepl.refresh_terminal_title(repl, working=True)
    PhosonRepl.refresh_terminal_title(repl, working=False)
    assert _record_set_title == ["Refactor", "* Refactor", "Refactor"]


def test_refresh_terminal_title_reads_controller_state(_record_set_title):
    from phoson_cli.repl import PhosonRepl

    repl = _fake_repl()
    repl.tree.title = "Refactor"
    repl._controller.is_running = True
    PhosonRepl.refresh_terminal_title(repl)
    assert _record_set_title == ["* Refactor"]
