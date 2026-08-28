"""Model picker — interactive fuzzy-search selector for available models."""

from typing import TypedDict
from dataclasses import dataclass
from collections.abc import Callable

from .theme import Theme
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
    #: Provider of the selected option (I-113 unified picker). Lets
    #: commands switch (model, provider) together via the I-89 path.
    provider: str | None = None


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
    *,
    current_provider: str = "",
    unavailable: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Render one page.

    ``current_provider`` is only used for the "active" marker when it is
    not empty (multi-provider view: the current *pair* marks the row).
    ``unavailable`` — ``(provider, error)`` rows for providers whose live
    listing failed (I-113) — renders as a non-navigable section below the
    selectable rows.
    """
    multi = bool(current_provider)
    model_width = 34 if multi else 38
    meta_width = 38 if multi else 44
    title = "  Models\n" if multi else "  Available Models\n"

    lines: list[tuple[str, str]] = []
    lines.append(("class:title", title))
    lines.append(("class:search.label", "  Search: "))
    lines.append(("class:search", query or ""))
    if not query:
        lines.append(("class:search.hint", "type to fuzzy filter"))
    lines.append(("", "\n"))
    lines.append(
        (
            "class:header",
            f"  {'#':>3}  {'Model':<{model_width}} {'Ctx':>7}"
            f"  {'Pricing / Meta':<{meta_width}}\n",
        )
    )
    lines.append(("class:header", "  " + "─" * 102 + "\n"))

    if not models:
        lines.append(("class:empty", "  No models match the current filter.\n"))
    else:
        start = page * page_size
        end = min(start + page_size, len(models))

        for i in range(start, end):
            model = models[i]
            idx = i + 1
            is_current = (
                (model.id == current_model and model.provider == current_provider)
                if multi
                else (model.id == current_model)
            )
            is_selected = i == selected

            if is_selected:
                style = "class:row.selected"
            elif is_current:
                style = "class:row.active"
            else:
                style = "class:row"

            marker = "▸" if is_selected else ("▶" if is_current else " ")
            context = _format_context_length(model.context_length)
            model_field = f"{model.id} ({model.provider})" if multi else model.id
            meta = _format_meta(model)
            line = (
                f"  {marker} {idx:>2}  {_truncate(model_field, model_width)}"
                f" {context:>7}  {_truncate(meta, meta_width)}\n"
            )
            lines.append((style, line))

    if unavailable:
        lines.append(("", "\n"))
        for provider, error in unavailable:
            lines.append(
                (
                    "class:empty",
                    f"  ⚠ {provider} — unavailable: {error}\n",
                )
            )

    if not models:
        lines.append(("\n", ""))
        lines.append(
            (
                "class:footer",
                "  type to search  ·  Backspace delete  ·  Esc cancel\n",
            )
        )
        return lines

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
    theme: "Theme | None" = None,
    *,
    current_provider: str = "",
    unavailable: list[tuple[str, str]] | None = None,
) -> ModelPickerResult:
    """Show an interactive picker over ``models`` with fuzzy search.

    ``current_provider`` (when set) switches the picker into the unified
    multi-provider layout (I-113): rows show ``id (provider)`` and the
    current *(model, provider)* pair is marked.
    """
    if not models:
        return ModelPickerResult(cancelled=True)
    picker = build_model_picker(
        models,
        current_model,
        page_size,
        theme,
        current_provider=current_provider,
        unavailable=unavailable,
    )
    return await picker.run()


def build_unified_model_picker(
    models: list[ModelOption],
    current_model: str,
    current_provider: str,
    unavailable: list[tuple[str, str]] | None = None,
    page_size: int = 12,
    theme: "Theme | None" = None,
    *,
    on_done: Callable[[ModelPickerResult], None] | None = None,
    invalidate: Callable[[], None] | None = None,
) -> BasePicker[ModelPickerResult]:
    """Unified multi-provider model picker (I-113).

    ``models`` is a flat, already-ordered list spanning every configured
    provider (active provider first); each row shows ``id (provider)``
    and the active marker matches the current *(model, provider)* pair.
    ``unavailable`` — ``(provider, error)`` for providers whose live
    listing failed — renders as a non-navigable section. Selecting a row
    resolves to ``ModelPickerResult(model_id, provider)`` so the caller
    can switch the (model, provider) pair together.
    """
    return build_model_picker(
        models,
        current_model,
        page_size,
        theme,
        current_provider=current_provider,
        unavailable=unavailable or None,
        on_done=on_done,
        invalidate=invalidate,
    )


def _is_current(model: ModelOption, current_model: str, current_provider: str) -> bool:
    if current_provider:
        return model.id == current_model and model.provider == current_provider
    return model.id == current_model


def build_model_picker(
    models: list[ModelOption],
    current_model: str,
    page_size: int = 12,
    theme: "Theme | None" = None,
    *,
    current_provider: str = "",
    unavailable: list[tuple[str, str]] | None = None,
    on_done: Callable[[ModelPickerResult], None] | None = None,
    invalidate: Callable[[], None] | None = None,
) -> BasePicker[ModelPickerResult]:
    """Build the picker's state/renderer/bindings without running it.

    Lets a host embed it as a Float (:meth:`BasePicker.as_float`) instead
    of it spinning up its own full-screen ``Application`` via ``run()``
    (``pick_model`` above does that for the classic REPL).

    With ``current_provider`` set (unified multi-provider view, I-113)
    rows show ``id (provider)`` and the active marker matches the
    current *(model, provider)* pair; ``unavailable`` renders the
    ``unavailable`` section for failed provider listings.
    """
    initial_selected = next(
        (
            i
            for i, m in enumerate(models)
            if _is_current(m, current_model, current_provider)
        ),
        0,
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
            current_provider=current_provider,
            unavailable=unavailable,
        )

    picker: BasePicker[ModelPickerResult] = BasePicker(
        render=render,
        style=picker_style(theme=theme),
        on_done=on_done,
        invalidate=invalidate,
    )

    def _refresh_selection() -> None:
        filtered = _filter_models(models, state["query"])
        state["filtered"] = filtered
        if not filtered:
            state.update(selected=0, page=0)
            return
        sel = next(
            (
                i
                for i, m in enumerate(filtered)
                if _is_current(m, current_model, current_provider)
            ),
            0,
        )
        state.update(selected=sel, page=sel // page_size)

    def confirm() -> None:
        if not state["filtered"]:
            return
        selected = state["filtered"][state["selected"]]
        picker.done(ModelPickerResult(model_id=selected.id, provider=selected.provider))

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

    return picker
