"""Key bindings for the full-screen app.

Kept separate from ``app.py`` so bindings can be exercised directly in
tests (call the bound handler function) without a running
``Application`` — same pattern used by ``phoson_cli/pickers/_base.py``.

**Customizable bindings (IMPROVEMENTS.md E6).** The built-in key map is
:data:`DEFAULT_KEY_BINDINGS` — the single source of truth for both the
default bindings and the ``/keys`` listing. Users can remap any action
from the ``[keys]`` section of ``~/.phoson/config.toml``::

    [keys]
    toggle_reasoning = "c-x"      # single sequence
    line_up = ["s-up", "c-up"]    # list = precedence order (unbound: "")

Remaps are loaded by :func:`phoson_cli.config.load_key_bindings` (which
also validates them — an unknown action or an unparseable sequence is a
hard error at startup, never a silent fallback to the defaults) and
passed into :func:`build_key_bindings` as ``overrides``.
"""

from typing import TYPE_CHECKING

from prompt_toolkit.key_binding import KeyBindings

if TYPE_CHECKING:
    from .app import PhosonApp
    from ..config import PhosonConfig


#: The built-in key map (IMPROVEMENTS.md E6). Action → list of
#: prompt_toolkit key sequences in precedence order (the first one
#: registered wins when several sequences share an action — e.g.
#: ``Ctrl+Up`` and ``Shift+Up`` both scroll the chat up).
#:
#: Every action that exists here is remappable from ``[keys]``; the
#: canonical list of names is
#: :data:`phoson_cli.config.KNOWN_KEY_ACTIONS` (kept in sync with this
#: mapping — the test suite cross-checks the two).
DEFAULT_KEY_BINDINGS: dict[str, list[str]] = {
    "submit": ["enter"],
    "newline": ["c-j"],
    "page_up": ["pageup"],
    "page_down": ["pagedown"],
    "line_up": ["s-up", "c-up"],
    "line_down": ["s-down", "c-down"],
    "scroll_home": ["home"],
    "scroll_end": ["end"],
    "clear": ["c-l"],
    "toggle_reasoning": ["c-t"],
    # Ctrl+E cycles the reasoning effort off → low → medium → high →
    # xhigh → max (mnemonic: E = effort). Ctrl+T stays the show/hide
    # toggle for the reasoning block — the two are different axes.
    "cycle_reasoning_effort": ["c-e"],
    "ctrl_d": ["c-d"],
    "paste_image": ["c-v"],
    "escape": ["escape"],
    "undo_jump": ["c-z"],
    # Shift+Tab cycles the visible permission mode (T-6): ask → auto.
    # prompt_toolkit parses "s-tab" to Keys.BackTab on every terminal.
    "toggle_permission_mode": ["s-tab"],
    # Ctrl+P opens the command palette (T-12): a single fuzzy picker over
    # every slash command. Ctrl+P is free of the classic in-chat bindings
    # (Ctrl+L clear, Ctrl+T reasoning, Ctrl+Z undo, …).
    "command_palette": ["c-p"],
    # Ctrl+Q and Ctrl+C share the exit action — both sequences keep their
    # classic roles (Ctrl+C also keeps its SIGINT handling elsewhere).
    "exit": ["c-q", "c-c"],
}

#: Action → no-arg method on ``PhosonApp`` that performs it. The single
#: place where an action is wired to its handler; ``build_key_bindings``
#: is pure table-driven from :data:`DEFAULT_KEY_BINDINGS` + this map.
_ACTION_HANDLERS: dict[str, str] = {
    "submit": "submit",
    "newline": "insert_newline",
    "page_up": "scroll_page_up",
    "page_down": "scroll_page_down",
    "line_up": "scroll_line_up",
    "line_down": "scroll_line_down",
    "scroll_home": "scroll_home",
    "scroll_end": "scroll_end",
    "clear": "clear",
    "toggle_reasoning": "toggle_reasoning",
    "cycle_reasoning_effort": "cycle_reasoning_effort",
    "ctrl_d": "handle_ctrl_d",
    "paste_image": "paste_image",
    "escape": "handle_escape",
    "undo_jump": "undo_jump",
    "toggle_permission_mode": "cycle_permission_mode",
    "command_palette": "open_command_palette",
    "exit": "request_exit",
}


def resolve_key_bindings(
    defaults: dict[str, list[str]] | None = None,
    overrides: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Merge user overrides onto the built-in key map.

    ``overrides`` maps action → replacement sequence list (the validated
    shape produced by ``phoson_cli.config.load_key_bindings``). An action
    absent from ``overrides`` keeps its default sequences; an action
    present with an empty list is *unbound* (the handler is dropped).
    Unknown actions are filtered out silently — validation happens in
    ``load_key_bindings`` before this is reached.

    Raises:
        ValueError: When two actions end up bound to the same sequence —
            a remap that collides with another action's default (e.g.
            ``newline = "enter"``) is ambiguous, so it is rejected rather
            than silently stealing one action's keys.
    """
    base = DEFAULT_KEY_BINDINGS if defaults is None else defaults
    resolved: dict[str, list[str]] = {action: list(seq) for action, seq in base.items()}
    if overrides:
        for action, sequences in overrides.items():
            if action in resolved:
                resolved[action] = list(sequences)

    owner: dict[str, str] = {}
    for action, sequences in resolved.items():
        for sequence in sequences:
            if sequence in owner and owner[sequence] != action:
                raise ValueError(
                    f"Key sequence {sequence!r} is bound to two actions:"
                    f" {owner[sequence]!r} and {action!r}."
                )
            owner[sequence] = action
    return resolved


def build_key_bindings(
    app: "PhosonApp",
    *,
    overrides: dict[str, list[str]] | None = None,
) -> KeyBindings:
    """Build the global key bindings for ``app``.

    ``overrides`` (IMPROVEMENTS.md E6) remaps individual actions; when
    absent the built-in :data:`DEFAULT_KEY_BINDINGS` are used unchanged.
    """
    kb = KeyBindings()
    try:
        bindings = resolve_key_bindings(overrides=overrides)
    except ValueError as exc:
        from ..config import PhosonKeyBindingsError

        raise PhosonKeyBindingsError(
            f"Conflicting key bindings in [keys]: {exc}"
        ) from exc

    for action, sequences in bindings.items():
        if not sequences:
            continue  # unbound action — the handler is dropped
        method_name = _ACTION_HANDLERS[action]
        method = getattr(app, method_name)

        def _handler(event: object, _m=method) -> None:  # noqa: ARG001
            _m()

        for sequence in sequences:
            if action == "escape":
                # escape must stay eager (see app.py) so a double-tap
                # during a run is never swallowed as a prefix of a
                # longer sequence, and so the single-Esc run cancel
                # (#68) always fires immediately.
                kb.add(*sequence.split(), eager=True)(_handler)  # type: ignore[call-overload]
            else:
                kb.add(*sequence.split())(_handler)  # type: ignore[call-overload]
    return kb


def key_bindings_for_config(
    config: "PhosonConfig | None",
) -> dict[str, list[str]] | None:
    """The validated remap table from ``config``, or ``None``.

    Accepts a plain object (or ``None``) so call sites that don't want a
    hard dependency on the ``PhosonConfig`` type (tests, the classic
    front end) can pass ``None`` or a duck-typed config.
    """
    overrides = getattr(config, "key_bindings", None)
    return overrides if overrides else None


def listing_for_config(
    config: "PhosonConfig | None",
) -> list[tuple[str, str]]:
    """``(action, display-key)`` pairs for ``/keys`` and help text.

    Sequences are shown in the order they were resolved (defaults or
    overrides); an unbound action renders as ``"(off)"``. Display names
    use the same vocabulary as the TUI footer (``Ctrl+T``, ``Enter``…).
    """
    resolved = resolve_key_bindings(overrides=key_bindings_for_config(config))
    rows: list[tuple[str, str]] = []
    for action in DEFAULT_KEY_BINDINGS:
        sequences = resolved.get(action, [])
        if not sequences:
            rows.append((action, "(off)"))
        else:
            rows.append((action, ", ".join(_display_sequence(s) for s in sequences)))
    return rows


def _display_sequence(sequence: str) -> str:
    """``"c-x"`` → ``Ctrl+X``; ``"c-x c-e"`` → ``Ctrl+X Ctrl+E``."""
    names = {"pageup": "PgUp", "pagedown": "PgDn", "escape": "Esc"}
    parts: list[str] = []
    for key in sequence.split():
        key = key.lower()
        ctrl = key.startswith("c-")
        shift = key.startswith("s-")
        name = key.split("-", 1)[1] if "-" in key else key
        name = names.get(name, name.capitalize())
        parts.append(("Ctrl+" if ctrl else "") + ("Shift+" if shift else "") + name)
    return " ".join(parts)


__all__ = [
    "DEFAULT_KEY_BINDINGS",
    "build_key_bindings",
    "key_bindings_for_config",
    "listing_for_config",
    "resolve_key_bindings",
]
