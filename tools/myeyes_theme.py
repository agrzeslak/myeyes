"""Load and resolve the myeyes theme definition.

This module is the single place that understands `theme.toml`. Every
implementation generator (nvim, alacritty, Windows Terminal, ...) and the
preview script go through `load_theme()`, so they all agree on what each role
resolves to and how OKLCH is turned into sRGB hex.

Standard library only, so any script can import it via `uv run` with no
dependency setup.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_THEME_PATH = Path(__file__).resolve().parent.parent / "theme.toml"


# ==============================================================================
# OKLCH -> sRGB conversion
# ==============================================================================

# The palette is authored in OKLCH because its lightness axis is perceptually
# uniform: two accents with the same L look equally bright, which is what lets
# us give every syntax colour the same legibility against the background.
# Consumers (terminals, editors) want sRGB hex, so we convert here, using the
# reference matrices from Björn Ottosson's OKLab definition.


@dataclass(frozen=True)
class Oklch:
    l: float  # 0..1
    c: float  # 0..~0.4
    h: float  # degrees

    def __str__(self) -> str:
        return f"oklch({self.l * 100:.1f}% {self.c:.3f} {self.h:.1f})"


def _oklch_to_linear_srgb(color: Oklch) -> tuple[float, float, float]:
    a = color.c * math.cos(math.radians(color.h))
    b = color.c * math.sin(math.radians(color.h))

    l_ = color.l + 0.3963377774 * a + 0.2158037573 * b
    m_ = color.l - 0.1055613458 * a - 0.0638541728 * b
    s_ = color.l - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_**3, m_**3, s_**3

    return (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _linear_to_srgb_channel(x: float) -> float:
    if x <= 0.0031308:
        return 12.92 * x
    return 1.055 * x ** (1 / 2.4) - 0.055


def _srgb_to_linear_channel(x: float) -> float:
    if x <= 0.04045:
        return x / 12.92
    return ((x + 0.055) / 1.055) ** 2.4


def _in_gamut(rgb: tuple[float, float, float], eps: float = 1e-5) -> bool:
    return all(-eps <= ch <= 1 + eps for ch in rgb)


def oklch_to_srgb(color: Oklch) -> tuple[tuple[int, int, int], bool]:
    """Convert to 8-bit sRGB, returning `(rgb, was_clipped)`.

    Saturated OKLCH colours can fall outside sRGB. Rather than clamping each
    channel (which shifts hue and lightness noticeably), we binary-search the
    largest chroma that fits while keeping L and h fixed. Lightness is the
    property we care most about for legibility, so it is the one we preserve.
    """
    linear = _oklch_to_linear_srgb(color)
    clipped = not _in_gamut(linear)
    if clipped:
        lo, hi = 0.0, color.c
        for _ in range(30):
            mid = (lo + hi) / 2
            if _in_gamut(_oklch_to_linear_srgb(Oklch(color.l, mid, color.h))):
                lo = mid
            else:
                hi = mid
        linear = _oklch_to_linear_srgb(Oklch(color.l, lo, color.h))

    rgb = tuple(
        round(min(1.0, max(0.0, _linear_to_srgb_channel(ch))) * 255) for ch in linear
    )
    return rgb, clipped  # type: ignore[return-value]


def srgb_hex_to_oklch(hex_str: str) -> Oklch:
    """Inverse conversion. Used for deriving OKLCH seeds from existing themes."""
    r, g, b = (int(hex_str.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4))
    r, g, b = (_srgb_to_linear_channel(ch) for ch in (r, g, b))

    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))

    lab_l = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    lab_a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    lab_b = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_

    c = math.hypot(lab_a, lab_b)
    h = math.degrees(math.atan2(lab_b, lab_a)) % 360
    return Oklch(lab_l, c, h)


# ==============================================================================
# Contrast
# ==============================================================================

# WCAG 2 contrast ratio. It is known to be imperfect (APCA models perception
# better, especially for light text on dark backgrounds), but for a light theme
# with dark text it tracks well and the thresholds (4.5 body, 3.0 large/UI) are
# widely understood.
# TODO: add APCA Lc alongside WCAG if WCAG ratios stop matching what looks right.


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_srgb_to_linear_channel(ch / 255) for ch in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ==============================================================================
# Theme loading
# ==============================================================================

# `theme.toml` has two layers:
#
#   [palette]      named colours, written as `oklch(L% C H)` or `#rrggbb`
#   [<role group>] semantic roles (`ui`, `syntax`, `ansi`, ...), whose values
#                  are palette names, not colours
#
# Keeping roles as references means tweaking one palette entry propagates to
# every role and every generated implementation that uses it.

_OKLCH_RE = re.compile(
    r"oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+)\s*\)", re.IGNORECASE
)
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


@dataclass(frozen=True)
class Color:
    name: str
    oklch: Oklch
    rgb: tuple[int, int, int]
    clipped: bool

    @property
    def hex(self) -> str:
        return "#{:02x}{:02x}{:02x}".format(*self.rgb)


def parse_color(name: str, value: str) -> Color:
    if m := _OKLCH_RE.fullmatch(value.strip()):
        l = float(m.group(1)) / (100 if m.group(2) else 1)
        oklch = Oklch(l, float(m.group(3)), float(m.group(4)))
        rgb, clipped = oklch_to_srgb(oklch)
        return Color(name, oklch, rgb, clipped)
    if _HEX_RE.fullmatch(value.strip()):
        oklch = srgb_hex_to_oklch(value)
        rgb, _ = oklch_to_srgb(oklch)
        return Color(name, oklch, rgb, False)
    raise ValueError(f"palette.{name}: cannot parse colour {value!r}")


@dataclass(frozen=True)
class Theme:
    meta: dict[str, str]
    palette: dict[str, Color]
    # roles["syntax"]["keyword"] -> Color, with insertion order preserved from
    # the TOML so generators and previews list roles in the author's order.
    roles: dict[str, dict[str, Color]]

    def role(self, dotted: str) -> Color:
        """Look up `"group.name"`, e.g. `theme.role("syntax.keyword")`."""
        group, _, name = dotted.partition(".")
        return self.roles[group][name]


def load_theme(path: Path = DEFAULT_THEME_PATH) -> Theme:
    raw = tomllib.loads(path.read_text())

    meta = raw.pop("meta", {})
    palette = {
        name: parse_color(name, value) for name, value in raw.pop("palette").items()
    }

    # Every remaining top-level table is a role group. Unknown references are
    # an error rather than a silent fallback, so typos surface immediately.
    roles: dict[str, dict[str, Color]] = {}
    for group, entries in raw.items():
        roles[group] = {}
        for role, ref in entries.items():
            if ref not in palette:
                raise KeyError(f"{group}.{role} references unknown palette entry {ref!r}")
            roles[group][role] = palette[ref]

    return Theme(meta, palette, roles)
