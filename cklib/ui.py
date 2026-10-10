"""Centralized ANSI color system.

One place decides whether ANSI SGR escape sequences are emitted and
what the canonical palette is (bold headers, green ``done`` states,
yellow/cyan active focus).

Color decision precedence (first match wins):

1. ``NO_COLOR`` set (non-empty, any value — https://no-color.org)
   → colors always OFF, nothing can override it. **This is the only
   opt-out.**
2. Otherwise → colors ALWAYS ON, including when stdout is a pipe or a
   file. ``FORCE_COLOR`` / ``CLICOLOR_FORCE`` are still honoured but
   are now redundant.

Rationale: the status badges (``[!]`` ``[i]`` ``[ok]`` ``[err]``) are
the primary severity channel of this CLI. Dropping them on a redirect
meant ``ck … > log.txt`` lost every success/failure signal, so TTY
detection is deliberately NOT consulted. A user who wants plain text
sets ``NO_COLOR``.

CONTRAST POLICY (dark/transparent-theme friendly):

- There are deliberately NO low-contrast tokens: no ``DIM`` and no
  hardcoded dark-gray (``90``) styling. Anything not explicitly
  styled INHERITS the terminal's native text color (no SGR emitted
  at all) — the color the user tuned their theme around.
- Secondary text (hints, progress labels, decorative bars) is
  rendered through the *slot* methods :meth:`Palette.muted`,
  :meth:`Palette.border`, :meth:`Palette.accent` and
  :meth:`Palette.text`. By default every slot maps to native
  inheritance; users can override any slot via the ``"colors"``
  mapping in the project's ``.ck.json`` (see
  :func:`cklib.config.read_color_config`), e.g.::

      {"colors": {"muted": "blue", "border": "bold cyan"}}

  Accepted values: standard ANSI names (``black`` … ``white``,
  ``bright-<name>``), an optional ``bold`` prefix, ``none`` for
  native inheritance, or a raw SGR parameter string (``38;5;208``).
- Status badges keep standard high-visibility colors (``GREEN``,
  ``YELLOW``, ``CYAN``, ``RED``) and are never mixed with DIM.

The decision is deliberately NOT cached: environment variables can
change within a process (tests patch ``os.environ``), and the check
itself costs microseconds — well under the latency budget.

Plain-text fallback: a disabled :class:`Palette` returns every input
unchanged, so renderers can call ``p.green(...)`` unconditionally
and byte-exact plain output is preserved when colors are off.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from typing import Any, Optional

# ---------------------------------------------------------------------------
# ANSI SGR codes (Select Graphic Rendition)
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"

# COMBINED bold+color sequences for status badges. Unlike
# ``bold_yellow`` & co. (which emit BOLD and the color as two separate
# SGR runs), a badge emits ONE combined parameter set so the whole
# badge is a single atomic styled token — important because the
# reset is written immediately after the badge itself and a split
# run would leave a window where the trailing text could inherit
# bold-but-uncolored state on terminals that re-scope per run.
BOLD_YELLOW = "\033[1;33m"
BOLD_GREEN = "\033[1;32m"
BOLD_RED = "\033[1;31m"

# Orange/amber banner: inverted, high-contrast background highlight
# (bold + amber background 43 + black foreground 30). Used for the
# bottom ``>> Backlog`` banner line of ``ck st``'s WORK CONTEXT so
# the remaining-work count visually pops as the block's last line on
# both dark and light terminal themes.
BANNER_BG = "\033[1;43;30m"

# Values treated as "on" for CLICOLOR_FORCE (mirrors cklib.sandbox).
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})

# Any SGR sequence: ESC [ <params> m — used by strip_ansi().
_ANSI_SGR_RE = re.compile(r"\033\[[0-9;]*m")

# Raw SGR parameter strings accepted in config overrides: digits and
# semicolons only ("94", "1;36", "38;5;208", "38;2;10;20;30", ...).
_RAW_SGR_RE = re.compile(r"[0-9][0-9;]*$")

# Standard ANSI foreground color names → SGR parameter.
COLOR_NAMES = {
    "black": "30",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "bright black": "90",
    "bright red": "91",
    "bright green": "92",
    "bright yellow": "93",
    "bright blue": "94",
    "bright magenta": "95",
    "bright cyan": "96",
    "bright white": "97",
}

# Config values that mean "inherit the terminal's native color".
_NATIVE_TOKENS = frozenset({"", "none", "default", "native", "reset"})

# Palette slots overridable via the project config.
PALETTE_SLOTS = ("text", "muted", "border", "accent")


def resolve_color(value: Any) -> Optional[str]:
    """Map a config color value to an SGR sequence.

    Returns the escape sequence (e.g. ``"\\033[1;36m"`` for
    ``"bold cyan"``), or None when the value means native terminal
    inheritance or is unrecognised — invalid values degrade
    gracefully to the user's native text color, never to a hardcoded
    low-contrast gray.

    Accepted forms (case-insensitive, ``-`` or space separated):

    - ``"none"`` / ``"default"`` / empty → None (native inheritance)
    - ``"cyan"``, ``"bright blue"``, ... → standard ANSI foreground
    - ``"bold"`` prefix → adds SGR parameter 1 (``"bold red"``)
    - raw SGR parameters: ``"94"``, ``"38;5;208"`` → passthrough
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in _NATIVE_TOKENS:
        return None
    tokens = v.replace("-", " ").split()
    params: list[str] = []
    if tokens[0] == "bold":
        params.append("1")
        tokens = tokens[1:]
    if tokens:
        name = " ".join(tokens)
        if _RAW_SGR_RE.match(name):
            params.append(name)
        else:
            code = COLOR_NAMES.get(name)
            if code is None:
                return None  # unknown name → native fallback
            params.append(code)
    if not params:
        return None
    return f"\033[{';'.join(params)}m"


def color_enabled(stream: Any = None) -> bool:
    """Return True when ANSI colors should be emitted.

    COLOR IS THE DEFAULT, NOT A LUXURY. Status badges are the only
    way a user can tell a success from a failure at a glance, so they
    keep their color **even when stdout is a pipe or a file**. Suppressing
    them on a redirect is what made ``ck … > log.txt`` silently drop
    every severity signal.

    ``NO_COLOR`` is therefore the ONLY opt-out, and it wins absolutely:

    - ``NO_COLOR`` set (non-empty)   -> False
    - ``FORCE_COLOR`` / ``CLICOLOR_FORCE`` -> True (redundant, kept for
      compatibility with callers that set them explicitly)
    - otherwise                      -> True

    ``stream`` is accepted for call-site compatibility and no longer
    affects the decision: TTY detection is deliberately NOT consulted.
    Never raises.
    """
    if os.environ.get("NO_COLOR"):
        # Highest precedence: an explicit opt-out cannot be forced back on.
        return False
    # Everything else is colored. ``stream`` is unused on purpose.
    return True


class Palette:
    """ANSI palette bound to a single enabled/disabled decision.

    A disabled palette is the identity transform: every method
    returns its argument untouched. This keeps renderers free of
    ``if color:`` branching — plain-text mode stays byte-exact.

    CONTRAST: the slot methods (:meth:`text`, :meth:`muted`,
    :meth:`border`, :meth:`accent`) default to NATIVE inheritance —
    the text is emitted with no SGR sequence, so the terminal's own
    foreground color shows through on any theme. ``colors`` maps
    slot names to config values (see :func:`resolve_color`); the
    ``text`` slot acts as the fallback for the other slots when they
    are not set individually.
    """

    __slots__ = ("enabled", "_codes")

    def __init__(self, enabled: bool,
                 colors: Optional[dict] = None) -> None:
        self.enabled = enabled
        cfg = colors if isinstance(colors, dict) else {}
        base = resolve_color(cfg.get("text"))
        codes: dict[str, Optional[str]] = {"text": base}
        for slot in ("muted", "border", "accent"):
            own = resolve_color(cfg.get(slot))
            codes[slot] = own if own is not None else base
        self._codes = codes

    # ---- core ----

    def paint(self, text: str, *codes: str) -> str:
        """Wrap ``text`` in the given SGR ``codes`` + RESET."""
        if not self.enabled or not codes or not text:
            return text
        return "".join(codes) + text + RESET

    def _paint_slot(self, text: str, slot: str) -> str:
        """Paint ``text`` with the config-resolved code for ``slot``.

        An unset (or invalid) slot emits NO escape sequence: the
        terminal's native text color is inherited — never a
        hardcoded dark gray.
        """
        code = self._codes.get(slot)
        if code is None:
            return text
        return self.paint(text, code)

    # ---- slots (native inheritance by default) ----

    def text(self, text: str) -> str:
        """Primary body text (native terminal color by default)."""
        return self._paint_slot(text, "text")

    def muted(self, text: str) -> str:
        """Secondary text: hints, metadata, background context.

        Native terminal color by default (readable on every theme);
        override via the ``"muted"`` config key.
        """
        return self._paint_slot(text, "muted")

    def border(self, text: str) -> str:
        """Structural borders / divider lines (native by default)."""
        return self._paint_slot(text, "border")

    def accent(self, text: str) -> str:
        """Small emphasis metadata: version tags, path hints
        (native by default)."""
        return self._paint_slot(text, "accent")

    # ---- standard high-visibility badges (never DIM-mixed) ----

    def bold(self, text: str) -> str:
        """Section titles, headings."""
        return self.paint(text, BOLD)

    def green(self, text: str) -> str:
        """Completed / done states."""
        return self.paint(text, GREEN)

    def yellow(self, text: str) -> str:
        """Active focus (st)."""
        return self.paint(text, YELLOW)

    def cyan(self, text: str) -> str:
        """Active-task notes / informational emphasis."""
        return self.paint(text, CYAN)

    def red(self, text: str) -> str:
        """Errors / hard failures."""
        return self.paint(text, RED)

    def bold_yellow(self, text: str) -> str:
        return self.paint(text, BOLD, YELLOW)

    def bold_cyan(self, text: str) -> str:
        return self.paint(text, BOLD, CYAN)

    def bold_green(self, text: str) -> str:
        return self.paint(text, BOLD, GREEN)

    def bold_red(self, text: str) -> str:
        return self.paint(text, BOLD, RED)

    def banner(self, text: str) -> str:
        """Bottom banner line: orange/amber background highlight.

        Bold black-on-amber (SGR ``1;43;30``) — an inverted,
        high-contrast accent that visually pops as a full-line
        banner on both dark and light terminal themes. A disabled
        palette returns the text unchanged (plain-text fallback).
        """
        return self.paint(text, BANNER_BG)


def get_palette(stream: Any = None,
                colors: Optional[dict] = None) -> Palette:
    """Build a Palette for ``stream`` (default ``sys.stdout``).

    ``colors`` are config-resolved slot overrides (see
    :class:`Palette`); without them every slot inherits the
    terminal's native text color.
    """
    return Palette(color_enabled(stream), colors)


# Default colour for STRUCTURAL chrome — table rules, card dividers.
# Bright black renders as a quiet hairline that structures a table on
# dark themes without competing with its content; an explicit "border"
# key in the project's .ck.json always wins over it.
DEFAULT_BORDER_COLOR = "bright-black"


def get_chrome_palette(colors: Optional[dict] = None,
                       stream: Any = None) -> Palette:
    """Palette for STRUCTURAL chrome (table borders, card dividers).

    Same slots as :func:`get_palette`, with one deliberate difference:
    the ``border`` slot defaults to :data:`DEFAULT_BORDER_COLOR`
    (bright black) instead of inheriting the native text color, so
    grid rules and dividers read as a quiet frame rather than as more
    content.

    Explicit configuration still wins — a ``"border"`` key in the
    project's ``.ck.json`` (e.g. ``"white"``, ``"bold cyan"``,
    ``"none"``) is used verbatim, and ``"none"`` restores native
    inheritance.
    """
    cfg = dict(colors) if isinstance(colors, dict) else {}
    cfg.setdefault("border", DEFAULT_BORDER_COLOR)
    return Palette(color_enabled(stream), cfg)


def strip_ansi(text: str) -> str:
    """Remove every SGR escape sequence from ``text``.

    Inverse of :class:`Palette` painting — used by tests to assert
    that colored output is content-identical to plain output.
    """
    return _ANSI_SGR_RE.sub("", text)


# ---------------------------------------------------------------------------
# Status badges ([!] / [i] / [ok] / [err])
# ---------------------------------------------------------------------------

# Badge token text -> the combined SGR sequence that paints it.
#
# Every CLI notice opens with exactly one of these four badges, so a
# user can scan a wall of output by COLOR ALONE without reading the
# bracket text:
#
#   [!]   bold yellow — warnings / notices that need attention
#   [i]   cyan        — information, hints, guidance
#   [ok]  bold green  — success confirmations
#   [err] bold red    — errors / failed operations
#
# NONE of these are configurable: a badge's color is part of its
# identity, so ``[err]`` is never rendered in the user's "muted"
# slot color and can never be mistaken for a hint.
BADGE_STYLES: dict[str, str] = {
    "[!]": BOLD_YELLOW,
    "[i]": CYAN,
    "[ok]": BOLD_GREEN,
    "[err]": BOLD_RED,
}

# Convenience aliases for the four badge kinds.
WARN = "[!]"
INFO = "[i]"
OK = "[ok]"
ERR = "[err]"


def badge(token: str, palette: Optional[Palette] = None) -> str:
    """Render a single status badge token, styled.

    ``token`` is one of ``"[!]"`` / ``"[i]"`` / ``"[ok]"`` / ``"[err]"``
    (the :data:`WARN` / :data:`INFO` / :data:`OK` / :data:`ERR`
    aliases). The RESET is emitted IMMEDIATELY after the badge and
    BEFORE any message text, so the trailing sentence is rendered in
    the terminal's standard formatting — never bold, never colored.

    A disabled palette (or an unknown token) returns the plain token,
    so piped / ``NO_COLOR`` output stays byte-identical to the
    unstyled text.
    """
    style = BADGE_STYLES.get(token)
    p = palette if palette is not None else get_palette()
    if style is None or not p.enabled:
        return token
    return f"{style}{token}{RESET}"


def notice(token: str, message: str = "",
           palette: Optional[Palette] = None) -> str:
    """A complete ``badge`` + message line, ready for ``print``.

    The message is deliberately left UNSTYLED (see :func:`badge`) so
    only the badge carries color::

        [i] Run 'ck init' in a project directory or ...   (cyan [i])
        [ok] Added task (id=4): ship the release          (green [ok])

    ``message`` may be empty, in which case only the badge is
    emitted. Newlines inside ``message`` are preserved verbatim.
    """
    head = badge(token, palette)
    return f"{head} {message}" if message else head


# ---------------------------------------------------------------------------
# Display width (terminal columns)
# ---------------------------------------------------------------------------

def char_width(ch: str) -> int:
    """Number of terminal columns a single character occupies (0, 1, 2).

    ``len()`` counts code points, not columns, so wide and combining
    glyphs misalign fixed-width boxes. The rules here mirror common
    terminal behaviour:

    - East Asian Wide / Fullwidth characters occupy two columns;
    - combining marks, zero-width joiners and format characters
      occupy none;
    - the emoji presentation selector (U+FE0F) promotes its base
      glyph to the two-column emoji form, so it contributes one
      extra column (``\u26a0\ufe0f`` renders two columns wide).
    """
    if ch == "\ufe0f":
        return 1
    if ch in ("\u200b", "\u200d"):
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def display_width(text: str) -> int:
    """Total terminal column width of ``text`` (never negative).

    ANSI SGR escape sequences are stripped first: they are a transport
    detail of styling, not content, and occupy ZERO columns — counting
    them would misalign every painted line in a fixed-width box.
    """
    return sum(char_width(ch) for ch in _ANSI_SGR_RE.sub("", text))


def pad_to_width(text: str, width: int) -> str:
    """Left-align ``text`` in ``width`` DISPLAY columns (not code points)."""
    pad = width - display_width(text)
    return text + " " * pad if pad > 0 else text


def take_columns(text: str, width: int, *, from_end: bool = False) -> str:
    """The longest PREFIX (or SUFFIX) of ``text`` fitting ``width``
    display columns.

    The single primitive every width-aware truncation is built on.
    Counts COLUMNS, not code points: a CJK glyph costs two columns, so
    ``take_columns("非同期処理", 3)`` returns ``"非同"`` (3 columns, 2
    code points) rather than three full-width glyphs. A character
    that would straddle the boundary is DROPPED rather than split, so
    the result never exceeds ``width``.

    ``from_end`` takes the SUFFIX instead of the prefix.
    """
    if width <= 0:
        return ""
    source = reversed(text) if from_end else text
    out: list[str] = []
    used = 0
    for ch in source:
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    if from_end:
        out.reverse()
    return "".join(out)


def fit_columns(text: str, width: int, *, keep: str = "start",
                ellipsis: str = "...") -> str:
    """Truncate ``text`` to at most ``width`` DISPLAY columns.

    ``keep`` selects which part survives the cut:

    - ``"start"``  — head survives, marker at the end
      (``"abcdef"`` @ 6 → ``"abc..."``): the informative beginning of
      a task title or a path stays readable;
    - ``"end"``    — tail survives, marker at the front
      (``"abcdef"`` @ 6 → ``"...def"``);
    - ``"both"``   — head and tail both survive, marker in the middle
      (``"[3] [>] implement the very long...ring module"``): keeps
      the ``[<id>] [>]`` prefix AND the title's ending.

    Widths too small for the marker degrade to a run of dots, matching
    the historical behaviour for ``width <= 3``.

    The result is guaranteed to fit ``width`` columns for ANY input,
    including wide (CJK) text and ANSI-styled content.
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    marker_w = display_width(ellipsis)
    if width <= marker_w:
        return take_columns(ellipsis, width)
    room = width - marker_w
    if keep == "end":
        return ellipsis + take_columns(text, room, from_end=True)
    if keep == "both":
        head = room // 2
        tail = room - head
        return (take_columns(text, head)
                + ellipsis
                + take_columns(text, tail, from_end=True))
    return take_columns(text, room) + ellipsis


# ---------------------------------------------------------------------------
# Uninitialized / no-project state (read commands: ck st & co)
# ---------------------------------------------------------------------------

NO_PROJECT_HEADING = "No active project found."
NO_PROJECT_HINT = (
    "Run 'ck init' in a project directory or switch context with "
    "'ck register'."
)


def render_no_project(palette: Optional[Palette] = None) -> str:
    """Render the explicit no-project / uninitialized state.

    Returned by read commands when NO project context could be
    resolved — neither a local ``PLAN.md`` (own directory, an
    ancestor up to the Git repo root, or a dev-mode sandbox mirror)
    nor a usable project in the global registry. Replaces the old
    ambiguous empty dashboard (``0/0 tasks``, ``focus not selected``)
    with a clean two-line message; no fake metrics are emitted.

    The ``[!]`` badge goes through :func:`notice`, so it carries the
    standard bold-yellow badge style with the reset immediately after
    the token and the sentence in normal formatting. The hint line
    inherits the native terminal text color (no low-contrast gray).
    """
    p = palette if palette is not None else get_palette()
    return "\n".join([
        notice(WARN, NO_PROJECT_HEADING, p),
        p.muted(NO_PROJECT_HINT),
    ])


__all__ = [
    "RESET",
    "BOLD",
    "GREEN",
    "YELLOW",
    "CYAN",
    "RED",
    "BANNER_BG",
    "COLOR_NAMES",
    "PALETTE_SLOTS",
    "resolve_color",
    "Palette",
    "color_enabled",
    "get_palette",
    "strip_ansi",
    "char_width",
    "display_width",
    "pad_to_width",
    "take_columns",
    "fit_columns",
    "BADGE_STYLES",
    "WARN",
    "INFO",
    "OK",
    "ERR",
    "badge",
    "notice",
    "NO_PROJECT_HEADING",
    "NO_PROJECT_HINT",
    "render_no_project",
]
