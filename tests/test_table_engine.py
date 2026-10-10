"""Tests that the Spaces table and the Global Dashboard share ONE engine.

There is exactly one grid in this codebase. ``GLOBAL DASHBOARD``,
``SPACES (GLOBAL CONTEXTS)`` and ``ck space list`` differ only in their
title and the label of the first column — every column width, every
truncation and every border is produced by the same code.

These tests pin that as a STRUCTURAL property (one call site into the
engine, one width policy, one cell builder) rather than as a snapshot of
today's output, so a future edit cannot quietly reintroduce a
Spaces-specific layout path.
"""

from __future__ import annotations

import ast
import io
import unittest
from pathlib import Path

from cklib import ui
from cklib.core import (_GRID_FIXED_WIDTHS, _GRID_FOCUS_WIDTH, _GRID_FLUID_KEY,
                        _GRID_KEYS, _LAST_WIDTH_CAP, _PROJECT_CAP,
                        _PROGRESS_WIDTH, _TABLE_BUDGET, _grid_headers,
                        _grid_row, _grid_total_width, _progress_cell,
                        _render_standard_grid, _smart_path,
                        _truncate_ellipsis, _truncate_right)

CORE = Path(__file__).resolve().parent.parent / "cklib" / "core.py"


def _geom(rendered: str) -> list[int]:
    """Display width of every grid line, in order."""
    return [ui.display_width(line) for line in rendered.splitlines()
            if line.startswith(("┌", "├", "└", "│"))]


def _cells(rendered: str, label: str) -> list[list[str]]:
    lines = rendered.splitlines()
    block: list[str] = []
    collecting = False
    for line in lines:
        if not line.startswith("│"):
            if collecting:
                break
            continue
        first = line.split("│")[1].strip()
        if not collecting:
            if first == label:
                collecting = True
                block.append(line)
        else:
            block.append(line)
    if not block:
        raise AssertionError(f"no row {label!r} in:\n{rendered}")
    n = len(block[0].split("│"))
    return [[l.split("│")[i].strip() for i in range(1, n - 1)] for l in block]


def _top_border(rendered: str) -> str:
    """The top rule of a grid — its outer boundary."""
    top = next(l for l in rendered.splitlines() if l.startswith("┌"))
    return top


def _seams(top_border: str) -> list[int]:
    """Character offsets of the interior column seams."""
    return [i for i, ch in enumerate(top_border) if ch in "┬┼"]


def _column_widths(rendered: str) -> list[int]:
    """Widths of the four data columns, read off the header row."""
    header = next(l for l in rendered.splitlines()
                  if l.startswith("│ Project") or l.startswith("│ Space"))
    # Each cell is " content " — two padding columns wider.
    return [ui.display_width(c) - 2 for c in header.split("│")[1:-1]]


class TestSingleEngineIsStructural(unittest.TestCase):
    """The unification is a property of the CODE, not of the output."""

    def setUp(self):
        self.tree = ast.parse(CORE.read_text(encoding="utf-8"))

    def _render_grid_call_sites(self) -> list:
        """Every call to ``_render_grid`` in the module."""
        out = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "_render_grid":
                out.append(node)
        return out

    def test_exactly_one_call_site_into_the_engine(self):
        """``_render_grid`` is reachable ONLY through the shared
        wrapper — no table can bypass it with its own layout."""
        calls = self._render_grid_call_sites()
        self.assertEqual(len(calls), 1,
                         "_render_grid must have a single call site")

    def test_the_single_call_site_is_the_shared_wrapper(self):
        parent = self._render_grid_call_sites()[0]
        enclosing = parent
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if parent in ast.walk(node) and (
                        parent.lineno >= node.lineno
                        and parent.lineno <= (node.end_lineno or 0)):
                    enclosing = node
        self.assertEqual(enclosing.name, "_render_standard_grid")

    def test_every_table_goes_through_the_shared_wrapper(self):
        tables = ("_render_dashboard_table", "_render_spaces_table",
                  "_render_space_manager_list")
        found = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in tables:
                calls = {getattr(c.func, "id", None) or
                         getattr(c.func, "attr", None)
                         for c in ast.walk(node)
                         if isinstance(c, ast.Call)}
                found[node.name] = calls
        self.assertEqual(set(found), set(tables))
        for name, calls in found.items():
            with self.subTest(table=name):
                self.assertIn("_render_standard_grid", calls)
                # No table builds its own cells inline any more.
                self.assertNotIn("_render_grid", calls)

    def test_no_per_table_key_constants_remain(self):
        """The shared key set replaced ``project``/``space`` keys, so
        no per-table key tuple can linger."""
        src = CORE.read_text(encoding="utf-8")
        for dead in ("_DASH_KEYS", "_SPACES_KEYS",
                     "_DASH_HEADERS", "_SPACES_HEADERS"):
            self.assertNotIn(dead, src, f"{dead} should be gone")


class TestSharedPolicy(unittest.TestCase):
    """One policy object, correct for every grid."""

    def test_every_column_has_an_exact_fixed_width(self):
        """No column may be content-derived — that is what let the two
        tables drift to different widths."""
        self.assertEqual(set(_GRID_FIXED_WIDTHS), set(_GRID_KEYS))
        self.assertEqual(_GRID_FIXED_WIDTHS["name"], _PROJECT_CAP)
        self.assertEqual(_GRID_FIXED_WIDTHS["focus"], _GRID_FOCUS_WIDTH)
        self.assertEqual(_GRID_FIXED_WIDTHS["progress"], _PROGRESS_WIDTH)
        self.assertEqual(_GRID_FIXED_WIDTHS["last"], _LAST_WIDTH_CAP)

    def test_focus_width_is_derived_from_the_budget(self):
        chrome = 5 + 8          # (n+1) separators + 2 padding per cell
        self.assertEqual(_GRID_FOCUS_WIDTH,
                         _TABLE_BUDGET - chrome - _PROJECT_CAP
                         - _PROGRESS_WIDTH - _LAST_WIDTH_CAP)

    def test_contracted_total_width_is_the_terminal_budget(self):
        self.assertEqual(_grid_total_width(), _TABLE_BUDGET)

    def test_headers_cover_the_key_set(self):
        for label in ("Project", "Space"):
            headers = _grid_headers(label)
            self.assertEqual(len(headers), len(_GRID_KEYS))
            self.assertEqual(headers[0], label)
            self.assertEqual(headers[1:], ("Focus Task", "Progress",
                                           "Last Active"))

    def test_a_grid_cannot_mutate_the_shared_policy(self):
        """The wrapper passes copies, so one table cannot poison the
        policy for the next render."""
        before = dict(_GRID_FIXED_WIDTHS)
        _render_standard_grid("X", "Space", [], palette=ui.Palette(False))
        self.assertEqual(_GRID_FIXED_WIDTHS, before)


class TestSharedCellBuilder(unittest.TestCase):
    """``_grid_row`` / ``_progress_cell`` are the only cell builders."""

    def test_row_shape_matches_the_key_set(self):
        row = _grid_row("alpha", "/h/u/p/alpha", "[1] [>]", "task",
                        _progress_cell(1, 2, 50.0), "2d ago")
        self.assertEqual(set(row), set(_GRID_KEYS))

    def test_row_applies_the_shared_truncation(self):
        long_title = "t" * 500
        row = _grid_row("n" * 500, "/h/" + "d" * 500, "[1] [>]",
                        long_title, _progress_cell(1, 2, 50.0), "2d ago")
        self.assertEqual(row["name"][0], _truncate_ellipsis("n" * 500,
                                                            _PROJECT_CAP))
        self.assertEqual(row["name"][1],
                         _smart_path("/h/" + "d" * 500, _PROJECT_CAP))
        self.assertEqual(row["focus"][1], _truncate_right(long_title, 80))

    def test_every_cell_fits_its_column(self):
        row = _grid_row("名前" * 40, "/家/" + "路" * 80, "[1] [>]",
                        "題" * 200, _progress_cell(1, 2, 50.0), "2d ago")
        self.assertLessEqual(ui.display_width(row["name"][0]), _PROJECT_CAP)
        self.assertLessEqual(ui.display_width(row["name"][1]), _PROJECT_CAP)
        self.assertLessEqual(ui.display_width(row["focus"][1]), 80)

    def test_progress_cell_is_two_lines(self):
        self.assertEqual(_progress_cell(2, 6, 33.3), ("2/6", "(33.3%)"))
        self.assertEqual(_progress_cell(100, 100, 100.0),
                         ("100/100", "(100.0%)"))

    def test_progress_cell_fits_the_fixed_width(self):
        for done, total, pct in ((0, 0, 0.0), (1, 3, 33.3), (100, 100, 100.0)):
            for line in _progress_cell(done, total, pct):
                self.assertLessEqual(
                    ui.display_width(line), _PROGRESS_WIDTH)


class TestIdenticalGeometryForIdenticalContent(unittest.TestCase):
    """Identical row data ⇒ identical column geometry, both labels."""

    CONTENT = (
        ("alpha", "/home/user/projects/alpha", "[3] [>]", "Active focus",
         (2, 6, 33.3), "33m ago"),
        ("a-very-long-project-name-here", "/home/user/deep/nested/tree/beta",
         "[1] [>]", "x" * 90, (10, 100, 10.0), "yesterday"),
        ("名前が長いプロジェクト", "/家/深い/道/γ", "[9] [>]", "題" * 60,
         (0, 1, 0.0), "just now"),
    )

    def _rows(self):
        return [_grid_row(n, p, h, t, _progress_cell(d, tt, pc), l)
                for n, p, h, t, (d, tt, pc), l in self.CONTENT]

    def test_project_and_space_grids_are_geometry_identical(self):
        as_dashboard = _render_standard_grid(
            "GLOBAL DASHBOARD", "Project", self._rows(),
            palette=ui.Palette(False))
        as_spaces = _render_standard_grid(
            "SPACES (GLOBAL CONTEXTS)", "Space", self._rows(),
            palette=ui.Palette(False))

        self.assertEqual(_geom(as_dashboard), _geom(as_spaces))

        # Layout: 0 title, 1 top border, 2 header row, 3 divider, ...
        # Everything but the title (0) and the header row (2) is
        # byte-identical; the header row differs only in its label, and
        # the labels differ in length, so it is compared by geometry.
        def body(rendered: str) -> list[str]:
            return rendered.splitlines()

        self.assertEqual(body(as_dashboard)[0], "GLOBAL DASHBOARD")
        self.assertEqual(body(as_spaces)[0], "SPACES (GLOBAL CONTEXTS)")
        for index in (1, 3, 4, 5, 6):
            self.assertEqual(body(as_dashboard)[index],
                             body(as_spaces)[index], f"line {index}")

        dash_header, space_header = body(as_dashboard)[2], body(as_spaces)[2]
        self.assertTrue(dash_header.startswith("│ Project"))
        self.assertTrue(space_header.startswith("│ Space"))
        self.assertEqual(ui.display_width(dash_header),
                         ui.display_width(space_header))

    def test_both_grids_are_rectangular(self):
        for label in ("Project", "Space"):
            out = _render_standard_grid("T", label, self._rows(),
                                        palette=ui.Palette(False))
            with self.subTest(label=label):
                self.assertEqual(len(set(_geom(out))), 1)

    def test_same_cells_land_in_the_same_columns(self):
        dash = _render_standard_grid("GLOBAL DASHBOARD", "Project",
                                     self._rows(), palette=ui.Palette(False))
        spc = _render_standard_grid("SPACES", "Space", self._rows(),
                                    palette=ui.Palette(False))
        for label in self.CONTENT[0][0], self.CONTENT[2][0]:
            self.assertEqual(_cells(dash, label), _cells(spc, label))


class TestOneHeaderLabelIsTheOnlyDifference(unittest.TestCase):
    def test_only_the_title_and_first_header_differ(self):
        rows = [_grid_row("alpha", "/h/u/p/alpha", "[1] [>]", "t",
                          _progress_cell(1, 2, 50.0), "2d ago")]
        dash = _render_standard_grid("GLOBAL DASHBOARD", "Project", rows,
                                     palette=ui.Palette(False)).splitlines()
        spc = _render_standard_grid("SPACES", "Space", rows,
                                    palette=ui.Palette(False)).splitlines()
        differing = [(i, a, b)
                     for i, (a, b) in enumerate(zip(dash, spc)) if a != b]
        # Layout: line 0 = title, line 1 = top border, line 2 = header
        # row. Only the title and the header label may differ.
        self.assertEqual([i for i, _a, _b in differing], [0, 2])
        header_a, header_b = differing[1][1], differing[1][2]
        self.assertIn("Project", header_a)
        self.assertIn("Space", header_b)


class TestRealRenderersAgree(unittest.TestCase):
    """End-to-end: the actual dashboard and spaces renderers agree."""

    def _state(self, name: str, path: str, done: int, total: int,
               condition: str = "ok"):
        class _Entry:
            pass

        entry = _Entry()
        entry.name = name
        entry.path = path
        entry.last_seen = None

        class _TL:
            pass

        tl = _TL()
        tl.done = list(range(done))
        tl.total = total
        tl.completion_pct = round(100.0 * done / total, 1) if total else 0.0
        tl.focused = []
        return {"entry": entry, "tl": tl, "condition": condition,
                "is_cwd": False}

    def test_real_renderers_are_identical_width(self):
        """THE assertion: the two REAL renderers must produce the
        same total width and the same column seams, whatever content
        they hold. This is the check that was previously weakened to
        accept a real defect.
        """
        from cklib.core import _render_dashboard_table, _render_spaces_table

        states = [
            self._state("alpha", "/home/user/projects/alpha", 2, 6),
            self._state("a-very-long-project-name", "/home/user/x/y/z/beta",
                        1, 100),
        ]
        dash = _render_dashboard_table(states, palette=ui.Palette(False))
        spaces = _render_spaces_table(palette=ui.Palette(False))

        self.assertEqual(dash.splitlines()[0], "GLOBAL DASHBOARD")
        self.assertEqual(spaces.splitlines()[0], "SPACES (GLOBAL CONTEXTS)")

        dash_top = _top_border(dash)
        spaces_top = _top_border(spaces)
        self.assertEqual(len(dash_top), len(spaces_top),
                         "tables must be the same overall width")
        self.assertEqual(len(dash_top), _grid_total_width())
        self.assertEqual(_seams(dash_top), _seams(spaces_top),
                         "column seams must sit at identical offsets")
        self.assertEqual(_column_widths(dash), _column_widths(spaces))

    def test_real_renderers_match_for_extreme_content(self):
        """Long names, CJK, huge counts — geometry must not move."""
        from cklib.core import _render_dashboard_table, _render_spaces_table

        states = [
            self._state("名前が長いプロジェクト名" * 5, "/家/" + "路" * 120,
                        0, 1),
            self._state("x" * 300, "/" + "y" * 300, 99999, 99999),
        ]
        dash = _render_dashboard_table(states, palette=ui.Palette(False))
        spaces = _render_spaces_table(palette=ui.Palette(False))

        self.assertEqual(len(_top_border(dash)), len(_top_border(spaces)))
        self.assertEqual(_seams(_top_border(dash)),
                         _seams(_top_border(spaces)))

    def test_every_content_case_renders_at_the_contract_width(self):
        from cklib.core import _render_standard_grid

        cases = [
            [],
            [("a", "/a", "[1] [>]", "t", (0, 0, 0.0), "n/a")],
            [("N" * 300, "/" + "p" * 300, "[1] [>]", "T" * 300,
              (99999, 99999, 100.0), "2d ago")] * 5,
            [("名前" * 60, "/家/" + "路" * 100, "[1] [>]", "題" * 200,
              (1, 2, 50.0), "yesterday")] * 3,
        ]
        for index, rows in enumerate(cases):
            built = [_grid_row(n, p, h, t, _progress_cell(d, tt, pc), l)
                     for n, p, h, t, (d, tt, pc), l in rows]
            for label in ("Project", "Space"):
                out = _render_standard_grid("T", label, built,
                                            palette=ui.Palette(False))
                with self.subTest(case=index, label=label):
                    self.assertEqual(set(_geom(out)), {_grid_total_width()})

    def test_both_real_renderers_stay_rectangular(self):
        from cklib.core import _render_dashboard_table, _render_spaces_table

        states = [
            self._state("名前が長いプロジェクト", "/家/深い/道/γ", 0, 1),
            self._state("missing-one", "/gone/away", 0, 0,
                        condition="missing"),
            self._state("x" * 200, "/" + "y" * 200, 7, 9),
        ]
        for name, out in (
                ("dashboard", _render_dashboard_table(
                    states, palette=ui.Palette(False))),
                ("spaces", _render_spaces_table(palette=ui.Palette(False)))):
            with self.subTest(table=name):
                self.assertEqual(len(set(_geom(out))), 1, out)

    def test_identical_content_yields_identical_total_width(self):
        """Same rows through the same engine ⇒ same width, whatever
        the first column is called."""
        rows = [_grid_row("alpha", "/h/u/p/alpha", "[3] [>]",
                          "Active focus task", _progress_cell(2, 6, 33.3),
                          "33m ago")]
        dash = _render_standard_grid("GLOBAL DASHBOARD", "Project", rows,
                                     palette=ui.Palette(False))
        spaces = _render_standard_grid("SPACES", "Space", rows,
                                       palette=ui.Palette(False))
        self.assertEqual(_geom(dash), _geom(spaces))


if __name__ == "__main__":
    unittest.main()