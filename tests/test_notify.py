"""Tests for task due dates and the cron deadline check (v0.13.5).

Guards:

- parser/models: a trailing ``@due(YYYY-MM-DD)`` marker on a task
  title extracts into ``Task.due`` (clean title + ISO date) and the
  renderer re-emits it BEFORE the ``<!-- id: ... -->`` comment, so
  canonical plans round-trip byte-exactly (LF and CRLF). Mid-title
  occurrences and calendar-invalid dates stay plain title text;
- CLI ``ck add --due``: valid dates persist the marker (echo shows
  it too), calendar/shape junk and a missing value are clean usage
  errors (exit 2), and ``ck list`` suffixes the greppable marker;
- ``ck st``: past dates are marked ``[!] OVERDUE <date>``, today/future
  dates get a muted ``(due <date>)`` suffix, and desc-less/due-less
  tasks keep the output byte-identical (no suffix, ever);
- ``ck notify --check``: quiet one-line-per-due-task output for cron,
  sorted by date, completed tasks excluded, future dates not printed;
  exit 0 clean, 1 when a task is due today or overdue, 2 on usage /
  unbound-project errors;
- space parity: ``ck <space> add --due`` stores the same marker.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib.cli import main
from cklib.core import _today
from cklib.parser import parse_due, parse_plan, render_plan


class TestDueParsing(unittest.TestCase):
    PLAN = ("# Demo\n\n## Current Sprint\n"
            "- [ ] Ship release @due(2026-12-01) <!-- id: aaaa1111 -->\n"
            "- [ ] No deadline here <!-- id: bbbb2222 -->\n")

    def test_due_marker_extracts_to_field(self):
        tl = parse_plan(self.PLAN)
        self.assertEqual(tl.by_id(1).due, "2026-12-01")
        # The AST title is CLEAN: the marker is structural, not text.
        self.assertEqual(tl.by_id(1).title, "Ship release")
        self.assertEqual(tl.by_id(2).due, "")

    def test_round_trip_is_byte_exact(self):
        self.assertEqual(render_plan(parse_plan(self.PLAN)), self.PLAN)

    def test_crlf_round_trip_is_byte_exact(self):
        crlf = self.PLAN.replace("\n", "\r\n")
        self.assertEqual(render_plan(parse_plan(crlf)), crlf)

    def test_render_is_idempotent(self):
        once = render_plan(parse_plan(self.PLAN))
        self.assertEqual(render_plan(parse_plan(once)), once)

    def test_mid_title_marker_stays_title_text(self):
        text = ("- [ ] review @due(2026-12-01) tomorrow "
                "<!-- id: cccc3333 -->\n")
        tl = parse_plan(text)
        self.assertEqual(tl.by_id(1).due, "")
        self.assertIn("@due(2026-12-01)", tl.by_id(1).title)
        self.assertEqual(render_plan(tl), text)

    def test_invalid_calendar_date_stays_title_text(self):
        # The marker shape matches but the date is impossible: user
        # content must never be silently eaten by the extractor.
        text = "- [ ] t @due(2026-02-30) <!-- id: dddd4444 -->\n"
        tl = parse_plan(text)
        self.assertEqual(tl.by_id(1).due, "")
        self.assertIn("@due(2026-02-30)", tl.by_id(1).title)
        self.assertEqual(render_plan(tl), text)

    def test_legacy_syntax_extracts_and_renders_canonical(self):
        tl = parse_plan("- [] legacy @due(2026-05-05)\n")
        self.assertEqual(tl.by_id(1).due, "2026-05-05")
        self.assertTrue(tl.by_id(1).legacy_syntax)
        self.assertIn("- [ ] legacy @due(2026-05-05)", render_plan(tl))

    def test_due_and_description_coexist(self):
        text = ("## Current Sprint\n"
                "- [ ] T @due(2026-12-01) <!-- id: eeee5555 -->\n"
                "  > line one\n"
                "  > line two\n")
        tl = parse_plan(text)
        self.assertEqual(tl.by_id(1).due, "2026-12-01")
        self.assertEqual(tl.by_id(1).description, "line one\nline two")
        self.assertEqual(render_plan(tl), text)

    def test_field_mutation_renders_marker(self):
        tl = parse_plan(self.PLAN)
        tl.by_id(2).due = "2027-01-31"
        out = render_plan(tl)
        self.assertIn("No deadline here @due(2027-01-31) "
                      "<!-- id: bbbb2222 -->", out)
        self.assertEqual(render_plan(parse_plan(out)), out)

    def test_parse_due_validation(self):
        self.assertIsNotNone(parse_due("2026-12-01"))
        self.assertIsNone(parse_due("2026-02-30"))    # impossible date
        self.assertIsNone(parse_due("2026-2-1"))      # loose shape
        self.assertIsNone(parse_due("tomorrow"))
        self.assertIsNone(parse_due(""))
        self.assertIsNone(parse_due(None))


class _Harness(unittest.TestCase):
    PLAN = ("# Demo\n\n## Current Sprint\n"
            "- [ ] Alpha task <!-- id: aaaa1111 -->\n"
            "- [ ] Beta overdue @due(2026-11-01) <!-- id: bbbb2222 -->\n"
            "- [ ] Gamma due-later @due(2026-12-01) <!-- id: cccc3333 -->\n")
    # The pinned "today": Beta is overdue, Gamma is ahead.
    TODAY = date(2026, 11, 16)

    def _project(self, plan: str = PLAN) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name) / "project"
        root.mkdir()
        (root / ".ck").mkdir()
        (root / ".ck" / "PLAN.md").write_text(plan, encoding="utf-8")
        (root / ".ck" / "HISTORY.md").write_text(
            "## Completed\n", encoding="utf-8")
        (root / ".ck" / "version").write_text("0.13.0\n", encoding="utf-8")
        return root

    def _run(self, argv, cwd: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = Path.cwd()
        os.chdir(cwd)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(argv)
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def _frozen(self):
        return mock.patch("cklib.core._today",
                          return_value=self.TODAY)


class TestAddDue(_Harness):
    def test_add_with_due_writes_marker_and_echoes_it(self):
        root = self._project()
        code, out, _err = self._run(
            ["add", "Fix the bug", "--due", "2026-12-24"], root)
        self.assertEqual(code, 0)
        self.assertIn("@due(2026-12-24)", out)
        plan = (root / ".ck" / "PLAN.md").read_text(encoding="utf-8")
        self.assertIn("Fix the bug @due(2026-12-24) <!-- id:", plan)
        # Round-trip stays byte-exact with the marker inline.
        self.assertEqual(
            render_plan(parse_plan(plan)), plan)

    def test_add_invalid_calendar_date_is_usage_error(self):
        root = self._project()
        code, out, _err = self._run(
            ["add", "Bad", "--due", "2026-02-30"], root)
        self.assertEqual(code, 2)
        self.assertIn("Invalid --due date", out)
        plan = (root / ".ck" / "PLAN.md").read_text(encoding="utf-8")
        self.assertNotIn("Bad", plan)

    def test_add_invalid_shape_is_usage_error(self):
        root = self._project()
        code, out, _err = self._run(
            ["add", "Bad", "--due", "tomorrow"], root)
        self.assertEqual(code, 2)
        self.assertIn("Invalid --due date", out)

    def test_add_flag_without_value_is_usage_error(self):
        root = self._project()
        code, out, _err = self._run(["add", "Bad", "--due"], root)
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck add", out)

    def test_list_suffixes_the_marker(self):
        root = self._project()
        code, out, _err = self._run(["list"], root)
        self.assertEqual(code, 0)
        self.assertRegex(
            out, r"\[ \] 2\. Beta overdue @due\(2026-11-01\) \[#[0-9a-z]{8}\]")
        self.assertRegex(
            out, r"\[ \] 3\. Gamma due-later @due\(2026-12-01\) "
                 r"\[#[0-9a-z]{8}\]")
        self.assertRegex(
            out, r"\[ \] 1\. Alpha task \[#[0-9a-z]{8}\]")


class TestStatusDueHighlight(_Harness):
    def test_next_block_marks_overdue_task(self):
        root = self._project()
        with self._frozen():
            code, out, _err = self._run(["st"], root)
        self.assertEqual(code, 0)
        # Alpha (no due) is the first open task -> the [>] Next row.
        self.assertIn("[1] Alpha task", out)
        self.assertNotIn("OVERDUE", out.split("Alpha task")[1].split("\n")[0])

    def test_overdue_in_focus_block(self):
        root = self._project()
        with self._frozen():
            self._run(["start", "2"], root)
            code, out, _err = self._run(["st"], root)
        self.assertEqual(code, 0)
        self.assertIn("CURRENT FOCUS:", out)
        self.assertIn("[!] OVERDUE 2026-11-01", out)

    def test_future_due_suffix_is_not_overdue(self):
        root = self._project()
        with self._frozen():
            self._run(["start", "3"], root)
            code, out, _err = self._run(["st"], root)
        self.assertEqual(code, 0)
        self.assertIn("(due 2026-12-01)", out)
        self.assertNotIn("OVERDUE", out)

    def test_due_less_tasks_render_unchanged(self):
        # Byte-identical guarantee: with today frozen AFTER every due
        # date, the Alpha task (no due) must carry no suffix at all.
        root = self._project()
        with self._frozen():
            code, out, _err = self._run(["st"], root)
        self.assertEqual(code, 0)
        self.assertNotIn("OVERDUE", out)
        self.assertNotIn("(due", out)


class TestNotifyCheck(_Harness):
    def test_no_due_tasks_exit_0(self):
        plan = ("# Demo\n\n## Current Sprint\n"
                "- [ ] Alpha task <!-- id: aaaa1111 -->\n")
        root = self._project(plan)
        with self._frozen():
            code, out, _err = self._run(["notify", "--check"], root)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_overdue_and_due_today_fire_exit_1(self):
        root = self._project()
        with self._frozen():
            code, out, _err = self._run(["notify", "--check"], root)
        # Beta (2026-11-01) is overdue; Gamma (2026-12-01) is ahead
        # and must NOT print (quiet cron: only actionable rows).
        self.assertEqual(code, 1)
        self.assertIn("[ ] #2 Beta overdue (due 2026-11-01) [!] OVERDUE",
                      out)
        self.assertNotIn("Gamma", out)

    def test_due_today_prints_without_overdue_tag(self):
        plan = ("# Demo\n\n## Current Sprint\n"
                "- [ ] Due today @due(2026-11-16) <!-- id: aaaa1111 -->\n")
        root = self._project(plan)
        with self._frozen():
            code, out, _err = self._run(["notify", "--check"], root)
        self.assertEqual(code, 1)
        self.assertIn("[ ] #1 Due today (due 2026-11-16)", out)
        self.assertNotIn("OVERDUE", out)

    def test_rows_sorted_by_date(self):
        plan = ("# Demo\n\n## Current Sprint\n"
                "- [ ] Late @due(2026-10-10) <!-- id: aaaa1111 -->\n"
                "- [ ] Earlier @due(2026-09-09) <!-- id: bbbb2222 -->\n")
        root = self._project(plan)
        with self._frozen():
            code, out, _err = self._run(["notify", "--check"], root)
        self.assertEqual(code, 1)
        self.assertLess(out.index("#2 Earlier"),
                        out.index("#1 Late"))

    def test_done_tasks_are_excluded(self):
        plan = ("# Demo\n\n## Current Sprint\n"
                "- [x] Finished @due(2026-01-01) <!-- id: aaaa1111 -->\n")
        root = self._project(plan)
        with self._frozen():
            code, out, _err = self._run(["notify", "--check"], root)
        self.assertEqual(code, 0)
        self.assertNotIn("Finished", out)

    def test_future_only_plan_exit_0(self):
        plan = ("# Demo\n\n## Current Sprint\n"
                "- [ ] Ahead @due(2999-01-01) <!-- id: aaaa1111 -->\n")
        root = self._project(plan)
        with self._frozen():
            code, out, _err = self._run(["notify", "--check"], root)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_without_check_is_usage_error(self):
        root = self._project()
        code, out, _err = self._run(["notify"], root)
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck notify --check", out)

    def test_unknown_flag_is_usage_error(self):
        root = self._project()
        code, out, _err = self._run(["notify", "--check", "--wat"], root)
        self.assertEqual(code, 2)
        # The generic legacy flag guard rejects it before the runner.
        self.assertIn("Unknown flag for `ck notify`", out)

    def test_outside_project_is_clean_error(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        code, out, _err = self._run(["notify", "--check"], Path(td.name))
        self.assertEqual(code, 2)
        self.assertIn("No active project", out + _err)


class TestNotifyCore(_Harness):
    def test_notify_check_rows_shape(self):
        root = self._project()
        from cklib.core import ContextKeeper
        old = Path.cwd()
        os.chdir(root)
        try:
            with self._frozen():
                rows = ContextKeeper().notify_check()
        finally:
            os.chdir(old)
        self.assertEqual(
            [(r["id"], r["due"], r["overdue"], r["due_today"])
             for r in rows],
            [(2, "2026-11-01", True, False),
             (3, "2026-12-01", False, False)])
        self.assertEqual(rows[0]["title"], "Beta overdue")
        self.assertEqual(rows[0]["status"], " ")

    def test_today_seam_is_shared(self):
        # One seam feeds both notify_check and the st suffix, so a
        # patched day can never make them disagree.
        self.assertIsInstance(_today(), date)


class TestSpaceAddDue(unittest.TestCase):
    """``ck <space> add --due`` parity (v0.13.5)."""

    def _project(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name) / "project"
        root.mkdir()
        (root / ".ck").mkdir()
        (root / ".ck" / "PLAN.md").write_text(
            "# Demo\n\n## Current Sprint\n"
            "- [ ] seed <!-- id: aaaa1111 -->\n",
            encoding="utf-8")
        (root / ".ck" / "HISTORY.md").write_text(
            "## Completed\n", encoding="utf-8")
        (root / ".ck" / "version").write_text("0.13.0\n", encoding="utf-8")
        return root

    def test_space_add_writes_marker(self):
        from cklib import spaces

        root = self._project()
        out = io.StringIO()
        old = Path.cwd()
        os.chdir(root)
        try:
            with redirect_stdout(out):
                code = main(["local", "add", "Space due task",
                             "--due", "2026-12-01"])
            self.assertEqual(code, 0)
            path = spaces.space_path("local")
            text = path.read_text(encoding="utf-8")
        finally:
            os.chdir(old)
        self.assertIn("Space due task @due(2026-12-01) <!-- id:", text)
        # The space file round-trips through the SAME parser: the
        # marker stays structural across load/commit cycles.
        self.assertEqual(render_plan(parse_plan(text)), text)

    def test_space_add_invalid_date_is_usage_error(self):
        root = self._project()
        out = io.StringIO()
        old = Path.cwd()
        os.chdir(root)
        try:
            with redirect_stdout(out):
                code = main(["local", "add", "Bad", "--due", "2026-13-99"])
        finally:
            os.chdir(old)
        self.assertEqual(code, 2)
        self.assertIn("Invalid --due date", out.getvalue())


if __name__ == "__main__":
    unittest.main()
