"""Tests for the task reordering commands: ``move`` / ``swap`` / ``reorder``.

Reordering reshuffles PENDING work — the ``[ ]`` and ``[>]`` tasks —
without ever touching what the user wrote. Three guarantees are pinned
here:

- **Position is the only thing that changes.** A reorder must not
  alter a title, a status marker, or the focus;
- **Completed tasks are immutable history.** Targeting a ``[x]`` task
  aborts the whole operation with exit code 1 and leaves the file
  byte-identical — no half-applied moves;
- **The structural document survives.** Headers, prose, the
  ``## Completed`` block and line order outside the active list are
  all preserved, so a reorder can never corrupt PLAN.md.

AST-level reordering is covered directly against ``TaskList``; the
CLI contract (messages and exit codes) is covered through ``main()``.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import ui as ckui
from cklib.cli import main
from cklib.core import ContextKeeper
from cklib.models import CompletedTaskError, TaskList, TaskStatus
from cklib.parser import parse_plan


def _plan(*rows: str) -> str:
    """A minimal PLAN.md document from ``id|marker|title`` rows."""
    body = ["# p", "", "## Current Sprint"]
    body += [f"- [{m}] {t}" for _i, m, t in rows]
    body += ["", "## Completed", ""]
    return "\n".join(body) + "\n"


def _order(tl: TaskList) -> list[str]:
    """Task titles in DOCUMENT order (the rendered order)."""
    return [t.title for t in sorted(tl.tasks, key=lambda t: t.line_number)]


class TestReorderAST(unittest.TestCase):
    """Unit coverage of the TaskList reordering mutations."""

    def setUp(self):
        self.tl = parse_plan(_plan(
            (1, " ", "one"), (2, ">", "two"), (3, " ", "three")))

    # -- move ------------------------------------------------------------ #

    def test_move_to_front(self):
        self.tl.move_task(3, 1)
        self.assertEqual(_order(self.tl), ["three", "one", "two"])

    def test_move_to_middle_shifts_intermediates(self):
        self.tl.move_task(1, 2)
        self.assertEqual(_order(self.tl), ["two", "one", "three"])

    def test_move_to_last(self):
        self.tl.move_task(1, 3)
        self.assertEqual(_order(self.tl), ["two", "three", "one"])

    def test_move_top_and_bottom_aliases(self):
        self.tl.move_task_to_edge(3, top=True)
        self.assertEqual(_order(self.tl)[0], "three")
        self.tl.move_task_to_edge(3, top=False)
        self.assertEqual(_order(self.tl)[-1], "three")

    def test_move_to_current_position_is_a_noop(self):
        self.assertEqual(self.tl.move_task(2, 2), 2)
        self.assertEqual(_order(self.tl), ["one", "two", "three"])

    def test_move_out_of_range_raises(self):
        for pos in (0, -1, 4):
            with self.assertRaises(ValueError):
                self.tl.move_task(1, pos)

    def test_move_unknown_id_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.tl.move_task(99, 1)

    def test_move_completed_task_is_refused(self):
        tl = parse_plan(_plan((1, " ", "open"), (2, "x", "done")))
        with self.assertRaises(CompletedTaskError) as ctx:
            tl.move_task(2, 1)
        self.assertEqual(ctx.exception.task_id, 2)
        self.assertEqual(_order(tl), ["open", "done"])

    # -- swap ------------------------------------------------------------ #

    def test_swap_exchanges_positions(self):
        self.tl.swap_tasks(1, 3)
        self.assertEqual(_order(self.tl), ["three", "two", "one"])

    def test_swap_returns_original_positions(self):
        self.assertEqual(self.tl.swap_tasks(1, 3), (1, 3))

    def test_swap_with_self_is_a_noop(self):
        self.tl.swap_tasks(2, 2)
        self.assertEqual(_order(self.tl), ["one", "two", "three"])

    def test_swap_completed_task_is_refused(self):
        tl = parse_plan(_plan((1, " ", "open"), (2, "x", "done")))
        with self.assertRaises(CompletedTaskError):
            tl.swap_tasks(1, 2)
        self.assertEqual(_order(tl), ["open", "done"])

    # -- reorder --------------------------------------------------------- #

    def test_reorder_applies_given_order(self):
        self.tl.reorder_tasks([3, 2, 1])
        self.assertEqual(_order(self.tl), ["three", "two", "one"])

    def test_reorder_subset_moves_listed_first_stably(self):
        """Unlisted tasks keep their relative order AFTER the listed
        ones (documented stable-partial semantics)."""
        tl = parse_plan(_plan(
            (1, " ", "a"), (2, " ", "b"), (3, " ", "c"), (4, " ", "d")))
        tl.reorder_tasks([3, 1])
        self.assertEqual(_order(tl), ["c", "a", "b", "d"])

    def test_reorder_single_id_promotes_it_to_the_front(self):
        """Stable-partial semantics: a lone listed id moves ahead of
        the unlisted ones."""
        self.tl.reorder_tasks([2])
        self.assertEqual(_order(self.tl), ["two", "one", "three"])

    def test_reorder_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            self.tl.reorder_tasks([1, 1])

    def test_reorder_unknown_id_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.tl.reorder_tasks([1, 99])

    def test_reorder_validates_everything_before_mutating(self):
        """A completed id anywhere in the list aborts the WHOLE
        operation, leaving the AST exactly as it was."""
        tl = parse_plan(_plan((1, " ", "a"), (2, ">", "b"), (3, "x", "c")))
        before = _order(tl)
        with self.assertRaises(CompletedTaskError):
            tl.reorder_tasks([3, 2, 1])
        self.assertEqual(_order(tl), before)

    def test_reorder_completed_task_is_refused(self):
        tl = parse_plan(_plan((1, " ", "open"), (2, "x", "done")))
        with self.assertRaises(CompletedTaskError):
            tl.reorder_tasks([2, 1])
        self.assertEqual(_order(tl), ["open", "done"])

    # -- invariants ------------------------------------------------------ #

    def test_reorder_never_changes_status_or_title(self):
        before = {t.id: (t.status, t.title) for t in self.tl.tasks}
        self.tl.reorder_tasks([3, 1, 2])
        after = {t.id: (t.status, t.title) for t in self.tl.tasks}
        self.assertEqual(before, after)

    def test_completed_task_slot_is_never_permuted(self):
        """A DONE task between two active ones keeps its LINE."""
        tl = parse_plan(_plan(
            (1, " ", "a"), (2, "x", "b"), (3, " ", "c"), (4, " ", "d")))
        done_line = tl.by_id(2).line_number
        tl.reorder_tasks([4, 3, 1])
        self.assertEqual(tl.by_id(2).line_number, done_line)

    def test_line_numbers_stay_unique_after_reorder(self):
        self.tl.reorder_tasks([3, 1, 2])
        self.tl.move_task(1, 3)
        lines = [t.line_number for t in self.tl.tasks]
        self.assertEqual(len(lines), len(set(lines)))


class _ReorderHarness(unittest.TestCase):
    """Isolated HOME + a real project on disk."""

    _ENV_KEYS = ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                 "CK_SANDBOX_SHELL", "CK_SANDBOX_ROOT")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "proj"
        (self.project / ".ck").mkdir(parents=True)
        self.plan = self.project / ".ck" / "PLAN.md"

        saved = {k: os.environ.get(k) for k in self._ENV_KEYS}
        os.environ["HOME"] = str(self.home)
        os.environ["CK_SANDBOX_ROOT"] = str(self.root / ".sandbox")
        for key in ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                    "CK_SANDBOX_SHELL"):
            os.environ.pop(key, None)

        def _restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(_restore)

    def write(self, *rows: str) -> None:
        self.plan.write_text(_plan(*rows), encoding="utf-8")

    def run_cli(self, argv) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch("pathlib.Path.cwd", return_value=self.project), \
                redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()

    def titles(self) -> list[str]:
        return [t.title for t in
                sorted(parse_plan(self.plan.read_text(encoding="utf-8")).tasks,
                       key=lambda t: t.line_number)
                if t.status != TaskStatus.DONE]


class TestReorderCli(_ReorderHarness):
    """The CLI contract: messages, exit codes, file contents."""

    def setUp(self):
        super().setUp()
        self.write((1, " ", "alpha"), (2, " ", "beta"), (3, " ", "gamma"))

    # -- move ------------------------------------------------------------ #

    def test_move_reports_position(self):
        code, out = self.run_cli(["move", "3", "1"])
        self.assertEqual(code, 0, out)
        self.assertIn("[ok] Moved task #3 -> position 1",
                      ckui.strip_ansi(out))
        self.assertEqual(self.titles(), ["gamma", "alpha", "beta"])

    def test_move_top_and_bottom(self):
        code, out = self.run_cli(["move", "1", "top"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.titles()[0], "alpha")
        code, out = self.run_cli(["move", "1", "bottom"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.titles()[-1], "alpha")

    def test_move_requires_two_arguments(self):
        code, out = self.run_cli(["move", "1"])
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck move", out)

    def test_move_unknown_id_exits_2(self):
        code, out = self.run_cli(["move", "99", "1"])
        self.assertEqual(code, 2)
        self.assertIn("No task with id 99", ckui.strip_ansi(out))

    def test_move_out_of_range_exits_2(self):
        code, out = self.run_cli(["move", "1", "9"])
        self.assertEqual(code, 2)
        self.assertIn("out of range", ckui.strip_ansi(out))

    def test_move_non_numeric_id_exits_2(self):
        code, out = self.run_cli(["move", "abc", "1"])
        self.assertEqual(code, 2)
        self.assertIn("must be a number", ckui.strip_ansi(out))

    # -- swap ------------------------------------------------------------ #

    def test_swap_reports_both_ids(self):
        code, out = self.run_cli(["swap", "1", "3"])
        self.assertEqual(code, 0, out)
        self.assertIn("[ok] Swapped task #1 <-> task #3",
                      ckui.strip_ansi(out))
        self.assertEqual(self.titles(), ["gamma", "beta", "alpha"])

    def test_swap_requires_two_arguments(self):
        code, out = self.run_cli(["swap", "1"])
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck swap", out)

    # -- reorder --------------------------------------------------------- #

    def test_reorder_applies_order(self):
        code, out = self.run_cli(["reorder", "3", "2", "1"])
        self.assertEqual(code, 0, out)
        self.assertIn("[ok] Reordered active tasks.", ckui.strip_ansi(out))
        self.assertEqual(self.titles(), ["gamma", "beta", "alpha"])

    def test_reorder_requires_at_least_one_id(self):
        code, out = self.run_cli(["reorder"])
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck reorder", out)

    def test_reorder_duplicate_id_exits_2(self):
        code, out = self.run_cli(["reorder", "1", "1"])
        self.assertEqual(code, 2)
        self.assertIn("Duplicate task ID", ckui.strip_ansi(out))


class TestReorderCompletedForbidden(_ReorderHarness):
    """The STRICT rule: completed tasks can never be reordered."""

    def setUp(self):
        super().setUp()
        self.write((1, " ", "alpha"), (2, "x", "beta"), (3, " ", "gamma"))
        self.before = self.plan.read_text(encoding="utf-8")

    def _assert_refused(self, argv, task_id):
        code, out = self.run_cli(argv)
        self.assertEqual(code, 1, out)
        self.assertIn(
            f"[err] Cannot move completed task #{task_id}. "
            "Only pending/active tasks can be reordered.",
            ckui.strip_ansi(out))
        # The document is byte-identical: nothing half-applied.
        self.assertEqual(self.plan.read_text(encoding="utf-8"), self.before)

    def test_move_completed_task_refused(self):
        self._assert_refused(["move", "2", "1"], 2)

    def test_swap_completed_task_refused(self):
        self._assert_refused(["swap", "1", "2"], 2)

    def test_reorder_completed_task_refused(self):
        self._assert_refused(["reorder", "3", "2", "1"], 2)

    def test_reorder_refuses_even_when_completed_is_last(self):
        self._assert_refused(["reorder", "1", "2"], 2)

    def test_completed_refusal_is_not_a_generic_error(self):
        _code, out = self.run_cli(["move", "2", "1"])
        plain = ckui.strip_ansi(out)
        self.assertNotIn("I/O error", plain)
        self.assertNotIn("Traceback", out)

    def test_pending_tasks_still_reorderable_around_a_done_one(self):
        """The rule forbids touching the DONE task, not reordering."""
        code, out = self.run_cli(["move", "3", "1"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.titles(), ["gamma", "alpha"])


class TestReorderPreservesDocument(_ReorderHarness):
    """A reorder must not disturb anything but task order."""

    def setUp(self):
        super().setUp()
        self.write((1, " ", "alpha"), (2, " ", "beta"))
        self.plan.write_text(
            "# proj\n\nSome prose line.\n\n## Current Sprint\n"
            "- [ ] alpha\n- [ ] beta\n\n## Completed\n- [x] old work\n",
            encoding="utf-8")

    def test_headers_prose_and_completed_survive(self):
        code, out = self.run_cli(["reorder", "2", "1"])
        self.assertEqual(code, 0, out)
        text = self.plan.read_text(encoding="utf-8")
        self.assertIn("# proj", text)
        self.assertIn("Some prose line.", text)
        self.assertIn("## Current Sprint", text)
        self.assertIn("## Completed", text)
        self.assertIn("- [x] old work", text)
        self.assertEqual(self.titles(), ["beta", "alpha"])

    def test_focus_marker_survives_a_move(self):
        self.write((1, " ", "alpha"), (2, ">", "beta"), (3, " ", "gamma"))
        code, out = self.run_cli(["move", "2", "3"])
        self.assertEqual(code, 0, out)
        tl = parse_plan(self.plan.read_text(encoding="utf-8"))
        focused = [t for t in tl.tasks if t.status == TaskStatus.FOCUSED]
        self.assertEqual(len(focused), 1)
        self.assertEqual(focused[0].title, "beta")


class TestReorderAfterAdd(_ReorderHarness):
    """A task added in the same session has no source slot yet."""

    def test_move_works_for_a_task_added_in_the_same_session(self):
        self.write((1, " ", "alpha"))
        ck = ContextKeeper(root=self.project)
        ck.add_task("beta")
        ck.add_task("gamma")

        code, out = self.run_cli(["move", "3", "1"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.titles(), ["gamma", "alpha", "beta"])

    def test_reorder_covers_freshly_added_tasks(self):
        self.write((1, " ", "alpha"))
        ck = ContextKeeper(root=self.project)
        ck.add_task("beta")
        ck.add_task("gamma")

        code, out = self.run_cli(["reorder", "3", "2", "1"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.titles(), ["gamma", "beta", "alpha"])


if __name__ == "__main__":
    unittest.main()