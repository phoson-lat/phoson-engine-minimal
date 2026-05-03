from dataclasses import dataclass

from prompt_toolkit.styles import Style
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import HSplit, Window

from .model_selector import ModelOption


@dataclass
class ModelPickerResult:
    model_id: str | None = None
    cancelled: bool = False


_MODEL_PICKER_STYLE = Style.from_dict(
    {
        "title": "bold #b57bee",
        "header": "#808080",
        "row.selected": "bg:#3d2b6e bold #ffffff",
        "row": "#9a8faa",
        "row.active": "bold #00ff9c",
        "footer": "#5a5a5a",
        "search": "bold #e0d0ff",
        "search.label": "#b57bee bold",
        "search.hint": "#6f6780",
        "empty": "#ff9aa2",
    }
)


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value.ljust(width)
    return value[: width - 1] + "…"


def _format_context_length(value: int | None) -> str:
    if not value:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


def _format_meta(model: ModelOption) -> str:
    parts: list[str] = []
    if model.pricing:
        parts.append(model.pricing)
    if model.description:
        parts.append(model.description)
    elif model.provider:
        parts.append(model.provider)
    return " · ".join(parts)


def _fuzzy_score(query: str, text: str) -> int | None:
    if not query:
        return 0

    query = query.lower()
    text = text.lower()

    pos = -1
    score = 0
    consecutive_bonus = 0

    for char in query:
        next_pos = text.find(char, pos + 1)
        if next_pos == -1:
            return None

        score += 1
        if next_pos == pos + 1:
            consecutive_bonus += 3
        else:
            consecutive_bonus += max(0, 2 - (next_pos - pos - 1))

        if next_pos == 0 or text[next_pos - 1] in "-_/ .":
            score += 4

        pos = next_pos

    return score + consecutive_bonus - max(0, len(text) - len(query)) // 12


def _filter_models(models: list[ModelOption], query: str) -> list[ModelOption]:
    if not query.strip():
        return list(models)

    scored: list[tuple[int, ModelOption]] = []
    for model in models:
        haystack = " ".join(
            part
            for part in [model.id, model.label, model.provider, model.description]
            if part
        )
        score = _fuzzy_score(query, haystack)
        if score is not None:
            scored.append((score, model))

    scored.sort(key=lambda item: (-item[0], item[1].id.lower()))
    return [model for _, model in scored]


def _render_models(
    models: list[ModelOption],
    current_model: str,
    selected: int,
    page: int,
    page_size: int,
    query: str = "",
) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    lines.append(("class:title", "  Available Models\n"))
    lines.append(("class:search.label", "  Search: "))
    lines.append(("class:search", query or ""))
    if not query:
        lines.append(("class:search.hint", "type to fuzzy filter"))
    lines.append(("", "\n"))
    lines.append(
        (
            "class:header",
            f"  {'#':>3}  {'Model':<38} {'Ctx':>7}  {'Pricing / Meta':<44}\n",
        )
    )
    lines.append(("class:header", "  " + "─" * 102 + "\n"))

    if not models:
        lines.append(("class:empty", "  No models match the current filter.\n"))
        lines.append(("\n", ""))
        lines.append(
            (
                "class:footer",
                "  type to search  ·  Backspace delete  ·  Esc cancel\n",
            )
        )
        return lines

    start = page * page_size
    end = min(start + page_size, len(models))

    for i in range(start, end):
        model = models[i]
        idx = i + 1
        is_current = model.id == current_model
        is_selected = i == selected

        if is_selected:
            style = "class:row.selected"
        elif is_current:
            style = "class:row.active"
        else:
            style = "class:row"

        marker = "▸" if is_selected else ("▶" if is_current else " ")
        context = _format_context_length(model.context_length)
        meta = _format_meta(model)
        line = (
            f"  {marker} {idx:>2}  {_truncate(model.id, 38)} {context:>7}"
            f"  {_truncate(meta, 44)}\n"
        )
        lines.append((style, line))

    total_pages = (len(models) + page_size - 1) // page_size
    page_info = f" Page {page + 1}/{total_pages} " if total_pages > 1 else ""
    lines.append(("\n", ""))
    lines.append(
        (
            "class:footer",
            "  "
            f"{page_info}↑/↓ navigate  ·  type to fuzzy filter  ·  "
            "Enter select  ·  Esc cancel\n",
        )
    )
    return lines


async def pick_model(
    models: list[ModelOption],
    current_model: str,
    page_size: int = 12,
) -> ModelPickerResult:
    if not models:
        return ModelPickerResult(cancelled=True)

    query = ""
    filtered_models = list(models)
    selected = next(
        (i for i, model in enumerate(filtered_models) if model.id == current_model),
        0,
    )
    page = selected // page_size

    def _refresh_selection() -> None:
        nonlocal filtered_models, selected, page
        filtered_models = _filter_models(models, query)
        if not filtered_models:
            selected = 0
            page = 0
            return
        selected = next(
            (i for i, model in enumerate(filtered_models) if model.id == current_model),
            0,
        )
        page = selected // page_size

    def _rerender() -> None:
        info_window.content = FormattedTextControl(get_text)

    def get_text() -> list[tuple[str, str]]:
        return _render_models(
            filtered_models,
            current_model,
            selected,
            page,
            page_size,
            query,
        )

    kb = KeyBindings()

    @kb.add("up")
    def _up(_event) -> None:
        nonlocal selected, page
        if selected > 0:
            selected -= 1
            page = selected // page_size
            _rerender()

    @kb.add("down")
    def _down(_event) -> None:
        nonlocal selected, page
        if selected < len(filtered_models) - 1:
            selected += 1
            page = selected // page_size
            _rerender()

    @kb.add("pageup")
    def _pageup(_event) -> None:
        nonlocal selected, page
        if page > 0:
            page -= 1
            selected = page * page_size
            _rerender()

    @kb.add("pagedown")
    def _pagedown(_event) -> None:
        nonlocal selected, page
        total_pages = max(1, (len(filtered_models) + page_size - 1) // page_size)
        if page < total_pages - 1:
            page += 1
            selected = min(page * page_size, len(filtered_models) - 1)
            _rerender()

    @kb.add("backspace")
    def _backspace(_event) -> None:
        nonlocal query
        if not query:
            return
        query = query[:-1]
        _refresh_selection()
        _rerender()

    @kb.add("enter")
    def _select(_event) -> None:
        if not filtered_models:
            return
        app.exit(result=ModelPickerResult(model_id=filtered_models[selected].id))

    @kb.add("escape")
    def _quit(_event) -> None:
        app.exit(result=ModelPickerResult(cancelled=True))

    @kb.add("<any>")
    def _type(_event) -> None:
        nonlocal query
        data = _event.data
        if not data or not data.isprintable() or data in {"\r", "\n"}:
            return
        query += data
        _refresh_selection()
        _rerender()

    info_window = Window(
        content=FormattedTextControl(get_text),
        always_hide_cursor=True,
    )

    app: Application = Application(
        layout=Layout(HSplit([info_window])),
        key_bindings=kb,
        full_screen=True,
        style=_MODEL_PICKER_STYLE,
        mouse_support=False,
    )
    return await app.run_async()
