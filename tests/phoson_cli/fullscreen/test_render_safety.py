"""Full-screen rendering safety: widths Rich and prompt_toolkit agree on, and
hyperlinks that can never stay open across a repaint."""

import io
import re
import asyncio

import pytest
from rich.text import Text
from rich.cells import cell_len
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.formatted_text import ANSI, to_formatted_text

from phoson_cli.hyperlinks import OSC8_CLOSE, guard_hyperlinks
from phoson_cli.fullscreen.render import BlockAnsiCache
from phoson_cli.fullscreen.terminal_text import safe_segment_text

TRICKY = {
    "variation_selector": "warn ⚠️ ok ✔️ done",
    "zwj_family": "family 👨‍👩‍👧 end",
    "skin_tone": "thumbs 👍🏽 up",
    "flag": "country 🇲🇽 code",
    "keycap": "press 1️⃣ now",
    "raw_osc": "title \x1b]0;hacked\x07 after",
    "raw_csi": "red \x1b[31mtext\x1b[0m end",
    "bell_backspace": "x\x07y\bz",
    "cjk_combining": "中文 café é",
}


def _ptk_width(text: str) -> int:
    return sum(get_cwidth(c) for c in text)


@pytest.mark.parametrize("sample", sorted(TRICKY))
def test_safe_text_keeps_richs_width_and_drops_controls(sample: str) -> None:
    raw = TRICKY[sample]
    safe = safe_segment_text(raw.replace("\x07", ""))  # Rich drops BEL itself
    assert _ptk_width(safe) == cell_len(raw.replace("\x07", ""))
    assert not any(ord(c) < 32 or ord(c) == 127 for c in safe)


def test_safe_text_specifics() -> None:
    assert safe_segment_text("plain ascii") == "plain ascii"
    assert safe_segment_text("🇲🇽") == "MX"
    assert "after" in safe_segment_text("title \x1b]0;hacked after")
    assert "hacked" not in safe_segment_text("title \x1b]0;hacked\x1b\\ after")
    assert safe_segment_text("中文 é") == "中文 é"


@pytest.mark.parametrize("sample", sorted(TRICKY))
def test_rendered_lines_have_the_same_width_in_prompt_toolkit(sample: str) -> None:
    width = 40
    rendered = BlockAnsiCache().get_or_render(Text(TRICKY[sample]), width)
    for line in rendered.split("\n"):
        fragments = to_formatted_text(ANSI(line))
        visible = "".join(t for style, t in fragments if "ZeroWidthEscape" not in style)
        rich_width = cell_len(re.sub(r"\x1b\[[0-9;]*m", "", line))
        assert _ptk_width(visible) == rich_width
        assert not any(ord(c) < 32 for c in visible)


def test_bash_preview_keeps_what_a_carriage_return_left() -> None:
    from phoson_cli.theme import load_theme
    from phoson_cli.formatting import _bash_output_body

    rendered = _bash_output_body("progress 10%\rprogress 100%", load_theme())
    text = "".join(getattr(r, "plain", "") for r in rendered)
    assert "progress 100%" in text and "10%" not in text


# ─── Hyperlink guard ─────────────────────────────────────────────────────────


class _FakeOutput:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def write_raw(self, data: str) -> None:
        self.calls.append(f"raw:{data}")

    def write(self, data: str) -> None:
        self.calls.append(f"write:{data}")

    def cursor_forward(self, n: int) -> None:
        self.calls.append(f"forward:{n}")

    def flush(self) -> None:
        self.calls.append("flush")


def test_guard_closes_an_open_link_before_moving_or_flushing() -> None:
    out = guard_hyperlinks(_FakeOutput())
    out.write_raw("\x1b]8;id=1;https://x\x1b\\")
    out.write("link")
    out.cursor_forward(3)
    out.write("plain")
    out.flush()
    assert out.calls == [
        "raw:\x1b]8;id=1;https://x\x1b\\",
        "write:link",
        f"raw:{OSC8_CLOSE}",
        "forward:3",
        "write:plain",
        "flush",
    ]


def test_guard_is_quiet_when_links_are_closed_and_idempotent() -> None:
    out = guard_hyperlinks(_FakeOutput())
    assert guard_hyperlinks(out) is out
    out.write_raw("\x1b]8;id=1;https://x\x1b\\")
    out.write("a")
    out.write_raw(OSC8_CLOSE)
    out.write("\r\n")
    out.flush()
    assert out.calls.count(f"raw:{OSC8_CLOSE}") == 1


def test_partial_repaint_never_writes_other_text_inside_a_link() -> None:
    """Real prompt_toolkit renderer: frame 2 rewrites only the first cell of
    a link, so without the guard the footer is written while it is open."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.application import Application
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout.controls import FormattedTextControl

    link_open = "\x1b]8;id=1;https://x.example\x1b\\"
    state = {"frame": 0}

    def body():
        first = "L" if state["frame"] == 0 else "K"
        return ANSI(f"see \x01{link_open}\x02{first}ink\x01{OSC8_CLOSE}\x02 tail\n")

    buf = io.StringIO()
    output = guard_hyperlinks(
        Vt100_Output(buf, lambda: Size(rows=5, columns=40), term="xterm-256color")
    )

    async def run() -> None:
        with create_pipe_input() as pipe:
            app = Application(
                layout=Layout(
                    HSplit(
                        [
                            Window(FormattedTextControl(body), height=1),
                            Window(
                                FormattedTextControl(
                                    lambda: f"footer {state['frame']}"
                                ),
                                height=1,
                            ),
                        ]
                    )
                ),
                output=output,
                input=pipe,
            )

            async def drive() -> None:
                await asyncio.sleep(0.05)
                buf.truncate(0)
                buf.seek(0)
                state["frame"] = 1
                app.invalidate()
                await asyncio.sleep(0.05)
                app.exit()

            asyncio.get_running_loop().create_task(drive())
            await app.run_async()

    asyncio.run(run())

    is_open = False
    inside: list[str] = []
    for token in re.split(
        r"(\x1b\]8;[^\x1b]*\x1b\\|\x1b\[[0-9;?]*[A-Za-z])", buf.getvalue()
    ):
        if token.startswith("\x1b]8;"):
            is_open = token != OSC8_CLOSE
        elif token and not token.startswith("\x1b") and is_open:
            inside.append(token)
    assert inside and all(set(t) <= set("seKLink- ") for t in inside), inside
