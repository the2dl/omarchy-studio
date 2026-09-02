"""Resolve the design spec's tokens from the user's live Omarchy theme.

docs/design/spec.md names a fixed dark palette with an amber accent. That palette is
the *shape* of the design, not its colours: Omarchy apps take their look from the
active theme, so every token here is derived from `colors.toml` plus Hyprland's live
`decoration:rounding`.

Three derivations carry the design intent rather than the literal values:

* The spec insists the greys are warm-shifted to sit with the amber. That solves itself
  -- Omarchy's background ramp is already tinted per theme (osaka-jade `#111c18` is
  green, nord `#2e3440` is blue), so pulling the ramp from the theme gets the correct
  tint for free, for every theme, without a hue calculation.

* The spec's radius scale (16 window / 9-11 row / 7 chip) is not arbitrary: it is
  almost exactly `decoration:rounding` and two fractions of it. Deriving the scale from
  the live value means a square-cornered theme squares the whole app.

* On a light theme the chrome inverts but the CANVAS STAYS DARK. You cannot judge a
  recording's exposure against white -- a dark surround is the reference condition, and
  every video tool ends up here. It is the one deliberate deviation from the theme.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

THEME_DIR = Path.home() / ".local/state/omarchy/current/theme"

# Fallbacks matching the spec's own palette, used when no Omarchy theme is present
# (a bare checkout, CI, or a non-Omarchy machine). Nothing should crash for want of a
# theme; it should just look like the mock.
_SPEC_DEFAULTS = {
    "mode": "dark",
    "accent": "#f2a25a",
    "background": "#0d0e10",
    "dark_background": "#0a0b0d",
    "darker_background": "#0a0b0d",
    "lighter_background": "#1d1a17",
    "foreground": "#eceae7",
    "bright_foreground": "#f6f4f2",
    "light_foreground": "#b3aea8",
    "dark_foreground": "#625d58",
    "red": "#e0523f",
    "selection": "#2a2a2a",
    "muted": "#333333",
}

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


# --- colour utilities -------------------------------------------------------


def _parse(hex_str: str) -> tuple[int, int, int]:
    m = _HEX.match(hex_str.strip())
    if not m:
        raise ValueError(f"not a #rrggbb colour: {hex_str!r}")
    v = m.group(1)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def _luminance(hex_str: str) -> float:
    """WCAG relative luminance, used only to choose foreground-on-accent."""

    def chan(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = _parse(hex_str)
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _mix(a: str, b: str, t: float) -> str:
    ra, ga, ba = _parse(a)
    rb, gb, bb = _parse(b)
    return _hex((ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t))


def _scale(hex_str: str, factor: float) -> str:
    r, g, b = _parse(hex_str)
    return _hex((r * factor, g * factor, b * factor))


def _rgba(hex_str: str, alpha: float) -> str:
    """QML accepts #aarrggbb, which is the form the tokens are consumed in."""
    r, g, b = _parse(hex_str)
    return f"#{round(alpha * 255):02x}{r:02x}{g:02x}{b:02x}"


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _ink_on(bg: str, dark: str, light: str) -> str:
    """Pick whichever ink actually reads on `bg`.

    A luminance threshold gets this wrong on saturated mid-tones: amber #e68e0d sits
    at 0.36 luminance yet wants near-black ink (8.2:1 against black, 2.6:1 against
    white), while blue #1e66f5 sits lower at 0.16 and wants white. Comparing the two
    contrast ratios is the rule that holds for both.
    """
    return dark if _contrast(bg, dark) >= _contrast(bg, light) else light


# --- source -----------------------------------------------------------------


def read_colors(theme_dir: Path | None = None) -> dict[str, str]:
    """Parse colors.toml with a regex rather than a TOML library.

    The file is a flat list of `key = "#value"` lines by construction, and this runs on
    every window open -- not worth an import, and it keeps the module dependency-free
    so the QML bridge can call it without a venv.
    """
    d = dict(_SPEC_DEFAULTS)
    path = (theme_dir or THEME_DIR) / "colors.toml"
    if not path.exists():
        return d
    for line in path.read_text().splitlines():
        m = re.match(r'^\s*([a-z_]+)\s*=\s*"([^"]*)"', line)
        if m:
            d[m.group(1)] = m.group(2)
    return d


def read_rounding() -> int:
    """Hyprland's live corner radius. Absent Hyprland, fall back to the spec's 16.

    Read live rather than from the theme's hyprland.lua: the user's own config may
    override the theme, and the app should match what is actually on screen.
    """
    try:
        out = subprocess.run(
            ["hyprctl", "getoption", "decoration:rounding"],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 16
    m = re.search(r"int:\s*(-?\d+)", out)
    return max(0, int(m.group(1))) if m else 16


# --- the resolved token set -------------------------------------------------


@dataclass
class Radii:
    window: int
    panel: int
    row: int
    chip: int

    @classmethod
    def from_rounding(cls, r: int) -> "Radii":
        # At r=14 (a common Omarchy setting) this yields 14/14/9/7, which is within a
        # pixel of the spec's hand-picked 16/15-18/9-11/7. At r=0 everything squares.
        return cls(window=r, panel=r, row=round(r * 0.65), chip=round(r * 0.5))


@dataclass
class Theme:
    mode: str
    tokens: dict[str, str] = field(default_factory=dict)
    radii: Radii = field(default_factory=lambda: Radii(16, 16, 10, 7))
    font: str = "JetBrainsMono Nerd Font"

    @property
    def is_light(self) -> bool:
        return self.mode == "light"

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "font": self.font,
            "radii": {"window": self.radii.window, "panel": self.radii.panel,
                      "row": self.radii.row, "chip": self.radii.chip},
            **self.tokens,
        }


def resolve(theme_dir: Path | None = None, rounding: int | None = None) -> Theme:
    c = read_colors(theme_dir)
    mode = c.get("mode", "dark").strip().lower()
    light = mode == "light"
    accent = c["accent"]

    bg = c["background"]
    fg = c["foreground"]
    bright = c.get("bright_foreground", fg)
    dim = c.get("dark_foreground", _mix(fg, bg, 0.55))
    mid = c.get("light_foreground", _mix(fg, bg, 0.25))

    # On a light theme the ramp runs the other way: "deeper" means darker than the
    # page, not lighter, so the recessed timeline tray reads as recessed either way.
    deep = c.get("darker_background", _scale(bg, 0.7))
    if light:
        deep = _mix(bg, "#000000", 0.06)

    # The canvas is always dark -- see the module docstring. Built from the accent so
    # it still belongs to the theme rather than being a foreign slab of grey.
    canvas_a = _mix("#16171a", accent, 0.06)
    canvas_b = "#101114"

    # accent-on: whatever reads on an accent fill. Both candidates are tinted with the
    # accent rather than pure black/white, which is what makes the spec's #1a1006 sit
    # on amber as though it belongs to it instead of punching a hole in it.
    accent_on = _ink_on(accent, _mix("#000000", accent, 0.10), _mix("#ffffff", accent, 0.08))

    # A theme that sets bright_foreground == foreground (several do) would collapse the
    # top two steps of the text ramp into one. Lift the brightest step so the six-step
    # hierarchy the spec relies on survives.
    if bright.lower() == fg.lower():
        bright = _mix(fg, "#000000" if light else "#ffffff", 0.18)

    tokens = {
        "bg": bg,
        "bgDeep": deep,
        "bgFloat": _rgba(bg, 0.92),
        "panel": c.get("lighter_background", _mix(bg, fg, 0.04)),
        "canvasA": canvas_a,
        "canvasB": canvas_b,
        "accent": accent,
        "accentDim": _scale(accent, 0.72),
        "accentOn": accent_on,
        "accentWash": _rgba(accent, 0.18),
        "accentGlow": _rgba(accent, 0.22),
        # The one red in the product. Kept as the theme's own red even when the accent
        # is also reddish -- the live dot's pulsing halo distinguishes it by motion,
        # which survives a hue collision that a static swatch would not.
        "live": c.get("red", "#e0523f"),
        "text": bright,
        "text2": fg,
        "text3": mid,
        "text4": _mix(mid, bg, 0.35),
        "text5": dim,
        "text6": _mix(dim, bg, 0.25),
        "hairline": _rgba(fg, 0.07),
        "fillSubtle": _rgba(fg, 0.05),
        "fillHover": _rgba(fg, 0.07),
        "track": _rgba(fg, 0.10),
        "selected": c.get("selection", _rgba(fg, 0.12)),
    }

    # Light themes need firmer separators and fills: the same 5-7% white that reads as
    # a hairline on near-black is invisible on near-white, so these lean on the
    # foreground ink instead and at higher alpha.
    if light:
        tokens.update({
            "hairline": _rgba(fg, 0.14),
            "fillSubtle": _rgba(fg, 0.05),
            "fillHover": _rgba(fg, 0.09),
            "track": _rgba(fg, 0.16),
        })

    return Theme(
        mode=mode,
        tokens=tokens,
        radii=Radii.from_rounding(read_rounding() if rounding is None else rounding),
    )


def dump(theme_dir: Path | None = None) -> str:
    """JSON for the QML bridge. Theme.qml reads this rather than parsing TOML itself,
    so there is exactly one implementation of the derivations."""
    return json.dumps(resolve(theme_dir).to_dict(), indent=2)


if __name__ == "__main__":
    print(dump())
