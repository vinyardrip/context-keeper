"""ONE notice/banner engine, ONE semantic color table.

The CLI grew its warnings, hints, badges and framed banners in
different places, and the sandbox banner in particular hand-rolled its
own box drawing and its own yellow. Two copies of "what does a warning
look like" is exactly how a banner ends up a slightly different shade
from the ``[!]`` badge two lines above it.

This pins the centralised contract:

* :data:`cklib.ui.NOTICE_STYLES` is the single semantic table, and
  :data:`cklib.ui.BADGE_STYLES` is DERIVED from it, so a badge can
  never drift from its role;
* every style is ONE combined SGR run — never a split ``1m`` + ``33m``
  pair, which leaves a window where trailing text inherits a partial
  style;
* :func:`cklib.ui.frame` is the only box-drawing primitive: it measures
  in display columns, so no ANSI sequence can ever shift a border;
* the NAME colour is bold magenta everywhere — compact tables and
  verbose card headers alike — so cyan stays reserved for ``[i]``
  badges, hints and active-context markers.
"""

from __future__ import annotations

import unittest

from cklib import core, ui
from cklib.core import (_render_dashboard_table, _render_space_manager_list,
                        _render_spaces_table)

BADGES = ("[!]", "[i]", "[ok]", "[err]")

# The palette the spec mandates, per semantic role.
REQUIRED = {
    "warn": "\033[1;33m",   # [!] + sandbox banner: bold yellow
    "info": "\033[36m",     # [i]: cyan
    "ok": "\033[1;32m",     # [ok]: bold green
    "err": "\033[1;31m",    # [err]: bold red
}
PROJECT_COLOR = "\033[1;35m"  # bold magenta


class TestSemanticColorTable(unittest.TestCase):
    """``NOTICE_STYLES`` is the one source of truth."""

    def test_roles_have_the_mandated_colors(self):
        for role, want in REQUIRED.items():
            with self.subTest(role=role):
                self.assertEqual(ui.NOTICE_STYLES[role], want)

    def test_badges_are_derived_from_the_table(self):
        """A badge cannot have a color its role does not define."""
        pairs = {"[!]": "warn", "[i]": "info",
                 "[ok]": "ok", "[err]": "err"}
        for token, role in pairs.items():
            with self.subTest(token=token):
                self.assertEqual(ui.BADGE_STYLES[token],
                                 ui.NOTICE_STYLES[role])

    def test_every_style_is_a_single_combined_run(self):
        """No split SGR pairs: one run opens, one reset closes.

        A ``\\033[1m\\033[33m`` pair would leave a window where the
        trailing text inherits bold-but-uncolored state.
        """
        for role, style in ui.NOTICE_STYLES.items():
            with self.subTest(role=role):
                self.assertEqual(style.count("\033["), 1, style)
                self.assertNotIn("\033[1m\033[", style)
                self.assertTrue(style.startswith("\033["), style)

    def test_project_magenta_is_not_any_notice_color(self):
        """The clash the palette exists to prevent."""
        self.assertEqual(ui.BOLD_MAGENTA, PROJECT_COLOR)
        for role, style in ui.NOTICE_STYLES.items():
            with self.subTest(role=role):
                self.assertNotEqual(style, PROJECT_COLOR)


class TestNoticeIsFullLine(unittest.TestCase):
    """Badge and sentence share one styled run."""

    def test_one_style_one_reset_per_line(self):
        p = ui.Palette(True)
        for token in BADGES:
            with self.subTest(token=token):
                line = ui.notice(token, "the message", p)
                self.assertEqual(line.count("\033["), 2, line)
                self.assertTrue(line.startswith(ui.BADGE_STYLES[token]), line)
                self.assertTrue(line.endswith(ui.RESET), line)

    def test_message_is_styled_not_just_the_badge(self):
        line = ui.notice("[i]", "hint text", ui.Palette(True))
        self.assertIn("\033[36m[i] hint text", line)
        self.assertNotIn("[i]\033[0m hint text", line)

    def test_styling_costs_no_columns(self):
        p = ui.Palette(True)
        for token in BADGES:
            with self.subTest(token=token):
                self.assertEqual(ui.display_width(ui.notice(token, "msg", p)),
                                 len(f"{token} msg"))

    def test_disabled_palette_is_plain(self):
        p = ui.Palette(False)
        for token in BADGES:
            with self.subTest(token=token):
                self.assertEqual(ui.notice(token, "msg", p), f"{token} msg")


class TestFrameIsTheOnlyBoxPrimitive(unittest.TestCase):
    """Framing is centralised and immune to styling."""

    def test_frame_borders_are_uniform_width(self):
        out = ui.frame(["[!] SANDBOX MODE ACTIVE", "short", "a much wider row"],
                       palette=ui.Palette(False))
        widths = {ui.display_width(l) for l in out.splitlines()}
        self.assertEqual(len(widths), 1, out)

    def test_frame_widens_for_wide_glyphs_without_breaking(self):
        """CJK paths measure 2 columns each; the frame must follow."""
        out = ui.frame(["日本語のテキスト", "x"], palette=ui.Palette(False))
        widths = {ui.display_width(l) for l in out.splitlines()}
        self.assertEqual(len(widths), 1, out)

    def test_frame_paints_one_combined_run_per_line(self):
        out = ui.frame(["a", "b"], style=ui.NOTICE_STYLES["warn"],
                       palette=ui.Palette(True))
        for line in out.splitlines():
            with self.subTest(line=line):
                self.assertTrue(
                    line.startswith(ui.NOTICE_STYLES["warn"]), line)
                self.assertTrue(line.endswith(ui.RESET), line)
                self.assertEqual(line.count("\033["), 2, line)

    def test_frame_styling_does_not_change_geometry(self):
        rows = ["[!] x", "Active binary: /some/where/ck", "hint"]
        plain = ui.frame(rows, palette=ui.Palette(False))
        styled = ui.frame(rows, style=ui.NOTICE_STYLES["warn"],
                          palette=ui.Palette(True))
        self.assertEqual(
            [ui.display_width(l) for l in plain.splitlines()],
            [ui.display_width(ui.strip_ansi(l)) for l in styled.splitlines()])

    def test_frame_default_is_the_warn_color(self):
        out = ui.frame(["x"], palette=ui.Palette(True))
        self.assertTrue(out.startswith(ui.NOTICE_STYLES["warn"]), out)


class TestProjectNameColorIsMagenta(unittest.TestCase):
    """Project names never collide with the cyan ``[i]`` badge."""

    class _Entry:
        def __init__(self, name, path):
            self.name, self.path, self.last_seen = name, path, None

    class _TL:
        done, total, completion_pct, focused = [], 1, 0.0, []

    def _state(self, entry):
        return {"entry": entry, "tl": self._TL(), "condition": "ok",
                "is_cwd": False}

    def test_dashboard_project_name_is_bold_magenta(self):
        out = _render_dashboard_table(
            [self._state(self._Entry("lbl", "/w/myproj"))],
            palette=ui.Palette(True))
        self.assertIn(f"{PROJECT_COLOR}myproj{ui.RESET}", out)

    def test_dashboard_project_name_is_not_cyan_or_plain_bold(self):
        out = _render_dashboard_table(
            [self._state(self._Entry("lbl", "/w/myproj"))],
            palette=ui.Palette(True))
        self.assertNotIn(f"{ui.CYAN}myproj{ui.RESET}", out)
        self.assertNotIn(f"{ui.BOLD}myproj{ui.RESET}", out)

    def test_magenta_survives_truncation(self):
        out = _render_dashboard_table(
            [self._state(self._Entry("lbl", "/w/" + "d" * 80))],
            palette=ui.Palette(True))
        row = next(l for l in out.splitlines()
                   if ui.strip_ansi(l).lstrip("│ ").startswith("d"))
        self.assertIn(PROJECT_COLOR, row)
        self.assertLessEqual(
            ui.display_width(ui.strip_ansi(row).split("│")[1]), 27)

    def test_magenta_shifts_no_border(self):
        states = [self._state(self._Entry("lbl", "/w/myproj"))]
        plain = _render_dashboard_table(states, palette=ui.Palette(False))
        styled = _render_dashboard_table(states, palette=ui.Palette(True))
        geom = lambda t: [ui.display_width(ui.strip_ansi(l)) for l in
                          t.splitlines() if l[:1] in "┌│├└"]
        self.assertEqual(geom(plain), geom(styled))

    def test_space_names_use_magenta_not_cyan(self):
        """Magenta marks EVERY name — project and space alike.

        This is the unification rule at the table level: cyan belongs
        to ``[i]`` badges, hints and active-context markers only, and
        must never appear on a name cell.
        """
        out = _render_spaces_table(palette=ui.Palette(True))
        self.assertIn(f"{PROJECT_COLOR}LOCAL{ui.RESET}", out)
        self.assertIn(f"{PROJECT_COLOR}REMOTE{ui.RESET}", out)
        for name in ("LOCAL", "REMOTE"):
            with self.subTest(name=name):
                self.assertNotIn(f"{ui.CYAN}{name}{ui.RESET}", out)

    def test_no_name_style_selects_cyan(self):
        """The dispatch table exposes no cyan entry, by construction.

        An unknown style falls back to bold, so cyan cannot leak into a
        name cell through a typo either.
        """
        self.assertNotIn("cyan", core._NAME_PAINTERS)
        self.assertEqual(core._paint_name("X", "cyan", ui.Palette(True)),
                         f"{ui.BOLD}X{ui.RESET}")

    def test_active_project_row_is_not_plain_bold(self):
        out = _render_space_manager_list("myproj", "/w/myproj",
                                         palette=ui.Palette(True))
        self.assertNotIn(f"{ui.BOLD}myproj{ui.RESET}", out)


if __name__ == "__main__":
    unittest.main()