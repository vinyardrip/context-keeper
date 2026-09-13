"""Tests for Step 3 layout & formatting fixes.

1. Gap collapsing in ``ck st`` — consecutive gap IDs render as
   ranges ("3-8"), non-consecutive as "3-4, 8".
2. Dashboard table width — hard cap at ~85 chars total, Path and
   Focus Task columns dynamically truncated with ellipsis.
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
            out = _render_local_status(ck, tl, _FakeGit())
            self.assertIn("GAPS detected: 4-9", out)
            self.assertNotIn("GAPS detected: 4, 5, 6, 7, 8, 9", out)

    def test_status_no_gaps_line_when_clean(self):
        plan = "# P\n## Current Sprint\n- [ ] a\n- [x] b\n"
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), plan)
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, _FakeGit())
            self.assertNotIn("GAPS", out)


class _FakeGit:
    """Stub git module for status rendering tests."""

    @staticmethod
    def is_git_repo(path=None):
        return False


# ---------------------------------------------------------------------------
# 2. Dashboard width
# ---------------------------------------------------------------------------


class TestDashboardWidth(_IsolatedHome, unittest.TestCase):
    """Dashboard table must stay within ~85 columns."""

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

    def test_table_width_within_85(self):
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
                len(line), 85,
                f"table line exceeds 85 chars: {len(line)}: {line!r}",
            )
        # Path and Focus Task columns still present
        self.assertIn("Path", out)
        self.assertIn("Focus Task", out)

    def test_long_path_and_title_truncated_with_ellipsis(self):
        # Deep nested names produce very long resolved paths that
        # must be middle-truncated, never shown in full.
        out = self._render_with([
            "a-very-long-project-directory-name-that-keeps-going-on-and-on",
        ])
        self.assertIn("…", out)
        for line in out.splitlines():
            if line.startswith("|") or line.startswith("+"):
                self.assertLessEqual(len(line), 85)

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
    """``ck st`` shows PREVIOUS / FOCUS / NEXT."""

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
            return _render_local_status(ck, tl, _FakeGit())

    def test_full_tri_state(self):
        plan = (
            "# P\n## Current Sprint\n"
            "- [x] finished first\n"
            "- [>] focused task\n"
            "- [ ] next up\n"
        )
        out = self._status(plan)
        self.assertIn("PREVIOUS: [1] finished first", out)
        self.assertIn("FOCUS:    [2] [>] focused task", out)
        self.assertIn("NEXT:      [3] next up", out)

    def test_focus_falls_back_to_first_open(self):
        plan = "# P\n- [ ] only open task\n"
        out = self._status(plan)
        self.assertIn("FOCUS:    [1] only open task", out)
        self.assertIn("NEXT:      --", out)

    def test_no_tasks_all_dashes(self):
        out = self._status("# P\n")
        self.assertIn("PREVIOUS: --", out)
        self.assertIn("FOCUS:    --", out)
        self.assertIn("NEXT:      --", out)

    def test_next_skips_done_tasks(self):
        plan = "# P\n- [>] f\n- [x] d\n- [ ] future\n"
        out = self._status(plan)
        self.assertIn("NEXT:      [3] future", out)

    def test_previous_is_last_done(self):
        plan = "# P\n- [x] old\n- [x] newer\n- [ ] cur\n"
        out = self._status(plan)
        self.assertIn("PREVIOUS: [2] newer", out)

    def test_no_done_no_previous(self):
        plan = "# P\n- [ ] a\n"
        out = self._status(plan)
        self.assertIn("PREVIOUS: --", out)
        self.assertIn("FOCUS:    [1] a", out)


if __name__ == "__main__":
    unittest.main()
