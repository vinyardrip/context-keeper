"""Centralized ANSI color system.

One place decides whether ANSI SGR escape sequences are emitted and
what the canonical palette is (bold headers, green ``done`` states,
yellow/cyan active focus).

Color decision precedence (first match wins):

1. ``NO_COLOR`` set (non-empty, any value — https://no-color.org)
   → colors always OFF, nothing can override it.
2. ``FORCE_COLOR`` set (non-empty, not ``0``) → colors ON even when
   piped, for readers that render ANSI (``less -R``, pagers, logs).
3. ``CLICOLOR_FORCE`` truthy (``1``/``true``/``yes``/``on``) → same
   as FORCE_COLOR.
4. Otherwise: the target stream is a TTY (``sys.stdout.isatty()``)
   → ON interactively, OFF for pipes/redirects.

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
    """Return True when ANSI colors should be emitted to ``stream``.

    ``stream`` defaults to ``sys.stdout``. See the module docstring
    for the precedence rules (``NO_COLOR`` > ``FORCE_COLOR`` /
    ``CLICOLOR_FORCE`` > TTY detection). Never raises: a stream
    without ``isatty`` (or one that fails the probe) is treated as
    non-interactive.
    """
    if os.environ.get("NO_COLOR"):
        # Highest precedence: an explicit opt-out cannot be forced back on.
        return False
    force = os.environ.get("FORCE_COLOR")
    if force and force != "0":
        return True
    if os.environ.get("CLICOLOR_FORCE", "").lower() in _TRUTHY_ENV:
        return True
    if stream is None:
        stream = sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


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


def get_palette(stream: Any = None,
                colors: Optional[dict] = None) -> Palette:
    """Build a Palette for ``stream`` (default ``sys.stdout``).

    ``colors`` are config-resolved slot overrides (see
    :class:`Palette`); without them every slot inherits the
    terminal's native text color.
    """
    return Palette(color_enabled(stream), colors)


def strip_ansi(text: str) -> str:
    """Remove every SGR escape sequence from ``text``.

    Inverse of :class:`Palette` painting — used by tests to assert
    that colored output is content-identical to plain output.
    """
    return _ANSI_SGR_RE.sub("", text)


# ---------------------------------------------------------------------------
# Uninitialized / no-project state (read commands: ck st & co)
# ---------------------------------------------------------------------------

NO_PROJECT_HEADING = "[!] No active project found."
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

    The hint line inherits the native terminal text color (no
    low-contrast gray).
    """
    p = palette if palette is not None else get_palette()
    return "\n".join([
        p.bold_yellow(NO_PROJECT_HEADING),
        p.muted(NO_PROJECT_HINT),
    ])


__all__ = [
    "RESET",
    "BOLD",
    "GREEN",
    "YELLOW",
    "CYAN",
    "RED",
    "COLOR_NAMES",
    "PALETTE_SLOTS",
    "resolve_color",
    "Palette",
    "color_enabled",
    "get_palette",
    "strip_ansi",
    "NO_PROJECT_HEADING",
    "NO_PROJECT_HINT",
    "render_no_project",
]
