"""Model picker — interactive fuzzy-search selector for available models."""

from dataclasses import dataclass
from typing import TypedDict

from .pickers import BasePicker, picker_style
from .model_selector import ModelOption


class _PickerState(TypedDict):
    query: str
    filtered: list[ModelOption]
    selected: int
    page: int


@dataclass
class ModelPickerResult:
    model_id: str | None = None
    cancelled: bool = False


# ─── Formatting helpers ──────────────────────────────────────────────────────


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


# ─── Fuzzy search ────────────────────────────────────────────────────────────


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


# ─── Renderer ────────────────────────────────────────────────────────────────


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


# ─── Public entry point ──────────────────────────────────────────────────────


async def pick_model(
    models: list[ModelOption],
    current_model: str,
    page_size: int = 12,
) -> ModelPickerResult:
    """Show an interactive picker over ``models`` with fuzzy search."""
    if not models:
        return ModelPickerResult(cancelled=True)

    initial_selected = next(
        (i for i, m in enumerate(models) if m.id == current_model), 0
    )
    state: _PickerState = {
        "query": "",
        "filtered": list(models),
        "selected": initial_selected,
        "page": initial_selected // page_size,
    }

    def render() -> list[tuple[str, str]]:
        return _render_models(
            state["filtered"],
            current_model,
            state["selected"],
            state["page"],
            page_size,
            state["query"],
        )

    picker: BasePicker[ModelPickerResult] = BasePicker(
        render=render,
        style=picker_style(),
    )

    def _refresh_selection() -> None:
        filtered = _filter_models(models, state["query"])
        state["filtered"] = filtered
        if not filtered:
            state.update(selected=0, page=0)
            return
        sel = next((i for i, m in enumerate(filtered) if m.id == current_model), 0)
        state.update(selected=sel, page=sel // page_size)

    def confirm() -> None:
        if not state["filtered"]:
            return
        picker.done(
            ModelPickerResult(model_id=state["filtered"][state["selected"]].id)
        )

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
        on_cancel=lambda: picker.done(ModelPickerResult(cancelled=True)),
    )
    picker.bind("backspace", backspace)
    picker.bind_typing(on_type)

    return await picker.run()
