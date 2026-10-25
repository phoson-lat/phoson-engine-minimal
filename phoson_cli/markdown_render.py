"""Rich ``Markdown`` with compact code blocks.

Rich draws every fenced code block as ``Syntax(..., padding=1)``: a blank,
background-filled line above and below the code, on top of the blank line
Markdown already puts between blocks — so each snippet sat in a double gap.
Here code blocks keep a one-column horizontal pad (the background band still
sets them apart) and no vertical pad.

Imported lazily by its callers, like ``rich.markdown`` itself, so front ends
that never render Markdown don't pay for ``markdown_it``.
"""

from rich.syntax import Syntax
from rich.console import Console, RenderResult, ConsoleOptions
from rich.markdown import Markdown as RichMarkdown
from rich.markdown import CodeBlock


class _CompactCodeBlock(CodeBlock):
    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        code = str(self.text).rstrip()
        yield Syntax(
            code, self.lexer_name, theme=self.theme, word_wrap=True, padding=(0, 1)
        )


class Markdown(RichMarkdown):
    """``rich.markdown.Markdown`` with :class:`_CompactCodeBlock` code blocks."""

    elements = {
        **RichMarkdown.elements,
        "fence": _CompactCodeBlock,
        "code_block": _CompactCodeBlock,
    }


__all__ = ["Markdown"]
