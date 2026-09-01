"""Command palette — fuzzy-search picker for slash commands (T-12).

A single unified picker over all native + plugin slash commands, opened
via ``Ctrl+P``. Selecting a row runs the command immediately (no args);
``enter`` confirms, ``esc`` cancels.

Reuses the shared fuzzy scorer from :mod:`phoson_cli.model_picker` and
the :class:`~phoson_cli.pickers._base.BasePicker` Float-hosting
scaffolding, so the palette opens the same way as the model, theme and
session pickers — as a modal Float on top of the chat pane.
"""

from dataclasses import dataclass

from .theme import Theme
from .pickers import BasePicker, picker_style
from .model_picker import _fuzzy_score

__all__ = ["PaletteEntry", "PalettePickerResult", "build_command_palette"]


# ─── Data types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PaletteEntry:
    """One selectable row in the command palette.

    Attributes:
        name: The canonical slash-command name (e.g. ``/model``).
        display: What the user sees in the row (primary name or alias join).
        help: One-line description.
        category: Help category (``Session``, ``Model``, ``Info``, …).
    """

    name: str
    display: str
    help: str
    category: str = ""


@dataclass
class PalettePickerResult:
    """Result of a palette interaction.

    ``command_name`` is set to the canonical name when the user confirms;
    ``cancelled`` is ``True`` when ``esc`` was pressed (or the list was
    empty at open time).
    """

    command_name: str | None = None
    cancelled: bool = False


# ─── Picker builder ──────────────────────────────────────────────────────────


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value.ljust(width)
    return value[: width - 1] + "…"


def _render_palette(
    entries: list[PaletteEntry],
    selected: int,
    page: int,
    page_size: int,
    query: str,
) -> list[tuple[str, str]]:
    """Render one page of the command palette."""
    lines: list[tuple[str, str]] = []
    lines.append(("class:title", "  Commands\n"))
    lines.append(("class:search.label", "  Search: "))
    lines.append(("class:search", query or ""))
    if not query:
        lines.append(("class:search.hint", "type to fuzzy filter  ·  ctrl+p"))
    lines.append(("", "\n"))

    if not entries:
        lines.append(("class:empty", "  No commands match.\n"))
    else:
        start = page * page_size
        end = min(start + page_size, len(entries))
        for i in range(start, end):
            entry = entries[i]
            is_selected = i == selected
            style = "class:row.selected" if is_selected else "class:row"
            marker = "▸" if is_selected else " "
            name = _truncate(entry.display, 24)
            desc = _truncate(entry.help, 50)
            line = f"  {marker} {name:<26} {desc}\n"
            lines.append((style, line))

    total = len(entries)
    shown_start = page * page_size + 1 if total else 0
    shown_end = min((page + 1) * page_size, total)
    footer = f"  {shown_start}–{shown_end} / {total} commands"
    if page * page_size + page_size < total:
        footer += "  ·  ↓ more"
    footer += "    enter run  ·  esc close"
    lines.append(("class:footer", footer))
    return lines


def _filter_entries(entries: list[PaletteEntry], query: str) -> list[PaletteEntry]:
    """Fuzzy-filter the palette entries against *query* (empty → all)."""
    if not query.strip():
        return list(entries)
    scored: list[tuple[int, PaletteEntry]] = []
    for entry in entries:
        haystack = f"{entry.display} {entry.help}"
        score = _fuzzy_score(query, haystack)
        if score is not None:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1].display.lower()))
    return [e for _, e in scored]


def build_command_palette(
    entries: list[PaletteEntry],
    *,
    theme: Theme | None = None,
    page_size: int = 12,
) -> BasePicker[PalettePickerResult]:
    """Build a command palette picker (not yet running).

    The caller is responsible for hosting it as a Float via
    ``app.run_float_picker(picker)``.

    Args:
        entries: All selectable commands (native + plugin).
        theme: Active theme for the palette style (defaults to current).
        page_size: Rows per page.
    """
    from .theme import load_theme

    resolved_theme = theme or load_theme()

    state: dict = {
        "query": "",
        "filtered": list(entries),
        "selected": 0,
        "page": 0,
    }

    def render() -> list[tuple[str, str]]:
        return _render_palette(
            state["filtered"],
            state["selected"],
            state["page"],
            page_size,
            state["query"],
        )

    picker = BasePicker(
        render=render,
        style=picker_style(theme=resolved_theme),
    )

    def _refresh_selection() -> None:
        filtered = _filter_entries(entries, state["query"])
        state["filtered"] = filtered
        if not filtered:
            state.update(selected=0, page=0)
            return
        sel = min(state["selected"], len(filtered) - 1)
        state.update(selected=sel, page=sel // page_size)

    def confirm() -> None:
        if not state["filtered"]:
            return
        entry = state["filtered"][state["selected"]]
        picker.done(PalettePickerResult(command_name=entry.name))

    def backspace() -> None:
        if not state["query"]:
            return
        state["query"] = state["query"][:-1]
        _refresh_selection()
        picker.refresh()

    def on_type(data: str) -> None:
        state["query"] += data
        _refresh_selection()
        picker.refresh()

    picker.bind_paged_nav(
        get_len=lambda: len(state["filtered"]),
        get_sel=lambda: state["selected"],
        set_sel=lambda i: state.update(selected=i),
        get_page=lambda: state["page"],
        set_page=lambda p: state.update(page=p),
        page_size=page_size,
        on_enter=confirm,
        on_cancel=lambda: picker.done(PalettePickerResult(cancelled=True)),
    )
    picker.bind("backspace", backspace)
    picker.bind_typing(on_type)

    return picker
