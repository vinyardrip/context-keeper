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


# Horizontal rules of the dashboard grid (thin box-drawing).
_RULE_HEADS = ("\u250c", "\u251c", "\u2514")


class TestDashboardWidth(_IsolatedHome, unittest.TestCase):
    """Dashboard table columns are content-capped before layout.

    The compact table fits an 80-column terminal: Project (25),
    Progress (9, two-line ``done/total`` + ``(pct%)``), Last Active
    (dynamic, capped at 16) stay tight, and **Focus Task is the
    fluid column** that absorbs whatever's left and right-truncates
    long task titles with a trailing ``...``.
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
        header = [l for l in out.splitlines() if l.startswith("│ Project")]
        self.assertEqual(len(header), 1)
        cells = [c.strip() for c in header[0].strip("│").split("│")]
        self.assertEqual(
            cells, ["Project", "Focus Task", "Progress", "Last Active"]
        )

    def test_progress_cell_is_two_line_and_bounded(self):
        """Progress renders as ``<done>/<total>`` on line 1 and
        ``(<pct>%)`` on line 2, inside a <= 9-char column."""
        from cklib.core import _safe_parse_plan, _PROGRESS_WIDTH

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "progproj"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.init()
            for n in range(3):
                ck.add_task(f"task {n}")
            ck.done("2")
            ck.register(path=root, name="progproj")
            out = _render_dashboard(
                ck,
                list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse_plan,
            )
        lines = out.splitlines()
        idx = next(i for i, l in enumerate(lines)
                   if l.startswith("│ progproj"))
        ratio = lines[idx].split("│")[3].strip()
        pct = lines[idx + 1].split("│")[3].strip()
        self.assertRegex(ratio, r"^\d+/\d+$")
        self.assertRegex(pct, r"^\(\d+(\.\d+)?%\)$")
        self.assertLessEqual(len(ratio), _PROGRESS_WIDTH)
        self.assertLessEqual(len(pct), _PROGRESS_WIDTH)

    def test_focus_column_truncated_at_cap(self):
        """Extreme focus titles truncate from the RIGHT with a
        trailing ellipsis: the head line carries ``[<id>] [>]`` and
        the text line is clamped to the fluid column width, never
        blowing up the table."""
        from cklib.core import _safe_parse_plan, _FOCUS_TEXT_CAP

        with tempfile.TemporaryDirectory() as td:
            # Build a registry entry whose focused-task title is
            # ~120 chars via a real project on disk.
            root = Path(td) / "longtitle"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.init()
            ck.add_task("x" * 120)  # replaces the init seed -> id 1
            ck.start(1)
            ck.register(path=root, name="longtitle")
            out = _render_dashboard(
                ck,
                list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse_plan,
            )
            # Head line carries the ID + focus marker; text line caps
            # at the focus cap with a TRAILING (right-side) ellipsis.
            lines = out.splitlines()
            data = [i for i, l in enumerate(lines)
                    if l.startswith("│ longtitle")]
            self.assertTrue(data)
            head_cell = lines[data[0]].split("│")[2]
            self.assertIn("[1] [>]", head_cell)
            self.assertNotIn("x", head_cell)
            text_cell = lines[data[0] + 1].split("│")[2]
            self.assertLessEqual(len(text_cell.strip()), _FOCUS_TEXT_CAP)
            self.assertTrue(text_cell.strip().endswith("..."),
                            f"ellipsis must trail, got {text_cell!r}")

    def test_long_project_name_truncated(self):
        out = self._render_with([
            "a-very-long-project-directory-name-that-keeps-going-on-and-on",
        ])
        # Project names cap at 40 chars.
        lines = out.splitlines()
        data = [i for i, l in enumerate(lines)
                if l.startswith("│ a-very-long")]
        self.assertTrue(data)
        project_cell = lines[data[0]].split("│")[1]
        self.assertLessEqual(len(project_cell.strip()), 25)
        self.assertIn("...", project_cell)

    def test_path_line_always_two_segment_form(self):
        """The path line renders the FIXED ~/../parent/project form
        (two-dot marker, FLUSH LEFT) for EVERY path — short and long
        — and never exceeds the compact 25-char Project cell."""
        out = self._render_with([
            "p",                        # short path, still contracts
            "a-very-long-project-directory-name-that-keeps-going",
        ])
        # Scope to the PROJECTS table: the unified spaces table above
        # it renders its OWN contracted path cells too.
        projects = out.split("GLOBAL DASHBOARD", 1)[-1]
        def _is_path_line(l: str) -> bool:
            cell = l.split("│")[1] if l.startswith("│") else ""
            return cell.strip().startswith(("/../", "~/../"))

        path_lines = [l for l in projects.splitlines() if _is_path_line(l)]
        self.assertEqual(len(path_lines), 2,
                         f"expected two contracted path lines:\n{out}")
        for line in path_lines:
            # Flush left: a single cell-padding space, NO indent.
            cell = line.split("│")[1]
            self.assertTrue(cell.startswith(" "),
                            f"path line lost its cell padding: {cell!r}")
            self.assertFalse(cell.startswith("  "),
                             f"path line must be flush left: {cell!r}")
            self.assertLessEqual(len(cell.rstrip()), 25 + 1)
            self.assertTrue(
                cell.strip().startswith(("/../", "~/../")),
                f"path line not in ~/../parent/project form: {cell!r}",
            )
            # Exactly two segments after the two-dot marker.
            body = cell.strip().split("/../", 1)[1]
            self.assertEqual(len(body.split("/")), 2, body)

    def test_path_line_keeps_parent_and_project_segments(self):
        """Deeply nested paths keep the LAST TWO segments: the
        immediate parent dir plus the project dir."""
        out = self._render_with(["myproject"])
        # Scope to the PROJECTS table (the spaces table above it also
        # emits contracted path cells).
        projects = out.split("GLOBAL DASHBOARD", 1)[-1]
        line = next(l for l in projects.splitlines()
                    if l.startswith("│")
                    and l.split("│")[1].strip().startswith(("/../", "~/../")))
        cell = line.split("│")[1].strip()
        # The registry fixture nests under a tmp dir; the last two
        # segments must be <tmp-parent>/myproject.
        self.assertTrue(cell.endswith("/myproject"), cell)
        self.assertIn("/", cell.split("/../", 1)[1])

    def test_short_data_fits_without_ellipsis(self):
        out = self._render_with(["p"])
        # The fixed two-segment path contraction itself contributes
        # the ``..`` marker ("/../<parent>/<project>") — no OTHER
        # cell may carry an ellipsis.
        table_lines = [l for l in out.splitlines()
                       if l.startswith("│")]
        for line in table_lines:
            for cell in line.strip("│").split("│"):
                cell = cell.strip()
                if cell.startswith("/../") or cell.startswith("~/../"):
                    continue  # the path line's structural marker
                self.assertNotIn("...", cell)
        self.assertIn("│ p ", out)

    def test_table_lines_bounded_for_80_col_terminal(self):
        """Every table line fits inside an 80-column terminal — even
        with long project names and a wide focus column."""
        out = self._render_with([
            "context-keeper",
            "my-second-project-with-a-very-long-name",
        ])
        table_lines = [
            l for l in out.splitlines()
            if l.startswith("│") or l.startswith("+")
        ]
        self.assertTrue(table_lines)
        for line in table_lines:
            self.assertLessEqual(
                len(line), 80,
                f"table line exceeds 80 cols: {len(line)}: {line!r}",
            )

    def test_single_table_line_stays_bounded(self):
        out = self._render_with(["p"])
        for line in out.splitlines():
            if line.startswith("│") or line.startswith("+"):
                self.assertLessEqual(len(line), 80)


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
    """``ck st`` renders the ordered WORK CONTEXT block (v0.13.0):

    1. ``[>] Focus`` / ``[>] Next`` — explicit focus (``[#N]``), or the
       auto-resolved first pending candidate (``[N] ... [ ]``, display
       only — the plan is never mutated)
    2. ``[~] Paused`` — tasks parked by ``ck focus`` (``[~]``), plus
       noted open tasks; each item carries its inline notes
    3. ``[x] History (last N):`` — the LAST N entries from
       ``.ck/HISTORY.md`` (``+M more in .ck/HISTORY.md`` overflow;
       ``(none completed)`` when empty). In-file ``[x]`` rows feed the
       progress ratio but are NOT displayed here.
    4. ``>> Backlog`` — the highlighted BOTTOM BANNER: a single
       ``N task(s) remaining`` count of ALL active tasks (Total − Done
       — never reduced by the tasks named in the sections above)

    The v0.12 ``<< Done`` / ``[!] Skipped`` / ``>> Upcoming`` sections
    are GONE: completions moved to HISTORY.md, pass-over gaps ride the
    progress line as ``(gaps: ...)``, and everything after the first
    open task is simply backlog.

    Every active section is a header line with its task items
    indented on new lines below it.
    """

    def _ck(self, tmp: Path, plan: str) -> ContextKeeper:
        root = tmp / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(plan, encoding="utf-8")
        # v0.13.0 write-gate: raw fixtures carry the current marker.
        ck.ck_path.joinpath("version").write_text(
            ckconfig.VERSION + "\n", encoding="utf-8")
        return ck

    def _status(self, plan: str, history: str | None = None) -> str:
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), plan)
            if history is not None:
                ck.history_file.write_text(history, encoding="utf-8")
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            # Plain palette: byte-exact assertions must never depend
            # on whether the test runner's stdout is a TTY.
            return _render_local_status(ck, tl, palette=ckui.Palette(False))

    # ---- element: [x] History (HISTORY.md ledger, last N shown) ---- #

    _HIST4 = (
        "## Completed\n"
        "- [x] 2026-11-01 09:00: old\n"
        "- [x] 2026-11-02 10:00: mid\n"
        "- [x] 2026-11-03 11:00: nearer\n"
        "- [x] 2026-11-04 12:00: freshest\n"
    )

    def test_history_shows_last_three_with_overflow(self):
        out = self._status("# P\n- [>] cur\n", history=self._HIST4)
        # The LAST 3 ledger entries — not just one — plus the
        # overflow line folding the earliest into a count.
        self.assertIn("    [x] History (last 3):", out)
        self.assertIn("- [x] 2026-11-02 10:00: mid", out)
        self.assertIn("- [x] 2026-11-03 11:00: nearer", out)
        self.assertIn("- [x] 2026-11-04 12:00: freshest", out)
        self.assertIn("... (+1 more in .ck/HISTORY.md)", out)
        # The earliest entry is folded into the overflow count.
        self.assertNotIn("2026-11-01 09:00: old", out)

    def test_history_shows_all_when_under_limit(self):
        hist = (
            "## Completed\n"
            "- [x] 2026-11-01 09:00: old\n"
            "- [x] 2026-11-02 10:00: newer\n"
        )
        out = self._status("# P\n- [ ] cur\n", history=hist)
        # Exactly 2 entries: all shown, no overflow.
        self.assertIn("    [x] History (last 3):", out)
        self.assertIn("- [x] 2026-11-01 09:00: old", out)
        self.assertIn("- [x] 2026-11-02 10:00: newer", out)
        self.assertNotIn("more in", out)

    def test_history_single_entry_no_overflow(self):
        hist = "## Completed\n- [x] 2026-11-01 09:00: only\n"
        out = self._status("# P\n- [>] cur\n", history=hist)
        self.assertIn("- [x] 2026-11-01 09:00: only", out)
        self.assertNotIn("more in", out)

    def test_history_none_hint(self):
        out = self._status("# P\n- [>] only focus\n")
        self.assertIn("    [x] History (last 3):", out)
        self.assertIn("       (none completed)", out)

    def test_in_file_done_rows_feed_progress_not_history(self):
        """A hand-written ``[x]`` row counts toward the progress ratio
        but is never displayed as a History ledger entry."""
        out = self._status("# P\n- [x] hand done\n- [>] cur\n")
        self.assertIn(" [%] Progress: 1/2 tasks done (50.0%)", out)
        self.assertIn("       (none completed)", out)
        self.assertNotIn("hand done", out.split("History")[1])

    # ---- backlog: Total − Done, sections never subtract ------------- #

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
        # Total − Done = 9 − 1: every open task counts.
        self.assertIn(">> Backlog: 8 tasks remaining", out)
        self.assertIn("- [1] task 1 [ ]", out)

    def test_backlog_is_total_minus_done(self):
        """Backlog = Total − Done: the Next candidate and the tail are
        NOT subtracted from the remaining count."""
        plan = (
            "# P\n- [x] done\n"
            + "\n".join(f"- [ ] t{i}" for i in range(1, 7))
            + "\n"
        )
        out = self._status(plan)
        self.assertNotIn("[!] Skipped", out)
        self.assertIn(">> Backlog: 6 tasks remaining", out)
        self.assertIn("- [2] t1 [ ]", out)

    def test_passed_over_opens_are_plain_backlog(self):
        """Open tasks positioned before the focus are NOT named in
        any section (the old Skipped block is gone) — they only count
        toward the backlog banner."""
        out = self._status("# P\n- [ ] a\n- [ ] b\n- [>] c\n- [ ] d\n")
        self.assertNotIn("[!] Skipped", out)
        self.assertIn(">> Backlog: 4 tasks remaining", out)  # 4 − 0
        # With an explicit focus, only the focus is named; the
        # passed-over opens and the tail stay unnamed.
        self.assertIn("- [#3] c", out)
        self.assertNotIn("- [1] a", out)
        self.assertNotIn("- [2] b", out)
        self.assertNotIn("- [4] d", out)

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
        WORK CONTEXT block — after ``[~] Paused`` — directly above
        the closing border."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(
                Path(td),
                "# P\n- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n",
            )
            ck.set_note("a note")
            ck.focus(2)  # a -> [~] (with note)
            ck.focus(3)  # b -> [~] (noteless), c -> focus
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
        self.assertIn("    [~] Paused:", out)
        idx_paused = out.index("    [~] Paused:")
        idx_banner = out.index(">> Backlog:")
        self.assertGreater(idx_banner, idx_paused)
        lines = out.splitlines()
        self.assertEqual(lines[-1], "=" * 61)
        self.assertIn(">> Backlog:", lines[-2])
        # Total − Done = 4 − 0: parked tasks are NOT subtracted.
        self.assertIn(">> Backlog: 4 tasks remaining", out)

    def test_sequential_opens_are_backlog_not_skipped(self):
        """With NO focus the first open becomes the Next candidate;
        the rest is backlog — not Skipped, not Upcoming."""
        out = self._status("# P\n- [x] d\n- [ ] one\n- [ ] two\n- [ ] three\n")
        self.assertIn("[>] Next:", out)
        self.assertIn("- [2] one [ ]", out)
        self.assertNotIn("[!] Skipped", out)
        self.assertNotIn(">> Upcoming:", out)
        # Total − Done = 4 − 1: the tail all counts.
        self.assertIn(">> Backlog: 3 tasks remaining", out)

    # ---- element: [>] Focus / Next ---------------------------------- #

    def test_note_renders_under_current_focus(self):
        """A note on the FOCUSED task renders once, directly under the
        top ``CURRENT FOCUS`` line."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), "# P\n- [x] done\n- [>] alpha\n- [ ] beta\n")
            ck.set_note("checking API mapping")
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
        self.assertIn("-> CURRENT FOCUS: [#2] alpha", out)
        self.assertEqual(out.count("* Note: checking API mapping"), 1)
        focus_idx = out.index("-> CURRENT FOCUS:")
        note_idx = out.index("* Note: checking API mapping")
        work_idx = out.index("-> WORK CONTEXT:")
        self.assertLess(focus_idx, note_idx)
        self.assertLess(note_idx, work_idx)

    def test_note_renders_under_paused_block_for_next_candidate(self):
        """A note on the auto-resolved ``[>] Next`` candidate is NOT
        hidden by the focus state — the noted open task surfaces in
        the ``[~] Paused`` block with its scratchpad."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td), "# P\n- [ ] alpha\n- [ ] beta\n")
            ck.set_note("backing later")
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
        self.assertIn("[>] Next:", out)
        self.assertIn("- [1] alpha [ ]", out)
        self.assertIn("    [~] Paused:", out)
        self.assertIn("- [1] alpha", out)
        self.assertIn("* Note: backing later", out)
        paused_idx = out.index("    [~] Paused:")
        note_idx = out.index("* Note: backing later")
        self.assertLess(paused_idx, note_idx)

    def test_note_survives_focus_switch_and_renders_under_paused(self):
        """Focusing another task never swallows the note: it travels
        into the ``[~] Paused`` block with its task."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(
                Path(td), "# P\n- [x] done\n- [ ] alpha\n- [ ] beta\n")
            ck.set_note("backing later")   # binds to the active task
            ck.start(3)                    # focus moves to beta
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
        self.assertIn("    [~] Paused:", out)
        self.assertIn("- [2] alpha", out)
        self.assertIn("* Note: backing later", out)
        self.assertIn("[>] Focus:", out)
        self.assertIn("- [#3] beta", out)

    def test_note_stays_bound_to_its_task_across_refocus(self):
        """``ck note`` binds the note to a TASK, not to the active
        slot: refocusing another task never re-binds or hides it."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(
                Path(td), "# P\n- [x] done\n- [ ] alpha\n- [ ] beta\n")
            ck.set_note("backing later")   # binds to alpha
            ck.start(3)                    # focus beta; note stays on alpha
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            out = _render_local_status(ck, tl, palette=ckui.Palette(False))
            # The note is still alpha's (visible in the Paused block),
            # and the focused beta carries no note.
            self.assertIn("* Note: backing later", out)
            self.assertIn("- [2] alpha", out)
            self.assertEqual(ck.get_note_entries(3), [])
            self.assertEqual(
                ck.get_note_entries(2)[-1]["text"], "backing later")

    def test_focus_renders_focus_label(self):
        out = self._status("# P\n- [>] focused task\n")
        self.assertIn("    [>] Focus:", out)
        self.assertIn("- [#1] focused task", out)
        # Focus item carries no [ ] suffix (it is already [>]).
        focus_line = [l for l in out.splitlines()
                      if "- [#1] focused task" in l][0]
        self.assertFalse(focus_line.endswith("[ ]"))

    def test_no_focus_auto_resolves_first_pending_as_next(self):
        """focus=None resolves to the first available pending task as
        the Next candidate — no more 'no focus selected' dead end."""
        out = self._status("# P\n- [ ] alpha\n- [ ] beta\n")
        self.assertIn("    [>] Next:", out)
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
        self.assertIn("    [>] Next: (no open tasks)", out)

    # ---- removed sections: Upcoming / Skipped are gone -------------- #

    def test_upcoming_section_removed(self):
        """v0.13.0: the ``>> Upcoming`` section is gone — everything
        after the first open task is the backlog banner."""
        out = self._status("# P\n- [>] f\n- [x] d\n- [ ] future\n")
        self.assertNotIn(">> Upcoming:", out)
        # The future task only counts toward the backlog.
        self.assertIn(">> Backlog: 2 tasks remaining", out)
        self.assertNotIn("- [3] future", out)

    def test_upcoming_none_hint_replaced_by_next_hint(self):
        out = self._status("# P\n- [x] done\n")
        self.assertNotIn(">> Upcoming:", out)
        self.assertIn("    [>] Next: (no open tasks)", out)

    # ---- structure --------------------------------------------------- #

    def test_strict_vertical_order(self):
        plan = "# P\n## Current Sprint\n- [>] focused task\n- [ ] next up\n"
        hist = (
            "## Completed\n"
            "- [x] 2026-11-01 09:00: finished first\n"
            "- [x] 2026-11-02 10:00: finished second\n"
        )
        out = self._status(plan, history=hist)
        self.assertIn("-> WORK CONTEXT:", out)
        idx_focus = out.index("    [>] Focus:")
        idx_focus_item = out.index("- [#1] focused task")
        idx_paused = out.index("    [~] Paused:")
        idx_history = out.index("    [x] History (last 3):")
        idx_backlog = out.index(">> Backlog:")
        for a, b in ((idx_focus, idx_focus_item),
                     (idx_focus_item, idx_paused),
                     (idx_paused, idx_history),
                     (idx_history, idx_backlog)):
            self.assertLess(a, b)
        # v0.12 sections are gone.
        self.assertNotIn("[!] Skipped", out)
        self.assertNotIn("<< Done:", out)
        self.assertNotIn(">> Upcoming:", out)
        # Ledger entries render under the History header.
        self.assertIn("- [x] 2026-11-01 09:00: finished first", out)
        self.assertIn("- [x] 2026-11-02 10:00: finished second", out)
        # BOTTOM PLACEMENT: the Backlog banner is the very last line
        # of WORK CONTEXT, directly above the closing border.
        lines = out.splitlines()
        self.assertEqual(lines[-1], "=" * 61)
        self.assertIn(">> Backlog:", lines[-2])

    def test_empty_plan_renders_section_hints(self):
        out = self._status("# P\n")
        self.assertIn("    [>] Next: (no open tasks)", out)
        self.assertIn("    [~] Paused: (none)", out)
        self.assertIn("    [x] History (last 3):", out)
        self.assertIn("       (none completed)", out)
        self.assertIn(">> Backlog: 0 tasks remaining", out)
        # v0.12 sections are gone.
        self.assertNotIn("[!] Skipped", out)
        self.assertNotIn(">> Upcoming:", out)

    def test_exact_spec_layout(self):
        """Byte-exact rendering of the vertical-list spec structure."""
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
            "    [>] Focus:",
            "       - [#2] focus task",
            "    [~] Paused: (none)",
            "    [x] History (last 3):",
            "       (none completed)",
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
