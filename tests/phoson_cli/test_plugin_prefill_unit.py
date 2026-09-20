"""Unit tests for plugin prompt-prefill plumbing.

``CliCommandContext.prefill_prompt`` lets a plugin command place text in the
user's editable prompt. ``PluginCommandContext`` routes it to the active
front end through ``CommandHost.insert_prompt_text``; both front ends
implement that (classic REPL queues it, full-screen TUI inserts it).
"""

from phoson_cli.commands import PluginCommandContext
from phoson_cli.command_host import RendererCommandHost


class _Host:
    def __init__(self):
        self.text = None

    def insert_prompt_text(self, text):
        self.text = text


class _StreamHost:
    def __init__(self):
        self.calls = []

    def stream_prompt_text(self, text):
        self.calls.append(text)
        return True


class _Repl:
    def __init__(self):
        self.calls = []

    def request_prefill(self, text):
        self.calls.append(text)


def test_plugin_command_context_routes_to_host():
    host = _Host()
    ctx = PluginCommandContext(plugin_name="stt", repl=object(), host=host)

    assert ctx.prefill_prompt("hola") is True
    assert host.text == "hola"


def test_plugin_command_context_reports_unsupported():
    ctx = PluginCommandContext(plugin_name="stt", repl=object(), host=object())

    assert ctx.prefill_prompt("hola") is False


def test_plugin_command_context_swallows_host_errors():
    class _BadHost:
        def insert_prompt_text(self, text):
            raise RuntimeError("boom")

    ctx = PluginCommandContext(plugin_name="stt", repl=object(), host=_BadHost())

    assert ctx.prefill_prompt("hola") is False


def test_plugin_command_context_streams_to_host():
    host = _StreamHost()
    ctx = PluginCommandContext(plugin_name="stt", repl=object(), host=host)

    assert ctx.stream_prompt_text("hola") is True
    assert host.calls == ["hola"]


def test_plugin_command_context_stream_unsupported():
    ctx = PluginCommandContext(plugin_name="stt", repl=object(), host=object())

    assert ctx.stream_prompt_text("hola") is False


def test_renderer_host_queues_prefill_on_the_repl():
    repl = _Repl()

    RendererCommandHost(repl).insert_prompt_text("dictado")

    assert repl.calls == ["dictado"]


def test_fullscreen_host_inserts_into_prompt_buffer():
    from phoson_cli.fullscreen.command_host import FullScreenCommandHost

    class _Buffer:
        def __init__(self):
            self.text = ""

        def insert_text(self, text):
            self.text += text

        def delete_before_cursor(self, count=1):
            self.text = self.text[:-count] if count else self.text

    class _PromptInput:
        def __init__(self):
            self.buffer = _Buffer()

    class _AppApp:
        def __init__(self):
            self.invalidations = 0

        def invalidate(self):
            self.invalidations += 1

        def create_background_task(self, coro):
            coro.close()  # don't run the repaint ticker in a unit test
            return object()

    class _App:
        def __init__(self):
            self._prompt_input = _PromptInput()
            self.app = _AppApp()

    app = _App()
    FullScreenCommandHost(app).insert_prompt_text("dictado")

    assert app._prompt_input.buffer.text == "dictado"
    assert app.app.invalidations == 1


def test_fullscreen_host_replaces_live_preview_segment():
    from phoson_cli.fullscreen.command_host import FullScreenCommandHost

    class _Buffer:
        def __init__(self, text=""):
            self.text = text

        def insert_text(self, text):
            self.text += text

        def delete_before_cursor(self, count=1):
            self.text = self.text[:-count] if count else self.text

    class _PromptInput:
        def __init__(self):
            self.buffer = _Buffer("previo ")

    class _AppApp:
        def __init__(self):
            self.invalidations = 0

        def invalidate(self):
            self.invalidations += 1

        def create_background_task(self, coro):
            coro.close()  # don't run the repaint ticker in a unit test
            return object()

    class _App:
        def __init__(self):
            self._prompt_input = _PromptInput()
            self.app = _AppApp()

    app = _App()
    host = FullScreenCommandHost(app)

    host.stream_prompt_text("hola")
    host.stream_prompt_text("hola mun")  # replaces, does not append
    assert app._prompt_input.buffer.text == "previo hola mun"

    host.stream_prompt_text("")  # clears only its own segment
    assert app._prompt_input.buffer.text == "previo "


def test_ctrl_o_is_bound_to_plugin_dictation():
    """Push-to-talk is a key binding, not a slash command."""
    from phoson_cli.config import KNOWN_KEY_ACTIONS
    from phoson_cli.fullscreen.keys import _ACTION_HANDLERS, DEFAULT_KEY_BINDINGS

    assert DEFAULT_KEY_BINDINGS["dictate"] == ["c-o"]
    assert _ACTION_HANDLERS["dictate"] == "handle_dictate"
    assert "dictate" in KNOWN_KEY_ACTIONS
