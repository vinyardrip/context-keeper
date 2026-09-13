"""Tests for Step 3 layout & formatting fixes.

1. Gap collapsing in ``ck st`` — consecutive gap IDs render as
   ranges ("3-8"), non-consecutive as "3-4, 8".
2. Dashboard table — priority columns (Project | Focus Task |
   Progress | Last Active) with content-capped widths.
3. Tri-state view — PREVIOUS / FOCUS / NEXT lines in ``ck st``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.core import (
    ContextKeeper,
    _collapse_ids,
    _render_dashboard,
    _render_local_status,
    _truncate_ellipsis,
)
from cklib.models import TaskList, TaskStatus
from cklib.parser import parse_plan


class _IsolatedHome:
    """Pin the global registry to a per-test tmp HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._orig_dir = ckconfig.GLOBAL_CONFIG_DIR
        self._orig_file = ckconfig.GLOBAL_REGISTRY_FILE
        self._orig_legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE

        new_global_dir = fake_home / ".config" / "ck"
        ckconfig.GLOBAL_CONFIG_DIR = new_global_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_global_dir / "projects.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"
        ckregistry.GLOBAL_CONFIG_DIR = ckconfig.GLOBAL_CONFIG_DIR
        ckregistry.GLOBAL_REGISTRY_FILE = ckconfig.GLOBAL_REGISTRY_FILE
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = ckconfig.LEGACY_GLOBAL_CONFIG_FILE

    def tearDown(self):
        ckconfig.GLOBAL_CONFIG_DIR = self._orig_dir
        ckconfig.GLOBAL_REGISTRY_FILE = self._orig_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
        ckregistry.GLOBAL_CONFIG_DIR = self._orig_dir
        ckregistry.GLOBAL_REGISTRY_FILE = self._orig_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy


# ---------------------------------------------------------------------------
# 1. Gap collapsing
# ---------------------------------------------------------------------------


class TestCollapseIds(unittest.TestCase):
    """``_collapse_ids`` range-collapses consecutive runs."""

    def test_consecutive_run_collapses(self):
        self.assertEqual(_collapse_ids([3, 4, 5, 6, 7, 8]), "3-8")

    def test_mixed_consecutive_and_single(self):
        self.assertEqual(_collapse_ids([3, 4, 8]), "3-4, 8")

    def test_single_id(self):
        self.assertEqual(_collapse_ids([5]), "5")

    def test_multiple_singles(self):
        self.assertEqual(_collapse_ids([2, 5, 9]), "2, 5, 9")

    def test_empty(self):
        self.assertEqual(_collapse_ids([]), "")

    def test_unsorted_input_normalized(self):
        self.assertEqual(_collapse_ids([8, 3, 7, 4, 5, 6]), "3-8")

    def test_deduplicates(self):
        self.assertEqual(_collapse_ids([3, 3, 4]), "3-4")

    def test_two_run_case(self):
        self.assertEqual(_collapse_ids([1, 2, 3, 7, 8]), "1-3, 7-8")


class TestGapDisplay(_IsolatedHome, unittest.TestCase):
    """``ck st`` renders collapsed gaps."""

    def _ck(self, tmp: Path, plan: str) -> ContextKeeper:
        root = tmp / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(plan, encoding="utf-8")
        return ck

    def test_status_shows_collapsed_gap_range(self):
        plan = (
            "# P\n## Current Sprint\n"
            + "\n".join(f"- [ ] task {i}" for i in range(1, 3))
            + "\n"
            + "\n".join(f"- [x] done {i}" for i in range(3, 4))
            + "\n"
            + "\n".join(f"- [ ] late {i}" for i in range(4, 10))
            + "\n"
        )
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), plan)
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            gaps = tl.gap_ids()
            self.assertEqual(gaps, [4, 5, 6, 7, 8, 9])
            out = _render_local_status(ck, tl)
            # Gaps fold compactly into the progress line.
            self.assertIn("(gaps: 4-9)", out)
            self.assertNotIn("GAPS detected", out)
            self.assertNotIn("4, 5, 6, 7, 8, 9", out)

    def test_status_no_gaps_when_clean(self):
        plan = "# P\n## Current Sprint\n- [ ] a\n- [x] b\n"
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), plan)
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl)
            self.assertNotIn("gaps", out)

    def test_status_structure_and_clutter_free(self):
        """Spec-exact structure: 61-char bars, Russian progress
        line, vertical triad, and NO TOOLS/BRANCH/RECENT NOTES."""
        plan = "# P\n## Current Sprint\n- [ ] a\n"
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), plan)
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl)

        lines = out.splitlines()
        bar = "═" * 61
        self.assertEqual(lines[0], bar)
        self.assertEqual(lines[-1], bar)
        # Header shows the project ROOT dir name, not the plan title.
        self.assertTrue(lines[1].startswith(" 🚀 project [v"),
                        f"unexpected header: {lines[1]!r}")
        self.assertIn("задач сделано", lines[2])
        # No clutter blocks.
        self.assertNotIn("TOOLS", out)
        self.assertNotIn("BRANCH", out)
        self.assertNotIn("RECENT NOTES", out)
        self.assertNotIn("[end]", out)


# ---------------------------------------------------------------------------
# 2. Dashboard width
# ---------------------------------------------------------------------------


class TestDashboardWidth(_IsolatedHome, unittest.TestCase):
    """Dashboard table columns are content-capped before layout.

    Focus Task titles truncate at ~90 chars (spec band 80-100) and
    project names at 40, so every column stays naturally bounded.
    """

    def _render_with(self, names: list) -> str:
        with tempfile.TemporaryDirectory() as td:
            rows = [
                _Entry(Path(td), n, "2026-09-07T16:04:00+00:00")
                for n in names
            ]
            return _render_dashboard(
                None,
                list_projects=lambda: rows,
                parse_plan_file=lambda p: None,
            )

    def test_column_priority_order(self):
        """Header columns: Project | Focus Task | Progress | Last Active."""
        out = self._render_with(["p"])
        header = [l for l in out.splitlines() if l.startswith("| Project")]
        self.assertEqual(len(header), 1)
        cells = [c.strip() for c in header[0].strip("|").split("|")]
        self.assertEqual(
            cells, ["Project", "Focus Task", "Progress", "Last Active"]
        )

    def test_focus_column_truncated_at_cap(self):
        """Extreme focus titles truncate with a middle ellipsis, never
        blow up the table width."""
        from cklib.core import _safe_parse_plan

        with tempfile.TemporaryDirectory() as td:
            # Build a registry entry whose focused-task title is
            # ~120 chars via a real project on disk.
            root = Path(td) / "longtitle"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.init()
            ck.add_task("x" * 120)
            ck.start(2)
            ck.register(path=root, name="longtitle")
            out = _render_dashboard(
                ck,
                list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse_plan,
            )
            # Title capped at 90 chars total for the focus cell.
            data = [l for l in out.splitlines()
                    if l.startswith("| longtitle")]
            self.assertTrue(data)
            focus_cell = data[0].split("|")[2]
            self.assertLessEqual(len(focus_cell.strip()), 90)
            self.assertIn("…", focus_cell)

    def test_long_project_name_truncated(self):
        out = self._render_with([
            "a-very-long-project-directory-name-that-keeps-going-on-and-on",
        ])
        # Project names cap at 40 chars.
        data = [l for l in out.splitlines()
                if l.startswith("| a-very-long")]
        self.assertTrue(data)
        project_cell = data[0].split("|")[1]
        self.assertLessEqual(len(project_cell.strip()), 40)
        self.assertIn("…", project_cell)

    def test_short_data_fits_without_ellipsis(self):
        out = self._render_with(["p"])
        self.assertNotIn("…", out)
        self.assertIn("| p ", out)

    def test_table_lines_bounded(self):
        """Every table line stays under a compact bound (~170 chars:
        90 focus + 40 project + short progress/last + chrome)."""
        out = self._render_with([
            "context-keeper",
            "my-second-project-with-a-very-long-name",
        ])
        table_lines = [
            l for l in out.splitlines()
            if l.startswith("|") or l.startswith("+")
        ]
        self.assertTrue(table_lines)
        for line in table_lines:
            self.assertLessEqual(
                len(line), 170,
                f"table line exceeds 170 chars: {len(line)}: {line!r}",
            )

    def test_short_data_fits_without_ellipsis(self):
        out = self._render_with(["p"])
        self.assertNotIn("…", out)
        self.assertIn("| p ", out)

    def test_single_table_line_stays_bounded(self):
        out = self._render_with(["p"])
        for line in out.splitlines():
            if line.startswith("|") or line.startswith("+"):
                self.assertLessEqual(len(line), 85)


class _Entry:
    """Duck-typed registry entry for dashboard rendering tests.

    Creates the project folder on disk (under the test sandbox) so
    the dashboard renders it as "on disk" rather than "missing".
    """

    def __init__(self, parent: Path, name: str, last_seen: str):
        p = parent / name
        p.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.path = str(p.resolve())
        self.last_seen = last_seen


class TestTruncateEllipsis(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(_truncate_ellipsis("short", 20), "short")

    def test_long_text_keeps_start_and_end(self):
        text = "/home/user/projects/context-keeper/sub/module/deep/file.py"
        out = _truncate_ellipsis(text, 20)
        self.assertEqual(len(out), 20)
        self.assertTrue(out.startswith("/home"))
        self.assertTrue(out.endswith(".py"))

    def test_never_exceeds_width(self):
        text = "x" * 200
        for w in (3, 5, 10, 30):
            out = _truncate_ellipsis(text, w)
            self.assertLessEqual(len(out), w)

    def test_width_zero_and_one(self):
        self.assertEqual(_truncate_ellipsis("abc", 1), "…")
        self.assertEqual(_truncate_ellipsis("abc", 0), "")


# ---------------------------------------------------------------------------
# 3. Tri-state view
# ---------------------------------------------------------------------------


class TestTriStateView(_IsolatedHome, unittest.TestCase):
    """``ck st`` renders the vertical КОНТЕКСТ РАБОТЫ triad."""

    def _ck(self, tmp: Path, plan: str) -> ContextKeeper:
        root = tmp / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(plan, encoding="utf-8")
        return ck

    def _status(self, plan: str) -> str:
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), plan)
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            return _render_local_status(ck, tl)

    def test_full_vertical_triad(self):
        plan = (
            "# P\n## Current Sprint\n"
            "- [x] finished first\n"
            "- [>] focused task\n"
            "- [ ] next up\n"
        )
        out = self._status(plan)
        self.assertIn("🎯 КОНТЕКСТ РАБОТЫ:", out)
        # Vertical order: PREV line above FOCUS line above NEXT line.
        idx_prev = out.index("⏮️  [1] finished first [x]")
        idx_focus = out.index("👉 [2] [>] focused task")
        idx_next = out.index("⏭️  [3] next up [ ]")
        self.assertLess(idx_prev, idx_focus)
        self.assertLess(idx_focus, idx_next)

    def test_prev_is_nearest_done_before_focus(self):
        plan = "# P\n- [x] old\n- [x] nearer\n- [>] cur\n- [x] after\n"
        out = self._status(plan)
        # Nearest [x] PRIOR to focus — not the last done overall.
        self.assertIn("⏮️  [2] nearer [x]", out)
        self.assertNotIn("[3] cur [x]", out)

    def test_no_prev_hint(self):
        out = self._status("# P\n- [>] only focus\n")
        self.assertIn("⏮️  (нет завершенных)", out)

    def test_no_focus_hint(self):
        """Without an explicit [>], show the usage hint (no fallback
        to the first open task)."""
        out = self._status("# P\n- [ ] only open task\n")
        self.assertIn("👉 (фокус не выбран — используйте 'ck start <id>')", out)
        self.assertNotIn("👉 [1]", out)

    def test_no_next_hint(self):
        out = self._status("# P\n- [>] all done after\n- [x] done\n")
        self.assertIn("⏭️  (нет открытых задач)", out)

    def test_no_tasks_all_hints(self):
        out = self._status("# P\n")
        self.assertIn("⏮️  (нет завершенных)", out)
        self.assertIn("фокус не выбран", out)
        self.assertIn("⏭️  (нет открытых задач)", out)

    def test_next_skips_done_tasks(self):
        plan = "# P\n- [>] f\n- [x] d\n- [ ] future\n"
        out = self._status(plan)
        self.assertIn("⏭️  [3] future [ ]", out)

    def test_next_falls_back_to_first_open_without_focus(self):
        """No [>] set: NEXT still shows the first open task."""
        plan = "# P\n- [x] d\n- [ ] first open\n- [ ] second\n"
        out = self._status(plan)
        self.assertIn("⏭️  [2] first open [ ]", out)

    def test_prev_without_focus_is_last_done(self):
        plan = "# P\n- [x] old\n- [x] newer\n- [ ] cur\n"
        out = self._status(plan)
        self.assertIn("⏮️  [2] newer [x]", out)

    def test_exact_spec_layout(self):
        """Byte-exact rendering of the spec structure (gap-free plan
        so the progress line stays compact)."""
        from cklib.config import VERSION
        plan = (
            "# Demo\n"
            "- [x] prev task\n"
            "- [>] focus task\n"
            "- [ ] next task\n"
            "- [x] later done\n"
        )
        out = self._status(plan)
        bar = "═" * 61
        expected = "\n".join([
            bar,
            f" 🚀 project [v{VERSION}]",
            " 📊 Прогресс: 2/4 задач сделано (50.0%)",
            "",
            " 🎯 КОНТЕКСТ РАБОТЫ:",
            "    ⏮️  [1] prev task [x]",
            "    👉 [2] [>] focus task",
            "    ⏭️  [3] next task [ ]",
            bar,
        ])
        self.assertEqual(out, expected)


if __name__ == "__main__":
    unittest.main()
