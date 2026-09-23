"""Tests for Step 3 layout & formatting fixes.

1. Gap collapsing in ``ck st`` — consecutive gap IDs render as
   ranges ("3-8"), non-consecutive as "3-4, 8" (progress line);
   the WORK CONTEXT block lists skipped tasks by NAME.
2. Dashboard table — priority columns (Project | Focus Task |
   Progress | Last Active) with content-capped widths.
3. WORK CONTEXT — ordered structure (Done / conditional Skipped /
   Focus|Next / Upcoming / optional Unfocused / Paused Context /
   Backlog bottom banner) where ``Skipped`` only appears on a real
   execution gap, sequential pending tasks stay in the queue, and
   the Backlog banner reports TOTAL − Done as the very last line.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import ui as ckui
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

    def test_status_no_gaps_for_sequential_pending(self):
        """Sequential IDs 1..9 (pending after done) are NOT gaps."""
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
            self.assertEqual(tl.gap_ids(), [])
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
            self.assertNotIn("(gaps:", out)
            self.assertNotIn("GAPS detected", out)

    def test_status_shows_collapsed_gap_range(self):
        """A literal missing ID still folds into the progress line."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), "# P\n- [ ] a\n- [ ] b\n- [ ] c\n")
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            tl.remove(2)  # IDs become 1, 3
            self.assertEqual(tl.gap_ids(), [2])
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
            self.assertIn("(gaps: 2)", out)

    def test_status_no_gaps_when_clean(self):
        plan = "# P\n## Current Sprint\n- [ ] a\n- [x] b\n"
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), plan)
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
            self.assertNotIn("gaps", out)

    def test_status_structure_and_clutter_free(self):
        """Spec-exact structure: 61-char ASCII bars, English progress
        line, vertical triad, and NO TOOLS/BRANCH/RECENT NOTES."""
        plan = "# P\n## Current Sprint\n- [ ] a\n"
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), plan)
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))

        lines = out.splitlines()
        bar = "=" * 61
        self.assertEqual(lines[0], bar)
        self.assertEqual(lines[-1], bar)
        # Header shows the project ROOT dir name, not the plan title.
        self.assertTrue(lines[1].startswith(" > project [v"),
                        f"unexpected header: {lines[1]!r}")
        self.assertIn("tasks done", lines[2])
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
        """Extreme focus titles truncate with an ellipsis: the head
        line carries ``[<id>] [>]`` and the text line caps at 20
        chars (spec band 18-22), never blowing up the table width."""
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
            # Head line carries the ID + focus marker; text line caps
            # at 20 chars with an ellipsis.
            lines = out.splitlines()
            data = [i for i, l in enumerate(lines)
                    if l.startswith("| longtitle")]
            self.assertTrue(data)
            head_cell = lines[data[0]].split("|")[2]
            self.assertIn("[2] [>]", head_cell)
            self.assertNotIn("x", head_cell)
            text_cell = lines[data[0] + 1].split("|")[2]
            self.assertLessEqual(len(text_cell.strip()), 20)
            self.assertIn("...", text_cell)

    def test_long_project_name_truncated(self):
        out = self._render_with([
            "a-very-long-project-directory-name-that-keeps-going-on-and-on",
        ])
        # Project names cap at 40 chars.
        lines = out.splitlines()
        data = [i for i, l in enumerate(lines)
                if l.startswith("| a-very-long")]
        self.assertTrue(data)
        project_cell = lines[data[0]].split("|")[1]
        self.assertLessEqual(len(project_cell.strip()), 40)
        self.assertIn("...", project_cell)

    def test_short_data_fits_without_ellipsis(self):
        out = self._render_with(["p"])
        self.assertNotIn("...", out)
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
        self.assertNotIn("...", out)
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
        self.assertEqual(_truncate_ellipsis("abc", 1), ".")
        self.assertEqual(_truncate_ellipsis("abc", 0), "")
        self.assertEqual(_truncate_ellipsis("abc", 2), "..")
        self.assertEqual(_truncate_ellipsis("abcde", 3), "...")

    def test_output_is_pure_ascii(self):
        """The ellipsis marker itself must be ASCII dots."""
        text = "y" * 100
        for w in (4, 7, 15, 40):
            self.assertEqual(_truncate_ellipsis(text, w), 
                             _truncate_ellipsis(text, w).encode(
                                 "ascii", "strict").decode())


# ---------------------------------------------------------------------------
# 3. WORK CONTEXT: strict 4-element structure
# ---------------------------------------------------------------------------


class TestWorkContext(_IsolatedHome, unittest.TestCase):
    """``ck st`` renders the ordered WORK CONTEXT block:

    1. ``<< Done``      — the completed tasks (last 2 shown, count +
       overflow line when more)
    2. ``[!] Skipped``  — opens genuinely passed over between the last
       completion and an *explicit* focus, BY NAME, capped at 2
       (count header + ``... (+N more skipped)`` when more). Omitted
       entirely when there is no real execution gap; sequential
       pending tasks are never labelled Skipped.
    3. ``[>] Focus`` / ``[>] Next`` — explicit focus, or the
       auto-resolved first pending candidate (no mutation)
    4. ``>> Upcoming``  — the next pending task after Focus/Next
    5. ``Unfocused / Paused Context`` — still-open tasks that lost
       focus (with their bound notes)
    6. ``>> Backlog``   — the highlighted BOTTOM BANNER: a single
       ``N tasks remaining`` count of the TOTAL remaining
       uncompleted tasks (Total − Done — never reduced by the
       tasks named in the sections above)

    Every active section is a header line with its task items
    indented on new lines below it (``- [ID] Title [st]`` per line).
    """

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
            # Plain palette: byte-exact assertions must never depend
            # on whether the test runner's stdout is a TTY.
            return _render_local_status(ck, tl, palette=ckui.Palette(False))

    # ---- element 1: << Done (last 2 shown, count + overflow) ------ #

    def test_done_shows_last_two_completed_before_focus(self):
        plan = "# P\n- [x] old\n- [x] mid\n- [x] nearer\n- [>] cur\n"
        out = self._status(plan)
        # The LAST 2 completions before the focus — not just one.
        self.assertIn("<< Done (3 tasks):", out)
        self.assertIn("- [2] mid [x]", out)
        self.assertIn("- [3] nearer [x]", out)
        self.assertIn("... (+1 more done)", out)
        # Overflow: the earliest completion is folded into the count.
        self.assertNotIn("[1] old", out)

    def test_done_without_focus_is_last_two_shown(self):
        plan = "# P\n- [x] old\n- [x] newer\n- [ ] cur\n"
        out = self._status(plan)
        # Exactly 2 tasks: all shown, no count header, no overflow.
        self.assertIn("<< Done:", out)
        self.assertNotIn("<< Done (", out)
        self.assertIn("- [1] old [x]", out)
        self.assertIn("- [2] newer [x]", out)

    def test_done_single_completion(self):
        out = self._status("# P\n- [x] only\n- [>] cur\n")
        self.assertIn("<< Done:", out)
        self.assertIn("- [1] only [x]", out)
        # No count header and no overflow when under the limit.
        self.assertNotIn("tasks):", out.split("<< Done")[1].split("[>")[0])

    def test_done_none_hint(self):
        out = self._status("# P\n- [>] only focus\n")
        self.assertIn("<< Done: (none completed)", out)

    # ---- element 2: [!] Skipped (named gaps, capped at 2) --------- #

    def test_sequential_after_done_is_backlog_not_skipped(self):
        """The motivating case: opens following a done task are just
        the unstarted backlog — NEVER labelled Skipped."""
        plan = (
            "# P\n## Current Sprint\n"
            + "\n".join(f"- [ ] task {i}" for i in range(1, 3))
            + "\n- [x] done 3\n"
            + "\n".join(f"- [ ] late {i}" for i in range(4, 10))
            + "\n"
        )
        out = self._status(plan)
        self.assertNotIn("[!] Skipped", out)
        # Total − Done = 9 − 1: Focus/Next and Upcoming are NOT
        # subtracted from the backlog count.
        self.assertIn(">> Backlog: 8 tasks remaining", out)
        self.assertIn("- [1] task 1 [ ]", out)
        self.assertIn("- [2] task 2 [ ]", out)

    def test_backlog_is_total_minus_done(self):
        """Backlog = Total − Done: the tasks shown as Focus/Next and
        Upcoming are NOT subtracted from the remaining count."""
        plan = (
            "# P\n- [x] done\n"
            + "\n".join(f"- [ ] t{i}" for i in range(1, 7))
            + "\n"
        )
        out = self._status(plan)
        # Total=7, Done=1 → 6 remaining (current=t1, upcoming=t2 stay
        # counted); no Skipped block either.
        self.assertNotIn("[!] Skipped", out)
        self.assertIn(">> Backlog: 6 tasks remaining", out)
        self.assertIn("- [2] t1 [ ]", out)
        self.assertIn("- [3] t2 [ ]", out)

    def test_backlog_counts_skipped_tasks(self):
        """Skipped tasks are NOT subtracted from the backlog count —
        the banner is always Total − Done."""
        # Skipped (a, b passed over before focus c) + upcoming d.
        out = self._status("# P\n- [ ] a\n- [ ] b\n- [>] c\n- [ ] d\n")
        self.assertIn("[!] Skipped:", out)
        self.assertIn("- [1] a [ ]", out)
        self.assertIn("- [2] b [ ]", out)
        self.assertIn(">> Backlog: 4 tasks remaining", out)  # 4 − 0

    def test_backlog_singular_grammar(self):
        """Exactly one remaining task reads '1 task remaining'."""
        out = self._status("# P\n- [x] a\n- [ ] b\n")
        self.assertIn(">> Backlog: 1 task remaining", out)
        self.assertNotIn("1 tasks remaining", out)

    def test_backlog_all_done_zero(self):
        out = self._status("# P\n- [x] a\n- [x] b\n")
        self.assertIn(">> Backlog: 0 tasks remaining", out)

    def test_backlog_banner_renders_below_paused_context(self):
        """PLACEMENT: the Backlog banner is the VERY LAST line of the
        WORK CONTEXT block — after ``Unfocused / Paused Context`` —
        directly above the closing border."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(
                Path(td),
                "# P\n- [ ] a\n- [ ] b\n- [>] c\n- [ ] d\n",
            )
            ck.set_note("a note")
            ck.start(2)  # a -> paused (with note), b -> skipped
            ck.start(3)  # b -> paused (noteless), c -> focus
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
        self.assertIn("Unfocused / Paused Context:", out)
        idx_paused = out.index("Unfocused / Paused Context:")
        idx_banner = out.index(">> Backlog:")
        self.assertGreater(idx_banner, idx_paused)
        lines = out.splitlines()
        self.assertEqual(lines[-1], "=" * 61)
        self.assertIn(">> Backlog:", lines[-2])
        # Total − Done = 4 − 0: paused/skipped tasks are NOT subtracted.
        self.assertIn(">> Backlog: 4 tasks remaining", out)

    def test_skipped_under_limit_has_no_count_header(self):
        out = self._status("# P\n- [ ] a\n- [ ] b\n- [>] c\n")
        self.assertIn("[!] Skipped:", out)
        self.assertNotIn("Skipped (", out)
        self.assertIn("- [1] a [ ]", out)
        self.assertIn("- [2] b [ ]", out)

    def test_skipped_passover_before_focus(self):
        """Open tasks positioned before the focus were passed over."""
        out = self._status("# P\n- [ ] a\n- [ ] b\n- [>] c\n")
        self.assertIn("[!] Skipped:", out)
        self.assertIn("- [1] a [ ]", out)
        self.assertIn("- [2] b [ ]", out)

    def test_skipped_absent_when_opens_follow_the_focus(self):
        """Regression: opens AFTER the focus are Pending/Upcoming,
        never Skipped, even when a completion sits before them."""
        out = self._status(
            "# P\n- [x] one\n- [>] two\n- [ ] three\n- [ ] four\n")
        self.assertNotIn("[!] Skipped", out)
        # The first open after the focus is still the Upcoming slot.
        self.assertIn(">> Upcoming:", out)
        self.assertIn("- [3] three [ ]", out)
        # Total − Done = 4 − 1: focus + upcoming + tail all count.
        self.assertIn(">> Backlog: 3 tasks remaining", out)

    def test_skipped_only_before_focus(self):
        """An open before the focus is Skipped; opens after it stay
        Pending/Upcoming regardless of later completions."""
        out = self._status(
            "# P\n- [ ] a\n- [>] b\n- [x] c\n- [ ] d\n- [ ] e\n")
        skipped_block = out.split("[!] Skipped")[1].split("[>] Focus")[0]
        self.assertIn("- [1] a [ ]", skipped_block)
        # d/e follow the focus -> not skipped (d is the Upcoming slot).
        self.assertNotIn("[4] d", skipped_block)
        self.assertNotIn("[5] e", skipped_block)
        self.assertIn(">> Upcoming:", out)
        self.assertIn("- [4] d [ ]", out)

    def test_skipped_section_omitted_when_clean(self):
        out = self._status("# P\n- [ ] a\n- [x] b\n")
        self.assertNotIn("[!] Skipped", out)
        # Total − Done = 2 − 1 (singular wording).
        self.assertIn(">> Backlog: 1 task remaining", out)

    def test_sequential_opens_are_backlog_not_skipped(self):
        """With NO focus the first two opens become the Next candidate
        and Upcoming; the rest is backlog — not Skipped."""
        out = self._status("# P\n- [x] d\n- [ ] one\n- [ ] two\n- [ ] three\n")
        self.assertIn("[>] Next:", out)
        self.assertIn("- [2] one [ ]", out)
        self.assertIn(">> Upcoming:", out)
        self.assertIn("- [3] two [ ]", out)
        self.assertNotIn("[!] Skipped", out)
        # Total − Done = 4 − 1: Next + Upcoming + tail all count.
        self.assertIn(">> Backlog: 3 tasks remaining", out)

    # ---- element 3: [>] Focus / Next ------------------------------- #

    def test_note_renders_under_focus_task(self):
        """A note on the FOCUSED task is shown inline under ``[>]
        Focus:`` (plus duplicated at the top ``CURRENT FOCUS``)."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), "# P\n- [x] done\n- [>] alpha\n- [ ] beta\n")
            ck.set_note("checking API mapping")
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
        self.assertIn("[>] Focus:", out)
        self.assertIn("- [2] alpha", out)
        self.assertIn("* Note: checking API mapping", out)
        # The note sits BELOW the focus item line (slice from the
        # WORK CONTEXT header so the top CURRENT FOCUS copy of the
        # task title cannot skew the ordering check).
        work = out[out.index("-> WORK CONTEXT:"):]
        self.assertGreater(work.index("* Note: checking API mapping"),
                           work.index("- [2] alpha"))

    def test_note_renders_under_next_candidate(self):
        """A note on the auto-resolved ``[>] Next`` candidate is NOT
        hidden by the focus state — it renders under the task line."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), "# P\n- [ ] alpha\n- [ ] beta\n")
            ck.set_note("backing later")
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
        self.assertIn("[>] Next:", out)
        self.assertIn("- [1] alpha [ ]", out)
        self.assertIn("* Note: backing later", out)
        self.assertGreater(out.index("* Note: backing later"),
                           out.index("- [1] alpha [ ]"))

    def test_note_survives_focus_switch_and_renders_under_paused(self):
        """Focusing another task never swallows the note: it travels
        into ``Unfocused / Paused Context`` with its task."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(
                Path(td), "# P\n- [x] done\n- [ ] alpha\n- [ ] beta\n")
            ck.set_note("backing later")   # binds to the active task
            ck.start(3)                    # focus moves to beta
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
        self.assertIn("Unfocused / Paused Context:", out)
        self.assertIn("- [2] alpha", out)
        self.assertIn("* Note: backing later", out)
        # The Next block must NOT be silenced by the switch: the note
        # is still visible (under the paused block).
        self.assertIn("[>] Focus:", out)
        self.assertIn("- [3] beta", out)

    def test_note_binds_to_focused_task_over_next_candidate(self):
        """``ck start <NEW_ID>`` AFTER ``ck note`` re-binds the note to
        the newly focused task — the active slot is the single source
        of truth and the note stays visible under Focus."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(
                Path(td), "# P\n- [x] done\n- [ ] alpha\n- [ ] beta\n")
            ck.set_note("backing later")   # active task = alpha (2)
            ck.start(3)                    # re-binds the note to beta
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
        self.assertIn("* Note: backing later", out)
        self.assertIn("- [3] beta", out)

    def test_focus_renders_focus_label(self):
        out = self._status("# P\n- [>] focused task\n")
        self.assertIn("[>] Focus:", out)
        self.assertIn("- [1] focused task", out)
        # Focus item carries no [ ] suffix (it is already [>]).
        focus_line = [l for l in out.splitlines()
                      if "- [1] focused task" in l][0]
        self.assertFalse(focus_line.endswith("[ ]"))

    def test_no_focus_auto_resolves_first_pending_as_next(self):
        """focus=None resolves to the first available pending task as
        the Next candidate — no more 'no focus selected' dead end."""
        out = self._status("# P\n- [ ] alpha\n- [ ] beta\n")
        self.assertIn("[>] Next:", out)
        self.assertIn("- [1] alpha [ ]", out)
        self.assertNotIn("no focus selected", out)

    def test_next_candidate_is_display_only(self):
        """Rendering the auto-candidate must NEVER mutate PLAN.md
        (no [>] marker is written)."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), "# P\n- [ ] alpha\n- [ ] beta\n")
            before = ck.plan_file.read_bytes()
            out = ck.status()
            self.assertEqual(ck.plan_file.read_bytes(), before)
            self.assertIn("[>] Next:", out)
            self.assertIn("- [1] alpha [ ]", out)
            self.assertNotIn("- [>]", ck.plan_file.read_text(encoding="utf-8"))

    def test_no_open_tasks_next_hint(self):
        out = self._status("# P\n- [x] a\n- [x] b\n")
        self.assertIn("[>] Next: (no open tasks)", out)

    # ---- element 4: >> Upcoming ------------------------------------ #

    def test_upcoming_follows_focus(self):
        plan = "# P\n- [>] f\n- [x] d\n- [ ] future\n"
        out = self._status(plan)
        # Upcoming skips DONE tasks after the focus.
        self.assertIn(">> Upcoming:", out)
        self.assertIn("- [3] future [ ]", out)

    def test_upcoming_follows_auto_candidate(self):
        out = self._status("# P\n- [x] d\n- [ ] first open\n- [ ] second\n")
        # Next = first open (candidate); Upcoming follows it.
        self.assertIn("[>] Next:", out)
        self.assertIn("- [2] first open [ ]", out)
        self.assertIn(">> Upcoming:", out)
        self.assertIn("- [3] second [ ]", out)

    def test_upcoming_none_hint(self):
        out = self._status("# P\n- [>] all done after\n- [x] done\n")
        self.assertIn(">> Upcoming: (none)", out)

    # ---- structure -------------------------------------------------- #

    def test_strict_vertical_order(self):
        plan = (
            "# P\n## Current Sprint\n"
            "- [x] finished first\n"
            "- [x] finished second\n"
            "- [>] focused task\n"
            "- [ ] next up\n"
        )
        out = self._status(plan)
        self.assertIn("-> WORK CONTEXT:", out)
        idx_done = out.index("<< Done:")
        idx_focus = out.index("[>] Focus:")
        idx_focus_item = out.index("- [3] focused task")
        idx_upcoming = out.index(">> Upcoming:")
        idx_backlog = out.index(">> Backlog:")
        for a, b in ((idx_done, idx_focus), (idx_focus, idx_focus_item),
                     (idx_focus_item, idx_upcoming),
                     (idx_upcoming, idx_backlog)):
            self.assertLess(a, b)
        # No execution gap -> the Skipped section is absent.
        self.assertNotIn("[!] Skipped", out)
        # Done items sit on indented lines under the header.
        self.assertIn("- [1] finished first [x]", out)
        self.assertIn("- [2] finished second [x]", out)
        # BOTTOM PLACEMENT: the Backlog banner is the very last line
        # of WORK CONTEXT, directly above the closing border.
        lines = out.splitlines()
        self.assertEqual(lines[-1], "=" * 61)
        self.assertIn(">> Backlog:", lines[-2])

    def test_empty_plan_renders_section_hints(self):
        out = self._status("# P\n")
        self.assertIn("<< Done: (none completed)", out)
        self.assertIn("[>] Next: (no open tasks)", out)
        self.assertIn(">> Upcoming: (none)", out)
        self.assertIn(">> Backlog: 0 tasks remaining", out)
        # No Skipped section without an execution gap.
        self.assertNotIn("[!] Skipped", out)

    def test_exact_spec_layout(self):
        """Byte-exact rendering of the vertical-list spec structure
        (gap-free plan so the progress line stays compact)."""
        from cklib.config import VERSION
        plan = (
            "# Demo\n"
            "- [x] prev task\n"
            "- [>] focus task\n"
            "- [ ] next task\n"
            "- [x] later done\n"
        )
        out = self._status(plan)
        bar = "=" * 61
        expected = "\n".join([
            bar,
            f" > project [v{VERSION}]",
            " [%] Progress: 2/4 tasks done (50.0%)",
            "",
            " -> CURRENT FOCUS: [#2] focus task",
            "",
            " -> WORK CONTEXT:",
            "    << Done:",
            "       - [1] prev task [x]",
            "    [>] Focus:",
            "       - [2] focus task",
            "    >> Upcoming:",
            "       - [3] next task [ ]",
            "    >> Backlog: 2 tasks remaining",
            bar,
        ])
        self.assertEqual(out, expected)


# ---------------------------------------------------------------------------
# 4. No active focus: hint banner (read-only, never mutates)
# ---------------------------------------------------------------------------


class TestNoFocusHint(_IsolatedHome, unittest.TestCase):
    """``ck st`` guides the user to ``ck start`` when nothing is focused."""

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
            return _render_local_status(ck, tl, palette=ckui.Palette(False))

    def test_hint_banner_when_no_focus(self):
        out = self._status("# P\n- [ ] alpha\n- [ ] beta\n")
        self.assertIn("[!] No active focus set.", out)
        self.assertIn("'ck start <ID>'", out)
        self.assertIn("'ck start 1'", out)

    def test_hint_uses_first_pending_id(self):
        out = self._status("# P\n- [x] done\n- [ ] alpha\n- [ ] beta\n")
        self.assertIn("'ck start 2'", out)

    def test_no_hint_when_focus_set(self):
        out = self._status("# P\n- [>] focused\n- [ ] alpha\n")
        self.assertNotIn("No active focus set", out)

    def test_no_hint_when_no_pending_tasks(self):
        out = self._status("# P\n- [x] done\n")
        self.assertNotIn("No active focus set", out)

    def test_hint_never_mutates_plan(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), "# P\n- [ ] alpha\n- [ ] beta\n")
            before = ck.plan_file.read_bytes()
            out = ck.status()
            self.assertEqual(ck.plan_file.read_bytes(), before)
            self.assertIn("No active focus set", out)
            self.assertNotIn("- [>]", ck.plan_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
