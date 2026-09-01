"""Theme tokens for the Phoson CLI.

The palette is defined once here as named tokens and consumed by every
rendering site (renderer, views, prompt, pickers, installer). Before this
module the same hex colors were hardcoded in ~6 files and only dark
terminals were supported.

Tiers
-----
- ``system`` — the default (T-8): inherits the terminal's own fg/bg.
  No ``on #rrggbb`` backgrounds anywhere; accent is reduced to the
  spinner/focus and the state colors are the terminal's ANSI green/red/
  yellow. The user's terminal (Gruvbox, Catppuccin, ...) does the rest.
- ``dark``  — the historical purple palette on dark RGB.
- ``light`` — for light terminals (Rich never inverts automatically).
- ``ansi``  — 16-color-safe palette for SSH/dumb terminals; no custom
  backgrounds, only named ANSI colors (Rich degrades gracefully anyway,
  but this tier is deliberate and predictable).
- ``no-color`` — plain text; selected automatically when ``NO_COLOR`` is
  set (non-empty) or ``CLICOLOR=0``, per the CLI color conventions.

Custom themes: drop a JSON file into ``~/.phoson/themes/`` — its tokens
merge onto a built-in ``base`` (see :func:`_load_json_themes`). Python
plugin themes (``ThemeExtension``) remain for the odd case.

Selection: ``NO_COLOR``/``CLICOLOR=0`` always win, then the
``PHOSON_THEME`` env var, then ``config.toml [theme]`` (via
``load_theme(config_value=...)``), then ``system``. Invalid names warn
and fall back to ``dark``.
"""

import os
import json
import warnings
from pathlib import Path
from dataclasses import replace, dataclass
from collections.abc import Mapping, Sequence

from phoson_agent import Plugin, ThemeExtension

# ── Token model ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Theme:
    """A complete color vocabulary for one terminal appearance.

    Every style is a Rich style string; ``panel_bg`` is either ``""`` or
    ``"on #rrggbb"`` so it can be appended to any style.
    """

    name: str

    # core text
    text: str
    muted: str
    muted_deep: str

    # brand
    accent: str
    accent_soft: str
    art: str

    # state
    ok: str
    err: str
    warn: str
    reasoning: str

    # tool cards (T-7): subtle backgrounds for diff +/- lines. ``""``
    # (ansi/system/no-color tiers) means "prefix color only".
    diff_add_bg: str
    diff_del_bg: str

    # containers
    panel_bg: str
    badge_user: str
    badge_assistant: str
    badge_history: str

    # markdown
    code_theme: str

    # prompt_toolkit-safe colors (prompt_toolkit does NOT understand Rich
    # names like grey50/medium_spring_green — only hex, default, or the 16
    # ANSI names without underscores)
    pt_muted: str
    pt_muted_deep: str
    pt_accent: str
    pt_ok: str
    pt_err: str

    # prompt_toolkit fragments (see :func:`build_prompt_style`)
    prompt_input: str
    prompt_bracket: str
    prompt_model: str
    prompt_node: str
    prompt_tokens: str
    prompt_arrow: str
    completion_bg: str
    completion_fg: str
    completion_current_bg: str
    completion_current_fg: str
    completion_meta_bg: str
    completion_meta_fg: str
    scrollbar_bg: str
    scrollbar_button: str

    @property
    def is_dark(self) -> bool:
        return self.name in {"dark", "ansi"}


#: Where user-dropped JSON themes live (T-8): ``~/.phoson/themes/*.json``.
JSON_THEMES_DIR = Path("~/.phoson/themes").expanduser()

#: Tier that inherits the terminal's own colors (T-8). Rich text tokens
#: are ``""`` (no style → the terminal's foreground); state colors are
#: the terminal's own ANSI green/red/yellow; nothing carries an
#: ``on #rrggbb`` background. ``pt_*`` tokens use prompt_toolkit's
#: ``default`` color where a background is structurally required
#: (completion menu, scrollbar) so the terminal's own palette shows.
SYSTEM = Theme(
    name="system",
    text="",
    muted="",
    muted_deep="",
    accent="",
    accent_soft="",
    art="",
    ok="green",
    err="red",
    warn="yellow",
    # "dim" is a Rich style modifier, not a color: the terminal's fg at
    # reduced intensity — the right treatment for thinking text on any
    # palette.
    reasoning="dim",
    diff_add_bg="",
    diff_del_bg="",
    panel_bg="",
    badge_user="bold",
    badge_assistant="bold",
    badge_history="bold",
    code_theme="ansi_dark",
    pt_muted="",
    pt_muted_deep="",
    # Accent reduced to spinner + focus (T-8): cyan is the one named
    # color that stays legible on both light and dark terminals.
    pt_accent="cyan",
    pt_ok="green",
    pt_err="red",
    prompt_input="",
    prompt_bracket="",
    prompt_model="",
    prompt_node="",
    prompt_tokens="",
    prompt_arrow="cyan",
    completion_bg="default",
    completion_fg="",
    completion_current_bg="default",
    completion_current_fg="cyan",
    completion_meta_bg="default",
    completion_meta_fg="",
    scrollbar_bg="default",
    scrollbar_button="default",
)


DARK = Theme(
    name="dark",
    text="white",
    muted="grey50",
    muted_deep="grey35",
    accent="medium_purple1",
    accent_soft="plum3",
    art="medium_purple1 bold",
    ok="medium_spring_green",
    err="indian_red1",
    warn="gold3",
    reasoning="grey42",
    diff_add_bg="on #0f2417",
    diff_del_bg="on #2a1216",
    panel_bg="on #120d1d",
    badge_user="bold white on #23192f",
    badge_assistant="bold white on #3a255e",
    badge_history="bold white on #2e2047",
    code_theme="monokai",
    pt_muted="#9a8faa",
    pt_muted_deep="#6b5b8a",
    pt_accent="#b57bee",
    pt_ok="#00ff9c",
    pt_err="#ff9aa2",
    prompt_input="#9a8faa",
    prompt_bracket="#5a4e6e",
    prompt_model="#e0d0ff",
    prompt_node="#6b5b8a",
    prompt_tokens="#8a7a9a",
    prompt_arrow="#b57bee",
    completion_bg="#1e1530",
    completion_fg="#9a8faa",
    completion_current_bg="#3d2b6e",
    completion_current_fg="#e0d0ff",
    completion_meta_bg="#150f24",
    completion_meta_fg="#6b5b8a",
    scrollbar_bg="#150f24",
    scrollbar_button="#5a4e6e",
)

LIGHT = Theme(
    name="light",
    text="#1a1425",
    muted="#5c5470",
    muted_deep="#8a8299",
    accent="#6f2dbd",
    accent_soft="#9a6fb8",
    art="#6f2dbd bold",
    ok="#1a7f37",
    err="#cf222e",
    warn="#9a6700",
    reasoning="#7a7288",
    diff_add_bg="on #e0f2e4",
    diff_del_bg="on #f8d7da",
    panel_bg="on #f2eef8",
    badge_user="bold #1a1425 on #ddd0f0",
    badge_assistant="bold #1a1425 on #cbb5ec",
    badge_history="bold #1a1425 on #d5c8ea",
    code_theme="friendly",
    pt_muted="#5c5470",
    pt_muted_deep="#8a8299",
    pt_accent="#6f2dbd",
    pt_ok="#1a7f37",
    pt_err="#cf222e",
    prompt_input="#5c5470",
    prompt_bracket="#9a8fb5",
    prompt_model="#3d1a78",
    prompt_node="#7a6b9a",
    prompt_tokens="#8a7a9a",
    prompt_arrow="#6f2dbd",
    completion_bg="#efe9f7",
    completion_fg="#40385a",
    completion_current_bg="#cbb5ec",
    completion_current_fg="#2a1250",
    completion_meta_bg="#f5f1fa",
    completion_meta_fg="#7a6b9a",
    scrollbar_bg="#f5f1fa",
    scrollbar_button="#9a8fb5",
)

ANSI = Theme(
    name="ansi",
    text="white",
    muted="bright_black",
    muted_deep="bright_black",
    accent="bright_magenta",
    accent_soft="magenta",
    art="bright_magenta bold",
    ok="bright_green",
    err="bright_red",
    warn="bright_yellow",
    reasoning="bright_black",
    # 16-color terminals: a full-line background reads as a stripe; the
    # +/- prefix color carries the meaning instead.
    diff_add_bg="",
    diff_del_bg="",
    panel_bg="",
    badge_user="bold",
    badge_assistant="bold",
    badge_history="bold",
    code_theme="monokai",
    # prompt_toolkit only accepts the 8 base ANSI colors (no bright*),
    # so the ANSI tier uses that reduced palette for prompt/picker styles.
    pt_muted="white",
    pt_muted_deep="white",
    pt_accent="magenta",
    pt_ok="green",
    pt_err="red",
    prompt_input="white",
    prompt_bracket="cyan",
    prompt_model="yellow",
    prompt_node="cyan",
    prompt_tokens="white",
    prompt_arrow="magenta",
    completion_bg="blue",
    completion_fg="white",
    completion_current_bg="magenta",
    completion_current_fg="white",
    completion_meta_bg="black",
    completion_meta_fg="white",
    scrollbar_bg="black",
    scrollbar_button="cyan",
)

NO_COLOR = Theme(
    name="no-color",
    text="",
    muted="",
    muted_deep="",
    accent="",
    accent_soft="",
    art="",
    ok="",
    err="",
    warn="",
    reasoning="",
    diff_add_bg="",
    diff_del_bg="",
    panel_bg="",
    badge_user="bold",
    badge_assistant="bold",
    badge_history="bold",
    code_theme="none",
    pt_muted="",
    pt_muted_deep="",
    pt_accent="",
    pt_ok="",
    pt_err="",
    prompt_input="",
    prompt_bracket="",
    prompt_model="",
    prompt_node="",
    prompt_tokens="",
    prompt_arrow="",
    completion_bg="",
    completion_fg="",
    completion_current_bg="",
    completion_current_fg="",
    completion_meta_bg="",
    completion_meta_fg="",
    scrollbar_bg="",
    scrollbar_button="",
)

_BY_NAME = {
    "system": SYSTEM,
    "dark": DARK,
    "light": LIGHT,
    "ansi": ANSI,
    "no-color": NO_COLOR,
}

VALID_NAMES = tuple(sorted(_BY_NAME))


def _load_json_theme_file(path: Path) -> Theme | None:
    """Load one drop-in JSON theme (T-8), or None when unusable.

    Shape — the ``Theme`` tokens, merged onto a built-in ``base``::

        {"name": "nord", "base": "dark", "accent": "#88c0d0", ...}

    ``base`` defaults to ``"dark"``. Unknown tokens or a bad base make
    the file silently skip (a broken user file must never break
    startup); its name is still reported in ``/theme list``.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or path.stem).strip().lower()
    if not name:
        return None
    base_name = str(raw.get("base") or "dark").strip().lower()
    base = _BY_NAME.get(base_name)
    if base is None:
        return None
    known_tokens = set(Theme.__dataclass_fields__) - {"name", "base"}
    tokens: dict[str, str] = {}
    for key, value in raw.items():
        if key in {"name", "base"}:
            continue
        if key in known_tokens and isinstance(value, str):
            tokens[key] = value
    return replace(base, name=name, **tokens)


def load_json_themes() -> dict[str, Theme]:
    """All valid JSON themes from :data:`JSON_THEMES_DIR` (T-8).

    Sorted by file name; a later file with the same theme name wins
    (drop a corrected ``nord.json`` over a broken one). Never raises.
    """
    themes: dict[str, Theme] = {}
    try:
        entries = sorted(JSON_THEMES_DIR.glob("*.json"))
    except OSError:
        return themes
    for entry in entries:
        theme = _load_json_theme_file(entry)
        if theme is not None:
            themes[theme.name] = theme
    return themes


@dataclass(frozen=True)
class ThemeRegistry:
    """Immutable built-in plus plugin-provided themes for one CLI session."""

    themes: Mapping[str, Theme]
    descriptions: Mapping[str, str]

    def get(self, name: str) -> Theme | None:
        return self.themes.get(str(name).strip().lower())

    def valid_names(self) -> tuple[str, ...]:
        return tuple(self.themes)

    def rows(self) -> tuple[tuple[str, str], ...]:
        return tuple((name, self.descriptions[name]) for name in self.themes)


def default_theme_registry() -> ThemeRegistry:
    """Return the registry with the built-in tiers plus drop-in JSON themes."""
    themes = dict(_BY_NAME)
    themes.update(load_json_themes())  # user files may add (not replace) names
    descriptions = {
        "system": "default, inherits your terminal's colors",
        "dark": "purple on dark",
        "light": "light background",
        "ansi": "16-color SSH-safe",
        "no-color": "plain text",
    }
    for name, theme in load_json_themes().items():
        descriptions.setdefault(name, "JSON theme (~/.phoson/themes/)")
    return ThemeRegistry(themes=themes, descriptions=descriptions)


def build_theme_registry(plugins: Sequence[Plugin]) -> ThemeRegistry:
    """Build a registry from one controller's loaded plugin instances."""
    themes = dict(_BY_NAME)
    themes.update(load_json_themes())  # drop-in JSON themes (T-8)
    descriptions = {
        "system": "default, inherits your terminal's colors",
        "dark": "purple on dark",
        "light": "light background",
        "ansi": "16-color SSH-safe",
        "no-color": "plain text",
    }
    for name in themes:
        if name not in descriptions:
            descriptions[name] = "JSON theme (~/.phoson/themes/)"
    known_tokens = set(Theme.__dataclass_fields__) - {"name"}

    for plugin in plugins:
        try:
            extension = plugin.get_theme_extension()
        except Exception as exc:
            raise ValueError(
                f"Plugin {plugin.name!r} failed while declaring its theme: {exc}"
            ) from exc
        if extension is None:
            continue
        if not isinstance(extension, ThemeExtension):
            raise ValueError(
                f"Plugin {plugin.name!r} returned {type(extension).__name__} "
                "instead of ThemeExtension"
            )
        name = extension.name.strip().lower()
        if not name or name in themes:
            raise ValueError(
                f"Plugin {plugin.name!r} theme {extension.name!r} is empty or conflicts"
            )
        invalid_tokens = set(extension.tokens) - known_tokens
        if invalid_tokens:
            raise ValueError(
                f"Plugin {plugin.name!r} theme {name!r} has unknown tokens: "
                f"{', '.join(sorted(invalid_tokens))}"
            )
        base = themes.get(extension.base)
        if base is None:  # Literal is static-only; keep this defensive at runtime.
            raise ValueError(
                f"Plugin {plugin.name!r} theme {name!r} has invalid base "
                f"{extension.base!r}"
            )
        themes[name] = replace(base, name=name, **dict(extension.tokens))
        descriptions[name] = extension.description.strip() or "plugin theme"

    return ThemeRegistry(themes=themes, descriptions=descriptions)


# ── Resolution ────────────────────────────────────────────────────────────────


def _env_requests_no_color() -> bool:
    """True when ``NO_COLOR`` is set (non-empty) or ``CLICOLOR=0``."""
    if os.environ.get("NO_COLOR", "").strip():
        return True
    return os.environ.get("CLICOLOR", "") == "0"


def load_theme(
    config_value: str | None = None, *, registry: ThemeRegistry | None = None
) -> Theme:
    """Resolve the active theme.

    Priority:

    1. ``NO_COLOR`` / ``CLICOLOR=0`` — always wins (CLI convention;
       scripts and CI get plain output).
    2. ``PHOSON_THEME`` env var.
    3. ``config_value`` (the ``theme`` key from ``config.toml``).
    4. ``system`` (T-8) — the terminal's own colors, so a user's
       Gruvbox/Catppuccin/... palette is never fought over.

    Unknown names warn and fall back to ``dark`` (or the no-color tier
    when the environment demands it).

    Args:
        config_value: Optional ``theme`` value from config.

    Returns:
        The resolved :class:`Theme`.
    """
    if _env_requests_no_color():
        return NO_COLOR

    requested = os.environ.get("PHOSON_THEME", "").strip().lower()
    if not requested and config_value:
        requested = str(config_value).strip().lower()
    if not requested:
        return SYSTEM

    active_registry = registry or default_theme_registry()
    theme = active_registry.get(requested)
    if theme is None:
        warnings.warn(
            f"Unknown theme {requested!r} — valid names: "
            f"{', '.join(active_registry.valid_names())}. "
            "Falling back to 'dark'.",
            stacklevel=2,
        )
        return DARK
    return theme


def get_theme(name: str, *, registry: ThemeRegistry | None = None) -> Theme | None:
    """Look up a tier by name (case-insensitive, no env overrides).

    Unlike :func:`load_theme`, this is a *direct* lookup: the
    NO_COLOR/PHOSON_THEME environment is deliberately ignored, so
    ``/theme ansi`` yields the ANSI tier even when the environment would
    otherwise force plain output. Returns ``None`` for unknown names.
    """
    return (registry or default_theme_registry()).get(name)


def suggest_theme(
    *,
    detected_light: bool | None,
    has_persisted: bool,
    env_requests_no_color: bool | None = None,
) -> str | None:
    """Decide whether to suggest a theme at startup (IMPROVEMENTS.md E4).

    Returns the suggested tier name (``"light"``/``"dark"``) when the CLI
    should ask the user, or ``None`` to start silently.

    Args:
        detected_light: Terminal light/dark detection result; ``None``
            means the terminal could not be classified.
        has_persisted: Whether a theme is already persisted (config.toml
            or the ``PHOSON_THEME`` env var) — nothing to suggest then.
        env_requests_no_color: The NO_COLOR/CLICOLOR condition. When
            omitted it is re-evaluated; when the environment demands
            plain output there is nothing to suggest.
    """
    if env_requests_no_color is None:
        env_requests_no_color = _env_requests_no_color()
    if env_requests_no_color or has_persisted or detected_light is None:
        return None
    return "light" if detected_light else "dark"


# ── prompt_toolkit style ──────────────────────────────────────────────────────


def _no_color_guard(d: dict[str, str], theme: Theme) -> dict[str, str]:
    """Blank every style under the no-color tier.

    prompt_toolkit treats ``""`` as "no style"; building ``f"bg:  bold"``
    from empty color tokens would instead be an unparseable style string.
    """
    if theme.name == NO_COLOR.name:
        return {k: "" for k in d}
    return d


def build_picker_style_dict(theme: Theme) -> dict[str, str]:
    """Build the shared full-screen picker style dict for a theme.

    Args:
        theme: The active theme.

    Returns:
        The style dict consumed by :func:`phoson_cli.pickers._base.picker_style`.
    """
    return _no_color_guard(
        {
            "title": f"bold {theme.pt_accent}",
            "header": theme.pt_muted,
            "row.selected": (
                f"bg:{theme.completion_current_bg} bold {theme.completion_current_fg}"
            ),
            "row": theme.prompt_input,
            "row.active": f"bold {theme.pt_ok}",
            "footer": theme.pt_muted_deep,
            "key-hint": f"bold {theme.pt_accent}",
            "search": f"bold {theme.prompt_model}",
            "search.label": f"{theme.pt_accent} bold",
            "search.hint": theme.prompt_input,
            "empty": theme.pt_err,
        },
        theme,
    )


def build_wizard_prompt_style(theme: Theme) -> dict[str, str]:
    """Build the setup-wizard prompt style dict for a theme.

    Args:
        theme: The active theme.

    Returns:
        The style dict for the wizard's ``PromptSession``.
    """
    return _no_color_guard(
        {
            "": theme.prompt_input,
            "wizard.label": f"{theme.prompt_arrow} bold",
            "wizard.default": theme.prompt_tokens,
            "wizard.input": theme.prompt_model,
        },
        theme,
    )


def build_prompt_style(theme: Theme) -> dict[str, str]:
    """Build the ``prompt_toolkit`` style dict for a theme.

    Args:
        theme: The active theme.

    Returns:
        The style dict to pass to ``PromptSession(style=...)``.
    """
    selected = f"bg:{theme.completion_current_bg} {theme.completion_current_fg}"
    return _no_color_guard(
        {
            "": theme.prompt_input,
            "prompt.prefix": f"{theme.prompt_arrow} bold",
            "prompt.bracket": theme.prompt_bracket,
            "prompt.model": f"{theme.prompt_model} bold",
            "prompt.sep": theme.prompt_bracket,
            "prompt.node": theme.prompt_node,
            "prompt.update": theme.prompt_tokens,
            "prompt.tokens": theme.prompt_tokens,
            "prompt.arrow": f"{theme.prompt_arrow} bold",
            "completion-menu": f"bg:{theme.completion_bg} {theme.completion_fg}",
            "completion-menu.completion": (
                f"bg:{theme.completion_bg} {theme.completion_fg}"
            ),
            "completion-menu.completion.current": f"{selected} bold",
            "completion-menu.meta": (
                f"bg:{theme.completion_meta_bg} {theme.completion_meta_fg}"
            ),
            "completion-menu.meta.current": (
                f"bg:{theme.completion_current_bg} {theme.completion_meta_fg}"
            ),
            "scrollbar.background": f"bg:{theme.scrollbar_bg}",
            "scrollbar.button": f"bg:{theme.scrollbar_button}",
        },
        theme,
    )
