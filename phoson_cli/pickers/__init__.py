"""Pickers package — shared TUI scaffolding for full-screen prompts.

Concrete pickers (model, provider, session) live in the
``phoson_cli`` top level for backwards compatibility but reuse the
:class:`BasePicker` and palette defined here.
"""

from ._base import BasePicker, picker_style

__all__ = ["BasePicker", "picker_style"]
