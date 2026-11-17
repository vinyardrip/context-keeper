"""Stable task UUIDs as first-class ID references (v0.13.6).

Every task carries ``<!-- id: a1b2c3d4 -->`` — an 8-character stable
reference written once and never regenerated. v0.13.6 promotes it
from a hidden anchor to a first-class handle:

- ``ck list`` / ``ck st`` / ``ck notes`` suffix every task row with
  `` [#a1b2c3d4]`` (the copy-paste-safe address);
- ``ck done`` / ``ck delete`` / ``ck start`` / ``ck focus`` /
  ``ck note`` / ``ck desc`` accept it anywhere a positional ID is
  expected, in specs too (``a1b2c3d4,3-4``);
- ``ck delete`` accepts full specs (ranges / lists / uuids) with one
  confirmation, closing the sequential-shift bug class.

Positional IDs still work everywhere and keep their old error
contracts (``ValueError`` for garbage specs, ``KeyError`` for unknown
IDs).
"""

import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib import config as ckconfig
from cklib.cli import main
from cklib.core import ContextKeeper, _parse_id_spec, resolve_id_spec
from cklib.models import TaskList, Task, TaskStatus, new_task_uuid


def _run_main(argv, root=None):
    cwd = os.getcwd()
    if root is not None:
        os.chdir(root)
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = main(argv)
    finally:
        os.chdir(cwd)
    return code, buf_out.getvalue(), buf_err.getvalue()


def _tl(*titles: str, uuids: bool = True) -> TaskList:
    tl = TaskList()
    for i, title in enumerate(titles, start=1):
        task = Task(id=i, title=title, status=TaskStatus.OPEN,
                    line_number=i, marker_raw="- []")
        if uuids:
            task.uuid = new_task_uuid()
        tl.tasks.append(task)
    return tl


class TestResolveIdSpec(unittest.TestCase):
    """``resolve_id_spec`` — numeric chunks plus stable uuid tokens."""

    def test_pure_numeric_spec_matches_parse_id_spec(self):
        tl = _tl("a", "b", "c")
        self.assertEqual(resolve_id_spec("2", tl), _parse_id_spec("2"))
        self.assertEqual(resolve_id_spec("1-3", tl), _parse_id_spec("1-3"))
        self.assertEqual(
            resolve_id_spec("1,3", tl), _parse_id_spec("1,3"))

    def test_uuid_chunk_resolves_to_positional_id(self):
        tl = _tl("a", "b", "c")
        target = tl.tasks[1]
        self.assertEqual(resolve_id_spec(target.uuid, tl), [2])

    def test_mixed_spec_uuid_and_range(self):
        tl = _tl("a", "b", "c")
        self.assertEqual(
            resolve_id_spec(f"{tl.tasks[0].uuid},2-3", tl), [1, 2, 3])

    def test_unknown_uuid_raises_keyerror(self):
        tl = _tl("a", "b", "c")
        with self.assertRaisesRegex(KeyError, "No task with stable id"):
            resolve_id_spec("zzzzzzzz", tl)

    def test_garbage_spec_keeps_valueerror_contract(self):
        """Not uuid-shaped (not 8 alnum chars) -> dropped -> ValueError
        path upstream (resolve returns empty)."""
        tl = _tl("a", "b", "c")
        self.assertEqual(resolve_id_spec("not-a-spec", tl), [])
        self.assertEqual(resolve_id_spec("work", tl), [])

    def test_eight_digit_chunk_is_numeric_not_uuid(self):
        tl = _tl("a", "b", "c")
        self.assertEqual(resolve_id_spec("12345678", tl), [12345678])


class TestTaskListByUuid(unittest.TestCase):
    def test_by_uuid_resolves_and_rejects(self):
        tl = _tl("a", "b")
        self.assertIs(tl.by_uuid(tl.tasks[0].uuid), tl.tasks[0])
        self.assertIsNone(tl.by_uuid("zzzzzzzz"))
        self.assertIsNone(tl.by_uuid(""))
        # Legacy task without a uuid comment never matches.
        legacy = Task(id=3, title="c", status=TaskStatus.OPEN,
                      line_number=3, marker_raw="- []")
        tl.tasks.append(legacy)
        self.assertIs(tl.by_uuid(legacy.uuid or ""), None)


class TestProjectUuidCommands(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ck_stable_ids_")
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        self.old_config = ckconfig.GLOBAL_CONFIG_DIR
        ck, _ = load_keeper_pair(self.root)
        self.keeper = ck

    def tearDown(self):
        os.chdir(self.old_cwd)
        ckconfig.GLOBAL_CONFIG_DIR = self.old_config

    # -- helpers -------------------------------------------------------

    def _plan_tasks(self):
        return self.keeper._load_plan()

    def _seed(self, *titles):
        for title in titles:
            self.keeper.add_task(title)

    # -- done ----------------------------------------------------------

    def test_done_accepts_uuid(self):
        self._seed("alpha", "beta")
        uuid = self._plan_tasks().tasks[1].uuid
        ids = self.keeper.done(uuid)
        self.assertEqual(ids, [2])
        remaining = [t.title for t in self._plan_tasks().tasks]
        self.assertEqual(remaining, ["alpha"])

    def test_done_mixed_uuid_and_range(self):
        self._seed("alpha", "beta", "gamma")
        uuid = self._plan_tasks().tasks[0].uuid
        ids = self.keeper.done(f"{uuid},2-3")
        self.assertEqual(sorted(ids), [1, 2, 3])
        self.assertEqual([t.title for t in self._plan_tasks().tasks], [])

    def test_done_unknown_uuid_is_keyerror(self):
        self._seed("alpha")
        with self.assertRaisesRegex(KeyError, "No task with stable id"):
            self.keeper.done("zzzzzzzz")

    def test_delete_accepts_uuid(self):
        self._seed("alpha", "beta", "gamma")
        uuid = self._plan_tasks().tasks[1].uuid
        removed = self.keeper.delete_task(
            self.keeper.resolve_task_ref(uuid))
        self.assertEqual(removed["title"], "beta")

    def test_delete_spec_range_and_list(self):
        """v0.13.6: ``ck delete 2-3`` / ``ck delete 2,3`` are valid."""
        self._seed("alpha", "beta", "gamma")
        code, out, err = _run_main(["delete", "2-3", "-f"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Deleted 2 task(s)", out)
        remaining = [t.title for t in self._plan_tasks().tasks]
        self.assertEqual(remaining, ["alpha"])

    def test_delete_uuid_spec(self):
        self._seed("alpha", "beta", "gamma")
        uuid = self._plan_tasks().tasks[0].uuid
        code, out, err = _run_main(["delete", uuid, "-f"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Deleted task #1: alpha", out)
        remaining = [t.title for t in self._plan_tasks().tasks]
        self.assertEqual(remaining, ["beta", "gamma"])

    def test_delete_non_numeric_non_uuid_points_at_space_delete(self):
        self._seed("alpha")
        code, out, err = _run_main(["delete", "work", "-f"])
        self.assertEqual(code, 2)
        self.assertIn("Invalid task ID or spec", out)
        self.assertIn("ck space delete work", out)
        self.assertEqual(len(self._plan_tasks().tasks), 1)

    # -- start / focus ---------------------------------------------------

    def test_start_accepts_uuid(self):
        self._seed("alpha", "beta")
        uuid = self._plan_tasks().tasks[0].uuid
        code, out, err = _run_main(["start", uuid])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Focused [1]: alpha", out)

    def test_focus_accepts_uuid(self):
        self._seed("alpha", "beta")
        uuid = self._plan_tasks().tasks[1].uuid
        code, out, err = _run_main(["focus", uuid])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Focused [2]: beta", out)

    def test_start_invalid_token_still_errors(self):
        self._seed("alpha")
        code, out, err = _run_main(["start", "not-a-ref"])
        self.assertEqual(code, 2)
        self.assertIn("Invalid task ID", out)

    # -- note / desc ------------------------------------------------------

    def test_note_accepts_uuid(self):
        self._seed("alpha", "beta")
        uuid = self._plan_tasks().tasks[0].uuid
        code, out, err = _run_main(["note", uuid, "hello"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Note saved for [1]", out)

    def test_desc_accepts_uuid(self):
        self._seed("alpha", "beta")
        uuid = self._plan_tasks().tasks[0].uuid
        code, out, err = _run_main(["desc", uuid, "body text"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Description set", out)


class TestStableSuffixRendering(unittest.TestCase):
    """Every task row in list/st/notes ends with `` [#uuid]``."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ck_stable_ids_")
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        self.old_config = ckconfig.GLOBAL_CONFIG_DIR

    def tearDown(self):
        os.chdir(self.old_cwd)
        ckconfig.GLOBAL_CONFIG_DIR = self.old_config

    def test_list_rows_carry_uuid_suffix(self):
        code, out, err = _run_main(["add", "first"])
        self.assertEqual(code, 0, out + err)
        _run_main(["add", "second"])
        code, out, _err = _run_main(["list"])
        self.assertEqual(code, 0)
        for line in out.splitlines():
            if re.match(r"^\[[ x>]\] \d+\.", line):
                self.assertRegex(line, r" \[#[0-9a-z]{8}\]$")

    def test_legacy_tasks_without_uuid_render_clean(self):
        """Hand-written PLAN.md lines without ``<!-- id: -->`` get no
        suffix — the renderer never invents references."""
        plan_dir = os.path.join(self.root, ".ck")
        os.makedirs(plan_dir, exist_ok=True)
        plan = os.path.join(plan_dir, "PLAN.md")
        with open(plan, "w", encoding="utf-8") as fh:
            fh.write("## Current Sprint\n- [] legacy task\n")
        code, out, _err = _run_main(["list"])
        self.assertEqual(code, 0)
        self.assertIn("[ ] 1. legacy task\n", out)


def load_keeper_pair(root):
    """A keeper pair bound to ``root`` (test-local helper)."""
    keeper = ContextKeeper(Path(root))
    keeper.init()
    return keeper, keeper


if __name__ == "__main__":
    unittest.main()
