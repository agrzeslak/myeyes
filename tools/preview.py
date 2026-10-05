#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Preview the myeyes theme in a truecolor terminal.

    tools/preview.py                  # everything
    tools/preview.py palette          # raw palette with OKLCH/hex/contrast
    tools/preview.py contrast         # every foreground role on every surface
    tools/preview.py ansi             # the 16 terminal colours
    tools/preview.py code             # annotated Rust sample as an editor view
    tools/preview.py --watch [...]    # re-render whenever theme or sample change
    tools/preview.py code --compare tmp/theme-darker.toml
                                      # space toggles A/B, i toggles comment italics, q quits

Everything is painted onto the theme's own background, so the preview looks
the same regardless of your terminal's current theme.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from myeyes_theme import DEFAULT_THEME_PATH, Color, Theme, contrast_ratio, load_theme

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLE = REPO / "samples" / "sample.rs.txt"

# WCAG thresholds: 4.5 for body text, 3.0 for large text and non-text UI.
BODY_MIN = 4.5
UI_MIN = 3.0


# ==============================================================================
# Terminal painting
# ==============================================================================

# All output is built from `Canvas` lines: fixed-width rows whose unpainted
# space gets the theme background. This avoids the terminal's own background
# bleeding through at line ends, which would make it impossible to judge the
# theme's colours in context.

RESET = "\x1b[0m"


def fg(c: Color) -> str:
    return "\x1b[38;2;{};{};{}m".format(*c.rgb)


def bg(c: Color) -> str:
    return "\x1b[48;2;{};{};{}m".format(*c.rgb)


class Canvas:
    def __init__(self, theme: Theme, width: int):
        self.theme = theme
        self.width = width
        self.out: list[str] = []

    def line(self, spans: list[tuple[str, Color | None, Color | None, str]] = ()):
        """Emit one row. Each span is `(text, fg, bg, extra_sgr)`; `None`
        means the default body fg / theme bg."""
        base_bg = self.theme.role("ui.bg")
        base_fg = self.theme.role("ui.fg")
        parts, used = [], 0
        for text, f, b, extra in spans:
            parts.append(f"{bg(b or base_bg)}{fg(f or base_fg)}{extra}{text}{RESET}")
            used += len(text)
        parts.append(f"{bg(base_bg)}{' ' * max(0, self.width - used)}{RESET}")
        self.out.append("".join(parts))

    def text(self, s: str = "", color: Color | None = None, extra: str = ""):
        self.line([(s, color, None, extra)])

    def heading(self, title: str):
        self.text()
        self.text(f"  {title}", self.theme.role("ui.fg_strong"), "\x1b[1m")
        self.text(f"  {'─' * len(title)}", self.theme.role("ui.border"))

    def render(self) -> str:
        return "\n".join(self.out)


def verdict(ratio: float, minimum: float) -> str:
    return " " if ratio >= minimum else "!"


# ==============================================================================
# Palette section
# ==============================================================================


def section_palette(cv: Canvas):
    theme = cv.theme
    page = theme.role("ui.bg")
    cv.heading("Palette")
    cv.text("  name              swatch   oklch                       hex      vs bg", theme.role("ui.fg_muted"))
    for c in theme.palette.values():
        ratio = contrast_ratio(c.rgb, page.rgb)
        clip = "  clipped: chroma reduced to fit sRGB" if c.clipped else ""
        cv.line([
            (f"  {c.name:<16}  ", None, None, ""),
            ("        ", None, c, ""),
            (f"  {str(c.oklch):<26}  {c.hex}  {ratio:5.2f}", None, None, ""),
            (clip, theme.role("diagnostic.warning"), None, ""),
        ])


# ==============================================================================
# Contrast matrix
# ==============================================================================

# Rows are *distinct colours* used as foreground roles, labelled with every
# role that uses them, so aliasing (e.g. keyword == keyword_control) is visible
# and the matrix stays short. Columns are the surfaces text actually sits on.

SURFACES = [
    "ui.bg", "ui.cursorline", "ui.selection", "ui.float",
    "ui.search", "diagnostic.error_bg", "diagnostic.diff_add_bg", "diagnostic.diff_change_bg",
]

# Roles that are never body text are held to the 3.0 UI threshold, not 4.5.
UI_ONLY_ROLES = {"ui.fg_subtle", "ui.line_number", "ui.border"}

# Background roles never make sense as foreground rows.
_BG_ROLE_RE = re.compile(r"(^|_)(bg|cursorline|selection|statusline|float|search|match_paren|border)$|_bg$")


def foreground_rows(theme: Theme) -> list[tuple[Color, list[str]]]:
    rows: dict[str, tuple[Color, list[str]]] = {}
    for group in ("ui", "syntax", "diagnostic"):
        for role, color in theme.roles.get(group, {}).items():
            if _BG_ROLE_RE.search(role) or role == "cursor_text":
                continue
            rows.setdefault(color.name, (color, []))[1].append(f"{group}.{role}")
    return list(rows.values())


def section_contrast(cv: Canvas):
    theme = cv.theme
    cell = 12
    cv.heading("Contrast: foreground roles × surfaces  (WCAG ratio, ! = below threshold)")

    header = [("  " + " " * 12, None, None, "")]
    for s in SURFACES:
        header.append((f"{s.split('.')[1][:cell - 1]:^{cell}}", theme.role("ui.fg_muted"), None, ""))
    cv.line(header)

    for color, roles in foreground_rows(theme):
        minimum = UI_MIN if all(r in UI_ONLY_ROLES for r in roles) else BODY_MIN
        spans = [(f"  {color.name:<12}", color, None, "\x1b[1m")]
        for s in SURFACES:
            surface = theme.role(s)
            ratio = contrast_ratio(color.rgb, surface.rgb)
            spans.append((f"  Aa {ratio:4.1f}{verdict(ratio, minimum)}  ", color, surface, ""))
        cv.line(spans)
        # Role names on their own line: they're too long to fit beside cells.
        cv.text(f"    {', '.join(r.split('.', 1)[1] for r in roles)}", theme.role("ui.fg_subtle"))


# ==============================================================================
# ANSI section
# ==============================================================================

ANSI_NAMES = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]


def section_ansi(cv: Canvas):
    theme = cv.theme
    ansi = theme.roles["ansi"]
    page = theme.role("ui.bg")
    cv.heading("ANSI 16  (text on bg, then as background)")
    for prefix in ("", "bright_"):
        spans = [("  ", None, None, "")]
        for name in ANSI_NAMES:
            c = ansi[prefix + name]
            spans.append((f"{(prefix[:2] + name)[:9]:<9} ", c, None, ""))
        cv.line(spans)
        spans = [("  ", None, None, "")]
        for name in ANSI_NAMES:
            c = ansi[prefix + name]
            ratio = contrast_ratio(c.rgb, page.rgb)
            spans.append((f" {ratio:4.1f}   ", ansi["black"] if ratio < 3 else page, c, ""))
            spans.append((" ", None, None, ""))
        cv.line(spans)


# ==============================================================================
# Code sample
# ==============================================================================

# The sample is hand-annotated rather than parsed, so what you see is exactly
# which role each token uses, independent of any highlighter's taxonomy.
#
#   «role|text»            fg from `syntax.role`
#   «group.role|text»      fg from any role group
#   «fgrole/bgrole|text»   fg and bg overlay, each resolved the same way
#
# Unannotated text uses `ui.fg`.
#
# Comments are drawn italic (toggle with `i` in interactive mode). Italics are
# keyed on the *role name* in the markup, not on the resolved colour: several
# roles share a palette entry (comment and punctuation are both `fg2`), so
# comparing colours would italicize every token that happens to share it.

_MARK_RE = re.compile(r"«([\w./]+)\|(.*?)»")


def _resolve(theme: Theme, ref: str) -> Color:
    return theme.role(ref if "." in ref else f"syntax.{ref}")


COMMENT_ROLES = {"comment", "syntax.comment"}


def parse_sample(
    theme: Theme, raw: str
) -> list[list[tuple[str, Color | None, Color | None, bool]]]:
    """Split annotated lines into `(text, fg, bg, is_comment)` spans."""
    lines = []
    for src in raw.rstrip("\n").split("\n"):
        spans, pos = [], 0
        for m in _MARK_RE.finditer(src):
            if m.start() > pos:
                spans.append((src[pos : m.start()], None, None, False))
            fg_ref, _, bg_ref = m.group(1).partition("/")
            spans.append((
                m.group(2),
                _resolve(theme, fg_ref),
                _resolve(theme, bg_ref) if bg_ref else None,
                fg_ref in COMMENT_ROLES,
            ))
            pos = m.end()
        if pos < len(src):
            spans.append((src[pos:], None, None, False))
        lines.append(spans)
    return lines


def section_code(cv: Canvas, sample: Path, cursorline: int, italic_comments: bool):
    theme = cv.theme
    cv.heading(f"Code: {sample.name}  (cursorline {cursorline}, gutter, float statusline)")
    lines = parse_sample(theme, sample.read_text())
    gutter = len(str(len(lines))) + 2

    for n, spans in enumerate(lines, start=1):
        is_cursor = n == cursorline
        # The cursorline tint must sit behind every span without its own bg,
        # otherwise it would stop at the first highlighted token.
        line_bg = theme.role("ui.cursorline") if is_cursor else None
        num_fg = theme.role("ui.line_number_active" if is_cursor else "ui.line_number")
        row = [(f"{n:>{gutter}} ", num_fg, line_bg, "")]
        used = len(row[0][0])
        for text, f, b, is_comment in spans:
            extra = "\x1b[3m" if is_comment and italic_comments else ""
            row.append((text, f, b or line_bg, extra))
            used += len(text)
        if is_cursor:
            row.append((" " * max(0, cv.width - used), None, line_bg, ""))
        cv.line(row)

    status = theme.role("ui.statusline")
    cv.line([
        (" NORMAL ", theme.role("ui.cursor_text"), theme.role("ui.cursor"), "\x1b[1m"),
        (f" {sample.name}  ", theme.role("ui.fg"), status, ""),
        ("● 1 ", theme.role("diagnostic.error"), status, ""),
        ("● 1 ", theme.role("diagnostic.warning"), status, ""),
        (" " * max(0, cv.width - 36), None, status, ""),
        (f"{cursorline}:1 ", theme.role("ui.fg_muted"), status, ""),
    ])


# ==============================================================================
# Entry point
# ==============================================================================


SECTIONS = ["palette", "contrast", "ansi", "code"]


def render(args: argparse.Namespace, theme_path: Path, banner: str | None = None) -> str:
    theme = load_theme(theme_path)
    cv = Canvas(theme, args.width)
    if banner:
        cv.text(banner, theme.role("ui.fg_strong"), "\x1b[1m")
    sections = args.sections or SECTIONS
    for s in sections:
        if s == "palette":
            section_palette(cv)
        elif s == "contrast":
            section_contrast(cv)
        elif s == "ansi":
            section_ansi(cv)
        elif s == "code":
            section_code(cv, args.sample, args.cursorline, args.italic_comments)
    cv.text()
    return cv.render()


# ==============================================================================
# Interactive mode: live reload and A/B toggle
# ==============================================================================

# `--watch` and `--compare` share one loop. It redraws when any watched file
# changes, and with `--compare` the space bar flips which theme is drawn.
#
# Each frame overwrites the previous one in place (cursor home, then clear only
# what's below the frame) rather than clearing the whole screen first. For A/B
# comparison this matters: a full clear flashes the terminal's own background
# between frames, and that flash is exactly what the eye latches onto instead
# of the colour difference being judged.
#
# The alternate screen buffer keeps the preview from polluting scrollback and
# restores the shell's screen on exit. Frames taller than the terminal will
# still scroll, which breaks the in-place overwrite; pick sections that fit
# (e.g. just `code`) when comparing.


def interactive(args: argparse.Namespace):
    paths = [args.theme] + ([args.compare] if args.compare else [])
    labels = "AB"
    active = 0

    # Keypresses are only readable when stdin is a terminal. Without one (piped
    # or backgrounded) we still live-reload, just without the toggle.
    is_tty = sys.stdin.isatty()
    if is_tty:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved_tty = termios.tcgetattr(fd)
        # cbreak: deliver keys immediately without Enter, but keep Ctrl-C.
        tty.setcbreak(fd)

    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
    try:
        last_stamp, dirty = None, True
        while True:
            # Poll mtimes rather than depend on inotify, so this works
            # unchanged on WSL-mounted Windows paths where inotify events are
            # unreliable.
            stamp = tuple(f.stat().st_mtime for f in (*paths, args.sample))
            if stamp != last_stamp:
                last_stamp, dirty = stamp, True

            if dirty:
                dirty = False
                italic = f"i: italic {'on' if args.italic_comments else 'off'}"
                if len(paths) > 1:
                    other = 1 - active
                    banner = (
                        f"  [{labels[active]}] {paths[active].name}"
                        f"    space: show [{labels[other]}] {paths[other].name}"
                        f"    {italic}    q: quit"
                    )
                else:
                    banner = f"  {paths[0].name}    {italic}    q: quit" if is_tty else None
                try:
                    frame = render(args, paths[active], banner)
                except Exception as e:  # keep watching through half-saved edits
                    frame = f"error in {paths[active]}: {e}"
                sys.stdout.write("\x1b[H" + frame + "\x1b[J")
                sys.stdout.flush()

            if not is_tty:
                time.sleep(0.3)
                continue
            ready, _, _ = select.select([sys.stdin], [], [], 0.3)
            if ready:
                key = sys.stdin.read(1)
                if key == " " and len(paths) > 1:
                    active, dirty = 1 - active, True
                elif key in ("i", "I"):
                    args.italic_comments, dirty = not args.italic_comments, True
                elif key in ("q", "Q"):
                    return
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")  # show cursor, main screen
        sys.stdout.flush()
        if is_tty:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sections", nargs="*", metavar="{palette,contrast,ansi,code}")
    p.add_argument("--theme", type=Path, default=DEFAULT_THEME_PATH)
    p.add_argument("--compare", type=Path, metavar="OTHER.toml",
                   help="second theme; space toggles between them (implies --watch)")
    p.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    p.add_argument("--cursorline", type=int, default=23)
    p.add_argument("--width", type=int, default=120)
    p.add_argument("--watch", action="store_true", help="re-render on file change")
    p.add_argument("--no-italic", dest="italic_comments", action="store_false",
                   help="draw comments upright (toggle with `i` in interactive mode)")
    args = p.parse_args()
    # Validated by hand: argparse `choices` rejects the empty default of nargs="*".
    for s in args.sections:
        if s not in SECTIONS:
            p.error(f"unknown section {s!r}; choose from {', '.join(SECTIONS)}")

    if args.watch or args.compare:
        interactive(args)
    else:
        print(render(args, args.theme))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
