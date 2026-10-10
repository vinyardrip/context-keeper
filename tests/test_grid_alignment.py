"""Tests for grid column alignment under wide / long content.

A fixed-width grid is only usable if every row occupies exactly the
same number of terminal COLUMNS. Two things break that:

- **code-point arithmetic.** ``len("非同期処理")`` is 4 but it paints
  8 columns. Truncating by ``len()`` therefore produces a string that
  OVERFLOWS its cell, and the row runs past the border — every ``│``
  below it shifts right and the table becomes unreadable;
- **over-long paths.** The contracted ``~/../parent/project`` form has
  a fixed two-segment shape; when the column budget is too small to
  hold both segments, the trimming floors used to emit a string
  WIDER than the budget.

The grid renderer now measures and clips in display columns, and
clamps every cell to its column as a structural last resort, so a
ragged table is impossible rather than merely unlikely.
"""

from __future__ import annotations

import unittest

from cklib import ui
from cklib.core import (_PROJECT_CAP, _render_grid, _smart_path,
                        _truncate_ellipsis, _truncate_right)

# Wide (East Asian) text: two columns per glyph.
CJK = "非同期処理のタスク"
# Mixed-width text, the realistic worst case.
MIXED = "task 日本語 mixed ascii 日本語 more"

HEADERS = ("Space", "Focus Task", "Progress", "Last Active")
KEYS = ("space", "focus", "progress", "last")


def _grid_widths(rendered: str) -> set[int]:
    """Display width of every grid line (borders + rows)."""
    return {
        ui.display_width(line)
        for line in rendered.splitlines()
        if line.startswith(("┌", "├", "└", "│"))
    }


class TestColumnAwareHelpers(unittest.TestCase):
    """``take_columns`` / ``fit_columns`` measure columns."""

    def test_take_columns_counts_columns_not_code_points(self):
        # Each CJK glyph is TWO columns, so a 3-column budget fits one
        # glyph (2 cols) — a second would need 4. Code-point slicing
        # would have returned two glyphs (4 columns).
        self.assertEqual(ui.take_columns(CJK, 3), "非")
        self.assertEqual(ui.display_width(ui.take_columns(CJK, 3)), 2)
        self.assertEqual(ui.display_width(ui.take_columns(CJK, 4)), 4)

    def test_take_columns_never_exceeds_width(self):
        for w in range(0, 12):
            for text in (CJK, MIXED, "ascii", ""):
                self.assertLessEqual(
                    ui.display_width(ui.take_columns(text, w)), w)

    def test_take_columns_from_end(self):
        self.assertEqual(ui.take_columns("abcdef", 3, from_end=True), "def")

    def test_fit_columns_returns_short_text_untouched(self):
        self.assertEqual(ui.fit_columns("short", 20), "short")
        # Exactly-fitting text is NOT truncated.
        self.assertEqual(ui.fit_columns("abcdef", 6, keep="start"),
                         "abcdef")

    def test_fit_columns_keep_start(self):
        self.assertEqual(ui.fit_columns("abcdef", 5, keep="start"),
                         "ab...")

    def test_fit_columns_keep_end(self):
        self.assertEqual(ui.fit_columns("abcdef", 5, keep="end"),
                         "...ef")

    def test_fit_columns_keep_both(self):
        self.assertEqual(ui.fit_columns("abcdef", 5, keep="both"),
                         "a...f")

    def test_fit_columns_never_exceeds_width_for_wide_text(self):
        for keep in ("start", "end", "both"):
            for w in range(0, 20):
                for text in (CJK, MIXED, CJK * 5):
                    out = ui.fit_columns(text, w, keep=keep)
                    self.assertLessEqual(
                        ui.display_width(out), w,
                        f"keep={keep} w={w} text={text!r} out={out!r}")

    def test_fit_columns_handles_styled_text(self):
        styled = ui.notice("[ok]", "done", ui.Palette(True))
        self.assertEqual(ui.display_width(ui.fit_columns(styled, 20)),
                         len("[ok] done"))


class TestTruncateHelpersAreColumnAware(unittest.TestCase):
    """The core truncation wrappers delegate to the column helpers."""

    def test_truncate_right_fits_wide_text(self):
        for w in range(0, 20):
            out = _truncate_right(CJK * 4, w)
            self.assertLessEqual(ui.display_width(out), w,
                                 f"w={w} out={out!r}")

    def test_truncate_ellipsis_fits_wide_text(self):
        for w in range(0, 20):
            out = _truncate_ellipsis(CJK * 4, w)
            self.assertLessEqual(ui.display_width(out), w,
                                 f"w={w} out={out!r}")

    def test_ascii_behaviour_is_unchanged(self):
        # The historical, documented single-width results.
        self.assertEqual(_truncate_right("abcdefgh", 5), "ab...")
        self.assertEqual(_truncate_ellipsis("abcdefgh", 6), "a...gh")
        self.assertEqual(_truncate_ellipsis("short", 20), "short")
        self.assertEqual(_truncate_ellipsis("abc", 1), ".")
        self.assertEqual(_truncate_ellipsis("abc", 0), "")
        # Truncation only kicks in when the text really is too wide.
        self.assertEqual(_truncate_right("abcd", 6), "abcd")

    def test_cyrillic_still_counts_one_column_each(self):
        self.assertEqual(
            ui.display_width(_truncate_right("Написать доп. шаблоны", 18)),
            18)


class TestSmartPathNeverOverflows(unittest.TestCase):
    """``_smart_path`` honours its budget at every width."""

    def test_never_exceeds_width(self):
        paths = [
            "/home/user/projects/alpha",
            "/home/user/deep/nested/tree/another/alpha",
            "/a/bb/ccccccccdddddddd",
            "/x/y", "/a", "/", "",
            "/home/user/" + CJK + "/project",
        ]
        for w in range(0, 30):
            for p in paths:
                out = _smart_path(p, w)
                self.assertLessEqual(
                    ui.display_width(out), w,
                    f"w={w} path={p!r} out={out!r}")

    def test_never_exceeds_the_real_project_cap(self):
        for p in ("/home/user/projects/alpha", "/a/bb/" + "x" * 60):
            self.assertLessEqual(
                ui.display_width(_smart_path(p, _PROJECT_CAP)), _PROJECT_CAP)

    def test_keeps_the_two_segment_shape_when_there_is_room(self):
        out = _smart_path("/home/user/projects/alpha", 25)
        self.assertIn("/", out)
        self.assertTrue(out.endswith("alpha") or out.endswith("a..."))


class TestGridNeverRendersRagged(unittest.TestCase):
    """The renderer itself guarantees a rectangular grid."""

    def _render(self, rows, caps=None, fixed=None, fluid="focus"):
        return _render_grid(
            "T", HEADERS, KEYS, rows,
            caps=caps if caps is not None else {"space": 25, "last": 16},
            fixed_widths=fixed if fixed is not None else {"progress": 9},
            fluid_key=fluid)

    def _assert_rectangular(self, rendered, label):
        widths = _grid_widths(rendered)
        self.assertEqual(len(widths), 1,
                         f"{label}: ragged grid widths {sorted(widths)}\n"
                         f"{rendered}")

    def test_wide_focus_title(self):
        rows = [{"space": ("LOCAL", "~/../spaces/local.md"),
                 "focus": ("[1] [>]", CJK * 6),
                 "progress": ("1/3", "(33.3%)"),
                 "last": ("just now",)}]
        self._assert_rectangular(self._render(rows), "wide focus title")

    def test_wide_space_name(self):
        rows = [{"space": (_truncate_ellipsis(CJK * 4, 25),
                           "~/../spaces/x.md"),
                 "focus": ("[1] [>]", "task"),
                 "progress": ("1/3", "(33.3%)"),
                 "last": ("2d ago",)}]
        self._assert_rectangular(self._render(rows), "wide space name")

    def test_wide_path_and_wide_focus(self):
        rows = [{"space": ("LOCAL",
                           _smart_path("/home/user/" + CJK + "/proj", 25)),
                 "focus": ("[1] [>]", MIXED * 3),
                 "progress": ("1/3", "(33.3%)"),
                 "last": ("2d ago",)}]
        self._assert_rectangular(self._render(rows), "wide path + focus")

    def test_overlong_ascii_path(self):
        rows = [{"space": ("LOCAL",
                           _smart_path("/home/user/" + "d" * 200 + "/p", 25)),
                 "focus": ("[1] [>]", "t" * 300),
                 "progress": ("1/3", "(33.3%)"),
                 "last": ("2d ago",)}]
        self._assert_rectangular(self._render(rows), "overlong ascii")

    def test_styled_cells_stay_rectangular(self):
        p = ui.Palette(True)
        rows = [{"space": (p.bold_cyan("MY PROJECT"), p.muted("~/../a/b")),
                 "focus": (p.bold_yellow("[1] [>]"),
                           p.muted(CJK * 5)),
                 "progress": ("1/3", "(33.3%)"),
                 "last": ("2d ago",)}]
        rendered = self._render(rows)
        self._assert_rectangular(rendered, "styled cells")
        # Stripping the styling must leave the same geometry.
        self._assert_rectangular(ui.strip_ansi(rendered), "styled stripped")

    def test_mixed_width_rows_all_rectangular(self):
        rows = []
        for i, title in enumerate((
                "ascii only title", CJK, MIXED, CJK * 8, "", "x",
                "а" * 40, CJK * 3 + "abc")):
            rows.append({
                "space": ("S" * i, _smart_path(f"/a/{'b' * i}", 25)),
                "focus": ("[1] [>]", title),
                "progress": (f"{i}/3", "(33.3%)"),
                "last": ("2d ago",),
            })
        self._assert_rectangular(self._render(rows), "mixed widths")

    def test_raw_overflow_in_a_fixed_width_column_is_clamped(self):
        """The Progress column is FIXED at 9 columns and its content is
        not pre-truncated by the callers, so a project with very many
        tasks is the one case the renderer itself must clamp. This
        pins the structural safety net independently of the
        truncation helpers.
        """
        rows = [{"space": ("LOCAL", "~/../spaces/local.md"),
                 "focus": ("[1] [>]", "task"),
                 "progress": ("100000/100000", "(100.0%)"),
                 "last": ("2d ago",)}]
        rendered = self._render(rows)
        self._assert_rectangular(rendered, "overflowing fixed column")

    def test_raw_overflow_in_a_capped_column_is_clamped(self):
        """Same safety net for a CAP-capped (not fixed) column fed
        content the caller never truncated."""
        rows = [{"space": ("X" * 80, "y" * 80),
                 "focus": ("[1] [>]", "task"),
                 "progress": ("1/3", "(33.3%)"),
                 "last": ("z" * 40,)}]
        self._assert_rectangular(self._render(rows), "capped overflow")


if __name__ == "__main__":
    unittest.main()