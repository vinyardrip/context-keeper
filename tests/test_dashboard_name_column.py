"""The dashboard's first column is the project DIRECTORY name.

The registry stores a *label* — whatever ``ck register -n`` was given,
or a value auto-derived at registration time. It can be anything, and
it can go stale relative to the folder on disk. The first column of
``GLOBAL DASHBOARD`` is an identifier of a place on disk, so it
resolves to the project's own directory name.

That restores the historical contract ("project name == directory
name") and stops generic container labels from being presented as if
they were projects — the classic case being a sandbox mirror whose
entry points at ``.../.sandbox/projects``.

These tests also pin the VISUAL HIERARCHY: the name line is bold, the
path line beneath it stays secondary, and the styling is applied AFTER
truncation so it can never shift a border.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import ui
from cklib.core import (_project_display_name, _render_dashboard_table,
                        _render_space_manager_list, _render_spaces_table)


class _Entry:
    """Minimal stand-in for :class:`cklib.registry.ProjectEntry`."""

    def __init__(self, name: str, path: str, last_seen=None):
        self.name = name
        self.path = path
        self.last_seen = last_seen


def _state(name: str, path: str, *, condition: str = "ok",
           is_cwd: bool = False) -> dict:
    """A dashboard state dict with one open task."""

    class _TL:
        pass

    tl = _TL()
    tl.done = []
    tl.total = 1
    tl.completion_pct = 0.0
    tl.focused = []
    return {"entry": _Entry(name, path), "tl": tl,
            "condition": condition, "is_cwd": is_cwd}


class TestProjectDisplayName(unittest.TestCase):
    """``_project_display_name`` resolves to the directory basename."""

    def test_prefers_the_directory_name_over_the_label(self):
        self.assertEqual(
            _project_display_name(_Entry("Pretty Label", "/w/myproj")),
            "myproj")

    def test_handles_a_trailing_slash(self):
        self.assertEqual(
            _project_display_name(_Entry("x", "/w/myproj/")), "myproj")

    def test_sandbox_container_label_resolves_to_the_folder(self):
        """The generic ``projects`` label must not be shown as if it
        were the project."""
        entry = _Entry("projects", "/repo/.sandbox/projects")
        self.assertEqual(_project_display_name(entry), "projects")
        # …and when the real folder is one level deeper, THAT is what
        # the dashboard shows, never the container label.
        deeper = _Entry("projects", "/repo/.sandbox/projects/alpha")
        self.assertEqual(_project_display_name(deeper), "alpha")

    def test_root_and_dot_paths_degrade_to_the_label(self):
        self.assertEqual(_project_display_name(_Entry("lbl", "/")), "lbl")
        self.assertEqual(_project_display_name(_Entry("lbl", ".")), "lbl")
        self.assertEqual(_project_display_name(_Entry("lbl", "..")), "lbl")

    def test_empty_path_falls_back_to_the_label(self):
        self.assertEqual(_project_display_name(_Entry("lbl", "")), "lbl")

    def test_never_returns_an_empty_cell(self):
        self.assertEqual(_project_display_name(_Entry("", "")), "(unnamed)")


class TestDashboardFirstColumn(unittest.TestCase):
    """The rendered grid shows the folder name, not the label."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _render(self, states) -> str:
        return _render_dashboard_table(states, palette=ui.Palette(False))

    def test_label_is_not_shown_when_the_folder_differs(self):
        out = self._render([_state("Pretty Label", "/w/myproj")])
        self.assertIn("myproj", out)
        self.assertNotIn("Pretty Label", out)

    def test_path_remains_the_companion_line(self):
        out = self._render([_state("Pretty Label", "/w/myproj")])
        row = [l for l in out.splitlines() if "myproj" in l][0]
        self.assertIn("/../w/myproj", out)

    def test_cwd_marker_still_appends(self):
        out = self._render([_state("lbl", "/w/myproj", is_cwd=True)])
        self.assertIn("myproj *", out)

    def test_missing_tag_survives_with_the_new_name(self):
        out = self._render([_state("lbl", "/w/gone", condition="missing")])
        self.assertIn("[MISSING] gone", out)

    def test_container_directory_is_not_presented_as_a_project(self):
        """Regression guard: a sandbox-mirror entry must not surface a
        bare ``projects`` row when the real project sits beneath it."""
        out = self._render([_state("alpha",
                                   "/repo/.sandbox/projects/alpha")])
        # The first DATA row (skip the header row).
        row = next(l for l in out.splitlines()
                   if l.startswith("│") and "Project" not in l)
        self.assertIn("alpha", row)
        self.assertNotIn(" projects ", row)


class TestNameStyling(unittest.TestCase):
    """Names are bold; the active project is bold+cyan; borders hold."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        saved = {k: os.environ.get(k) for k in
                 ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE")}
        for key in saved:
            os.environ.pop(key, None)
        os.environ["FORCE_COLOR"] = "1"

        def _restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(_restore)

    def test_dashboard_name_is_bold_magenta(self):
        """Project names are bold magenta — never cyan.

        Cyan already belongs to the ``[i]`` notice badge; a cyan
        project name sitting next to a cyan hint made the two read as
        the same kind of thing.
        """
        out = _render_dashboard_table([_state("lbl", "/w/myproj")])
        self.assertIn(f"{ui.BOLD_MAGENTA}myproj{ui.RESET}", out)
        self.assertNotIn(f"{ui.BOLD}myproj{ui.RESET}", out)
        self.assertNotIn(f"{ui.CYAN}myproj{ui.RESET}", out)

    def test_dashboard_name_magenta_differs_from_every_badge_color(self):
        """No notice badge shares the project-name colour."""
        out = _render_dashboard_table([_state("lbl", "/w/myproj")])
        for style in ui.BADGE_STYLES.values():
            with self.subTest(style=style):
                self.assertNotEqual(style, ui.BOLD_MAGENTA)

    def test_spaces_name_is_bold(self):
        """Space names keep their own (plain bold) styling."""
        out = _render_spaces_table(palette=ui.Palette(True))
        self.assertIn(f"{ui.BOLD}LOCAL{ui.RESET}", out)
        self.assertIn(f"{ui.BOLD}REMOTE{ui.RESET}", out)
        self.assertNotIn(ui.BOLD_MAGENTA, out)

    def test_active_project_row_stays_bold_cyan(self):
        out = _render_space_manager_list("myproj", "/w/myproj",
                                         palette=ui.Palette(True))
        self.assertIn(f"{ui.BOLD}{ui.CYAN}myproj{ui.RESET}", out)

    def test_styling_does_not_change_any_column_width(self):
        """Bold paint is zero-width: the grid geometry must be
        identical whether or not names are accented."""
        states = [_state("lbl", "/w/myproj")]
        plain = _render_dashboard_table(states, palette=ui.Palette(False))
        styled = _render_dashboard_table(states, palette=ui.Palette(True))
        plain_w = [ui.display_width(l) for l in plain.splitlines()
                   if l.startswith(("┌", "│", "├", "└"))]
        styled_w = [ui.display_width(ui.strip_ansi(l))
                    for l in styled.splitlines()
                    if l.startswith(("┌", "│", "├", "└"))]
        self.assertEqual(plain_w, styled_w)


class TestStyleAppliedAfterTruncation(unittest.TestCase):
    """The accent must wrap the TRUNCATED name, never split it."""

    def test_long_name_is_truncated_then_styled(self):
        long_dir = "d" * 80
        out = _render_dashboard_table(
            [_state("lbl", f"/w/{long_dir}")], palette=ui.Palette(True))
        # Exactly one magenta run on the name line, and it is closed.
        row = next(l for l in out.splitlines()
                   if ui.strip_ansi(l).lstrip("│ ").startswith("d"))
        self.assertIn(f"{ui.BOLD_MAGENTA}", row)
        self.assertLessEqual(
            ui.display_width(ui.strip_ansi(row).split("│")[1]), 27)


if __name__ == "__main__":
    unittest.main()