"""Built-in color palettes and color helpers for the CLI themes.

A :class:`Palette` is the dozen semantic colors a well-known editor theme
defines (background, surfaces, text levels, accents, state colors);
:func:`palette_theme` expands it into every token of a
:class:`~phoson_cli.theme.Theme`, so a palette is data, not forty
hand-tuned strings. The same helpers derive the accent family when the user
recolors a theme (``/theme accent``).

The palettes are ports of the public color specifications of each project
(Catppuccin, Dracula, Nord, Gruvbox, Tokyo Night, Solarized, Rosé Pine,
One Dark); only their colors are used.
"""

from typing import TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from .theme import Theme


# ── Color helpers ─────────────────────────────────────────────────────────────


def to_hex(color: str) -> str:
    """Any Rich color (``#abc``, ``#aabbcc``, ``magenta``, ``plum3``…) as ``#rrggbb``.

    Raises:
        ValueError: when Rich cannot parse ``color``.
    """
    from rich.color import Color, ColorParseError

    raw = color.strip()
    if raw.startswith("#") and len(raw) == 4:
        raw = "#" + "".join(ch * 2 for ch in raw[1:])
    try:
        parsed = Color.parse(raw)
    except ColorParseError as exc:
        raise ValueError(f"not a color: {color!r}") from exc
    if parsed.is_default:
        raise ValueError(f"not a concrete color: {color!r}")
    return parsed.get_truecolor().hex


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def blend(color: str, other: str, amount: float) -> str:
    """``amount`` of ``color`` over ``other`` (both ``#rrggbb``), as hex."""
    a, b = _rgb(color), _rgb(other)
    mixed = (round(x * amount + y * (1 - amount)) for x, y in zip(a, b, strict=True))
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


def is_light(hex_color: str) -> bool:
    """Whether ``hex_color`` reads as a light background (relative luminance)."""
    r, g, b = (channel / 255 for channel in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5


def _hex_bg(style: str) -> str | None:
    """The ``#rrggbb`` of an ``on #rrggbb`` style or a bare hex, else None."""
    value = style.strip()
    if value.startswith("on "):
        value = value[3:].strip()
    if value.startswith("#") and len(value) == 7:
        return value
    return None


# ── Palettes ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Palette:
    """The semantic colors of one editor theme (all ``#rrggbb``).

    ``bg`` is the editor background; ``surface`` a raised panel; ``overlay``
    the selection/highlight; ``text`` > ``subtext`` > ``muted`` the text
    levels; ``accent``/``accent2`` the brand colors; ``code_theme`` a
    Pygments style for fenced code.
    """

    name: str
    description: str
    bg: str
    surface: str
    overlay: str
    text: str
    subtext: str
    muted: str
    accent: str
    accent2: str
    green: str
    red: str
    yellow: str
    code_theme: str


def palette_theme(palette: Palette) -> "Theme":
    """Expand a :class:`Palette` into a complete :class:`Theme`."""
    from .theme import Theme

    p = palette
    return Theme(
        name=p.name,
        text=p.text,
        muted=p.subtext,
        muted_deep=p.muted,
        accent=p.accent,
        accent_soft=p.accent2,
        art=f"{p.accent} bold",
        ok=p.green,
        err=p.red,
        warn=p.yellow,
        reasoning=p.muted,
        diff_add_bg=f"on {blend(p.green, p.bg, 0.18)}",
        diff_del_bg=f"on {blend(p.red, p.bg, 0.18)}",
        panel_bg=f"on {p.surface}",
        badge_user=f"on {blend(p.accent, p.bg, 0.2)}",
        badge_assistant=f"bold {p.text}",
        badge_history=f"bold {p.text}",
        code_theme=p.code_theme,
        pt_muted=p.subtext,
        pt_muted_deep=p.muted,
        pt_accent=p.accent,
        pt_ok=p.green,
        pt_err=p.red,
        prompt_input=p.subtext,
        prompt_bracket=p.muted,
        prompt_model=p.text,
        prompt_node=p.muted,
        prompt_tokens=p.subtext,
        prompt_arrow=p.accent,
        completion_bg=p.surface,
        completion_fg=p.subtext,
        completion_current_bg=blend(p.accent, p.bg, 0.35),
        completion_current_fg=p.text,
        completion_meta_bg=p.bg,
        completion_meta_fg=p.muted,
        scrollbar_bg=p.bg,
        scrollbar_button=p.overlay,
    )


PALETTES: tuple[Palette, ...] = (
    Palette(
        name="catppuccin-mocha",
        description="Catppuccin Mocha — soft pastels on dark",
        bg="#1e1e2e",
        surface="#313244",
        overlay="#45475a",
        text="#cdd6f4",
        subtext="#a6adc8",
        muted="#6c7086",
        accent="#cba6f7",
        accent2="#f5c2e7",
        green="#a6e3a1",
        red="#f38ba8",
        yellow="#f9e2af",
        code_theme="monokai",
    ),
    Palette(
        name="catppuccin-latte",
        description="Catppuccin Latte — soft pastels on light",
        bg="#eff1f5",
        surface="#e6e9ef",
        overlay="#bcc0cc",
        text="#4c4f69",
        subtext="#6c6f85",
        muted="#9ca0b0",
        accent="#8839ef",
        accent2="#ea76cb",
        green="#40a02b",
        red="#d20f39",
        yellow="#df8e1d",
        code_theme="friendly",
    ),
    Palette(
        name="dracula",
        description="Dracula — purple and pink on dark",
        bg="#282a36",
        surface="#343746",
        overlay="#44475a",
        text="#f8f8f2",
        subtext="#c6c8d1",
        muted="#6272a4",
        accent="#bd93f9",
        accent2="#ff79c6",
        green="#50fa7b",
        red="#ff5555",
        yellow="#f1fa8c",
        code_theme="dracula",
    ),
    Palette(
        name="nord",
        description="Nord — arctic blues on dark",
        bg="#2e3440",
        surface="#3b4252",
        overlay="#4c566a",
        text="#eceff4",
        subtext="#d8dee9",
        muted="#7b88a1",
        accent="#88c0d0",
        accent2="#81a1c1",
        green="#a3be8c",
        red="#bf616a",
        yellow="#ebcb8b",
        code_theme="nord",
    ),
    Palette(
        name="gruvbox-dark",
        description="Gruvbox dark — warm retro on dark",
        bg="#282828",
        surface="#3c3836",
        overlay="#504945",
        text="#ebdbb2",
        subtext="#d5c4a1",
        muted="#928374",
        accent="#fe8019",
        accent2="#d3869b",
        green="#b8bb26",
        red="#fb4934",
        yellow="#fabd2f",
        code_theme="gruvbox-dark",
    ),
    Palette(
        name="gruvbox-light",
        description="Gruvbox light — warm retro on light",
        bg="#fbf1c7",
        surface="#ebdbb2",
        overlay="#d5c4a1",
        text="#3c3836",
        subtext="#504945",
        muted="#928374",
        accent="#af3a03",
        accent2="#8f3f71",
        green="#79740e",
        red="#9d0006",
        yellow="#b57614",
        code_theme="gruvbox-light",
    ),
    Palette(
        name="tokyo-night",
        description="Tokyo Night — neon blues on night",
        bg="#1a1b26",
        surface="#24283b",
        overlay="#414868",
        text="#c0caf5",
        subtext="#a9b1d6",
        muted="#565f89",
        accent="#7aa2f7",
        accent2="#bb9af7",
        green="#9ece6a",
        red="#f7768e",
        yellow="#e0af68",
        code_theme="one-dark",
    ),
    Palette(
        name="solarized-dark",
        description="Solarized dark — low-contrast classic",
        bg="#002b36",
        surface="#073642",
        overlay="#2a4f58",
        text="#93a1a1",
        subtext="#839496",
        muted="#586e75",
        accent="#268bd2",
        accent2="#6c71c4",
        green="#859900",
        red="#dc322f",
        yellow="#b58900",
        code_theme="solarized-dark",
    ),
    Palette(
        name="solarized-light",
        description="Solarized light — low-contrast classic",
        bg="#fdf6e3",
        surface="#eee8d5",
        overlay="#d9d2bd",
        text="#586e75",
        subtext="#657b83",
        muted="#93a1a1",
        accent="#268bd2",
        accent2="#6c71c4",
        green="#859900",
        red="#dc322f",
        yellow="#b58900",
        code_theme="solarized-light",
    ),
    Palette(
        name="rose-pine",
        description="Rosé Pine — muted rose and iris",
        bg="#191724",
        surface="#1f1d2e",
        overlay="#26233a",
        text="#e0def4",
        subtext="#908caa",
        muted="#6e6a86",
        accent="#c4a7e7",
        accent2="#ebbcba",
        green="#9ccfd8",
        red="#eb6f92",
        yellow="#f6c177",
        code_theme="monokai",
    ),
    Palette(
        name="one-dark",
        description="One Dark — Atom's balanced dark",
        bg="#282c34",
        surface="#2c313a",
        overlay="#3e4451",
        text="#abb2bf",
        subtext="#9da5b4",
        muted="#5c6370",
        accent="#61afef",
        accent2="#c678dd",
        green="#98c379",
        red="#e06c75",
        yellow="#e5c07b",
        code_theme="one-dark",
    ),
)


def palette_themes() -> dict[str, "Theme"]:
    """Every built-in palette as a theme, in display order."""
    return {p.name: palette_theme(p) for p in PALETTES}


def palette_descriptions() -> dict[str, str]:
    return {p.name: p.description for p in PALETTES}


# ── Accent family ─────────────────────────────────────────────────────────────

#: Tokens recolored together when only ``accent`` is overridden (unless the
#: user overrides them explicitly too).
ACCENT_FAMILY: tuple[str, ...] = (
    "accent_soft",
    "art",
    "pt_accent",
    "prompt_arrow",
    "completion_current_bg",
    "completion_current_fg",
    "badge_user",
)


def accent_family(theme: "Theme", accent: str) -> dict[str, str]:
    """The tokens to change on ``theme`` so its accent becomes ``accent``.

    Derived from the theme it lands on: a softer variant mixed toward the
    theme's background, a selection background tinted with the accent, and
    the user-turn badge tinted the same way. Tiers that inherit the
    terminal's colors (no hex backgrounds) only take the accent itself.

    Raises:
        ValueError: when ``accent`` is not a color.
    """
    color = to_hex(accent)
    tokens = {
        "accent": color,
        "art": f"{color} bold",
        "pt_accent": color,
        "prompt_arrow": color,
    }
    bg = _hex_bg(theme.completion_meta_bg) or _hex_bg(theme.scrollbar_bg)
    if bg is None:
        # Terminal-native tiers (system/ansi): no palette background to mix
        # with. The selected completion is drawn in the accent color there.
        tokens["accent_soft"] = color
        if theme.completion_current_bg in ("", "default"):
            tokens["completion_current_fg"] = color
        return tokens
    toward = "#000000" if is_light(bg) else "#ffffff"
    tokens["accent_soft"] = blend(color, toward, 0.7)
    tokens["completion_current_bg"] = blend(color, bg, 0.35)
    if _hex_bg(theme.badge_user) is not None:
        tokens["badge_user"] = f"on {blend(color, bg, 0.2)}"
    return tokens


__all__ = [
    "ACCENT_FAMILY",
    "PALETTES",
    "Palette",
    "accent_family",
    "blend",
    "is_light",
    "palette_descriptions",
    "palette_theme",
    "palette_themes",
    "to_hex",
]
