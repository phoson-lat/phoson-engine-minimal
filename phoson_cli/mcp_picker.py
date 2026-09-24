"""Interactive MCP server / tool toggle picker.

``/mcp toggle`` with no argument opens this picker instead of printing a
usage line: a flat list of every configured server with its tools nested
underneath. ``Enter``/``space`` flips the selected server (or one of its
tools) on or off; the change is persisted immediately through the
``on_toggle`` callback so the toggles survive even if the picker is closed
right after. ``q``/``Esc`` closes the window.

The picker is deliberately dumb about *how* a toggle is persisted: it
receives a synchronous ``on_toggle(server, tool) -> new_state`` callable
and only flips its own view when the callback reports the new state. That
keeps it reusable by both front ends (classic ``run()`` / full-screen
``Float``) and testable without a live MCP runtime.
"""

from dataclasses import field, dataclass
from collections.abc import Callable

from .theme import Theme
from .pickers import BasePicker, picker_style


@dataclass
class McpToolView:
    """One remote tool of a server, with its effective enabled flag."""

    name: str
    enabled: bool = True


@dataclass
class McpServerView:
    """A configured MCP server and the tools known for it."""

    name: str
    transport: str = "stdio"
    target: str = ""
    enabled: bool = True
    tools: list[McpToolView] = field(default_factory=list)


@dataclass
class McpPickerResult:
    """Outcome of an MCP toggle session."""

    cancelled: bool = False
    #: Number of flags flipped while the picker was open.
    changes: int = 0


#: ``on_toggle(server, tool)`` persists one flip and returns the new
#: enabled state (or ``None`` when the flip failed). ``tool=None`` means
#: the whole server.
ToggleFn = Callable[[str, "str | None"], "bool | None"]

_ROW_WIDTH = 68


def _short(text: str, limit: int = 42) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _row_key(row: dict) -> tuple[str, int, int]:
    return (row["kind"], row["s"], row["t"])


def build_mcp_picker(
    servers: list[McpServerView],
    *,
    on_toggle: ToggleFn,
    theme: "Theme | None" = None,
    on_done: Callable[[McpPickerResult], None] | None = None,
    invalidate: Callable[[], None] | None = None,
) -> BasePicker[McpPickerResult]:
    """Build the picker without running it (see module docstring)."""
    rows: list[dict] = []
    for s_idx, server in enumerate(servers):
        rows.append({"kind": "server", "s": s_idx, "t": -1})
        for t_idx in range(len(server.tools)):
            rows.append({"kind": "tool", "s": s_idx, "t": t_idx})

    state: dict = {"selected": 0, "changes": 0, "status": "", "offset": 0}

    def _current() -> tuple[McpServerView, McpToolView | None]:
        row = rows[state["selected"]]
        server = servers[row["s"]]
        tool = server.tools[row["t"]] if row["kind"] == "tool" else None
        return server, tool

    def _viewport_height() -> int:
        """How many rows fit on screen (scroll viewport size).

        Reads the live terminal height so a server with dozens of tools can
        be navigated without the selection walking off the bottom of the
        screen (the Window itself does not scroll a static text control).
        Falls back to "show everything" when no application is running
        (unit tests / direct ``_render`` calls).
        """
        total = len(rows)
        try:
            from prompt_toolkit.application import get_app

            available = get_app().output.get_size().rows
        except Exception:  # noqa: BLE001 - no running app (tests), show all
            return total or 1
        # chrome: title + rule + blank + footer + a 2-line safety margin
        # (the modal Float insets the terminal by one row top/bottom), plus
        # the status block when one is shown.
        chrome = 6 + (2 if state["status"] else 0)
        return max(3, available - chrome)

    def _render() -> list[tuple[str, str]]:
        total = len(rows)
        visible = min(total, _viewport_height())
        offset = state["offset"]
        selected = state["selected"]
        if selected < offset:
            offset = selected
        elif selected >= offset + visible:
            offset = selected - visible + 1
        offset = max(0, min(offset, max(0, total - visible)))
        state["offset"] = offset
        window = rows[offset : offset + visible]

        range_info = (
            f"  {offset + 1}–{offset + len(window)}/{total}" if total > visible else ""
        )
        lines: list[tuple[str, str]] = []
        lines.append(("class:title", f"  MCP Servers{range_info}\n"))
        lines.append(("class:header", "  " + "─" * _ROW_WIDTH + "\n"))

        for i, row in enumerate(window, start=offset):
            is_selected = i == selected
            server = servers[row["s"]]
            if row["kind"] == "server":
                marker = "▸" if is_selected else " "
                bullet = "●" if server.enabled else "○"
                suffix = "" if server.enabled else "  (disabled)"
                line = (
                    f"  {marker} {bullet} {server.name:<16}"
                    f" [{server.transport}]{suffix}"
                )
                target = _short(server.target)
                if target:
                    line += f"   {target}"
                line += "\n"
            else:
                tool = server.tools[row["t"]]
                marker = "▸" if is_selected else " "
                check = "✓" if tool.enabled else "✗"
                line = f"      {marker} {check} {tool.name}\n"
            if is_selected:
                style = "class:row.selected"
            elif not server.enabled:
                style = "class:header"
            else:
                style = "class:row"
            lines.append((style, line))

        if state["status"]:
            lines.append(("\n", ""))
            lines.append(("class:empty", f"  {state['status']}\n"))

        lines.append(("\n", ""))
        lines.append(
            (
                "class:footer",
                "  ↑/↓ navigate  ·  Enter/space toggle  ·  q close\n",
            )
        )
        return lines

    picker: BasePicker[McpPickerResult] = BasePicker(
        render=_render,
        style=picker_style(theme=theme),
        initial=McpPickerResult(cancelled=True),
        on_done=on_done,
        invalidate=invalidate,
    )

    def _toggle() -> None:
        server, tool = _current()
        tool_name = tool.name if tool is not None else None
        label = f"{server.name}/{tool_name}" if tool_name else server.name
        try:
            new_state = on_toggle(server.name, tool_name)
        except Exception as exc:  # noqa: BLE001 - report, never crash the picker
            state["status"] = f"{label}: {exc}"
            picker.refresh()
            return
        if new_state is None:
            state["status"] = f"{label}: could not toggle"
            picker.refresh()
            return
        if tool is not None:
            tool.enabled = new_state
        else:
            server.enabled = new_state
        state["changes"] += 1
        state["status"] = f"{label} → {'enabled' if new_state else 'disabled'}"
        picker.refresh()

    def _cancel() -> None:
        picker.done(McpPickerResult(cancelled=True, changes=state["changes"]))

    picker.bind_list_nav(
        get_len=lambda: len(rows),
        get_sel=lambda: state["selected"],
        set_sel=lambda i: state.update(selected=i),
        on_enter=_toggle,
        on_cancel=_cancel,
    )
    picker.bind("space", _toggle)
    picker.bind("q", _cancel)
    return picker


async def pick_mcp(
    servers: list[McpServerView],
    *,
    on_toggle: ToggleFn,
    theme: "Theme | None" = None,
) -> McpPickerResult:
    """Run the picker as its own full-screen app (classic front end)."""
    if not servers:
        return McpPickerResult(cancelled=True)
    return await build_mcp_picker(servers, on_toggle=on_toggle, theme=theme).run()


__all__ = [
    "McpToolView",
    "McpServerView",
    "McpPickerResult",
    "build_mcp_picker",
    "pick_mcp",
]
