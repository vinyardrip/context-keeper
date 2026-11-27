"""Task reordering inside spaces and daily/period journals (v0.14.1).

Covered surface:

- ``SpaceManager.move_task`` / ``swap_tasks`` / ``reorder_tasks`` use the
  shared ``TaskList`` slot engine: positional moves, ``top`` / ``bottom``
  targets and stable partial reorders;
- completed ``[x]`` tasks are refused as a whole operation
  (``CompletedTaskError``) and the space file stays byte-identical;
- invalid / out-of-range IDs surface as clean CLI usage errors (exit 2);
- stable UUIDs, inline descriptions and sidecar notes travel with their
  task, including when a completed row shifts the positional IDs;
- the same verbs work through the CLI for non-daily spaces and the
  ``today`` / ``week`` daily journals, and ``reorder`` re-renders the
  refreshed listing.
"""

from __future__ import annotations

import io
import json
import os
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib import spaces
from cklib.cli import main
from cklib.models import CompletedTaskError


def _cfg_dir() -> Path:
    """The isolated CK_CONFIG_DIR of the current test (conftest pins it)."""
    return Path(os.environ["CK_CONFIG_DIR"])


def _titles(mgr) -> list[str]:
    return [task.title for task in mgr.load().reorderable_tasks()]


class _Harness(unittest.TestCase):
    def _run(self, argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _space_file(self, name: str) -> Path:
        return _cfg_dir() / "spaces" / f"{name}.md"

    def _sidecar(self, name: str) -> dict:
        path = _cfg_dir() / "spaces" / f"{name}.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _make_space(self, name: str, *titles: str):
        mgr = spaces.SpaceManager(name, create=True)
        for title in titles:
            mgr.add_task(title)
        return mgr


class TestSpaceManagerReorder(_Harness):
    """Direct unit tests for the space reorder engine."""

    def test_move_position_top_and_bottom(self):
        mgr = self._make_space("work", "one", "two", "three", "four")

        self.assertEqual(mgr.move_task(3, 1), "Moved task #3 -> position 1")
        self.assertEqual(_titles(mgr), ["three", "one", "two", "four"])

        self.assertEqual(mgr.move_task(1, "bottom"),
                         "Moved task #1 -> position 4")
        self.assertEqual(_titles(mgr), ["one", "two", "four", "three"])

        self.assertEqual(mgr.move_task(2, "top"),
                         "Moved task #2 -> position 1")
        self.assertEqual(_titles(mgr), ["two", "one", "four", "three"])

    def test_swap_exchanges_two_active_tasks(self):
        mgr = self._make_space("work", "one", "two", "three")
        self.assertEqual(mgr.swap_tasks(1, 3),
                         "Swapped task #1 <-> task #3")
        self.assertEqual(_titles(mgr), ["three", "two", "one"])

    def test_reorder_is_stable_partial(self):
        mgr = self._make_space("work", "one", "two", "three", "four")
        message = mgr.reorder_tasks([3, 1])
        self.assertIn("Reordered active tasks: 3, 1", message)
        self.assertIn("1. three", message)
        self.assertIn("2. one", message)
        self.assertIn("3. two", message)
        self.assertIn("4. four", message)
        self.assertEqual(_titles(mgr), ["three", "one", "two", "four"])

    def test_completed_task_refused_by_every_verb(self):
        mgr = self._make_space("work", "one", "two", "three")
        mgr.done("2")
        before = self._space_file("work").read_bytes()

        for call in (
            lambda: mgr.move_task(2, 1),
            lambda: mgr.move_task(2, "top"),
            lambda: mgr.swap_tasks(1, 2),
            lambda: mgr.reorder_tasks([2, 1]),
        ):
            with self.subTest(call=call):
                with self.assertRaises(CompletedTaskError) as ctx:
                    call()
                self.assertEqual(ctx.exception.task_id, 2)
                self.assertEqual(
                    self._space_file("work").read_bytes(), before)

    def test_unknown_id_and_invalid_position(self):
        mgr = self._make_space("work", "one", "two", "three")

        with self.assertRaises(KeyError):
            mgr.move_task(99, 1)
        with self.assertRaises(ValueError):
            mgr.move_task(1, 99)
        with self.assertRaises(ValueError):
            mgr.move_task(1, "middle")
        with self.assertRaises(KeyError):
            mgr.swap_tasks(1, 99)
        with self.assertRaises(KeyError):
            mgr.reorder_tasks([1, 99])
        with self.assertRaises(ValueError):
            mgr.reorder_tasks([1, 1])

    def test_uuid_description_and_sidecar_note_follow_task(self):
        path = self._space_file("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "## Current Sprint\n"
            "- [ ] alpha <!-- id: aaaaaaaa -->\n"
            "  > alpha description\n"
            "- [ ] beta <!-- id: bbbbbbbb -->\n"
            "  > beta description\n",
            encoding="utf-8",
        )
        mgr = spaces.SpaceManager("work")
        mgr.set_note(1, "note-alpha")

        mgr.move_task(1, "bottom")

        text = path.read_text(encoding="utf-8")
        self.assertLess(text.index("beta"), text.index("alpha"))
        self.assertIn("  > beta description", text)
        self.assertIn("  > alpha description", text)
        self.assertEqual(mgr.load().by_id(2).uuid, "aaaaaaaa")
        self.assertEqual(mgr.get_note(2), "note-alpha")
        self.assertEqual(self._sidecar("work")["notes"]["2"][0]["text"],
                         "note-alpha")

    def test_sidecar_remap_keeps_completed_note_in_place(self):
        mgr = self._make_space("work", "one", "two", "three")
        mgr.done("2")
        mgr.set_note(1, "note-one")
        mgr.set_note(2, "note-two")
        mgr.set_note(3, "note-three")

        mgr.move_task(3, "top")

        # Document order: three (active), two (completed), one (active).
        self.assertEqual(mgr.get_note(1), "note-three")
        self.assertEqual(mgr.get_note(2), "note-two")
        self.assertEqual(mgr.get_note(3), "note-one")
        listing = mgr.list_tasks()
        self.assertIn("three", listing)
        self.assertIn("* Note: note-three", listing)


class TestSpaceReorderCli(_Harness):
    """CLI parity for ``ck <space> move|swap|reorder``."""

    def _seed_local(self, *titles: str) -> None:
        for title in titles:
            code, out, err = self._run(["local", "add", title])
            self.assertEqual(code, 0, out + err)

    def test_move_swap_and_reorder_messages(self):
        self._seed_local("one", "two", "three")

        code, out, err = self._run(["local", "move", "3", "1"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("[ok] Moved task #3 -> position 1", out)

        code, out, err = self._run(["local", "swap", "1", "2"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("[ok] Swapped task #1 <-> task #2", out)

        code, out, err = self._run(["local", "reorder", "2", "1"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("[ok] Reordered active tasks: 2, 1", out)
        self.assertIn("     New order:", out)
        # `reorder` auto-renders the refreshed listing after the summary.
        self.assertIn("[local] 0/3 done", out)
        self.assertIn("1. three", out)
        self.assertIn("2. one", out)
        self.assertIn("3. two", out)

    def test_uuid_operands_are_accepted(self):
        self._seed_local("one", "two", "three")
        code, out, err = self._run(["local", "list"])
        self.assertEqual(code, 0, out + err)
        uuids = re.findall(r"\[#([a-z0-9]+)\]", out)
        self.assertEqual(len(uuids), 3)

        code, out, err = self._run(["local", "move", uuids[2], "top"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Moved task #3 -> position 1", out)
        self.assertEqual(_titles(spaces.SpaceManager("local")),
                         ["three", "one", "two"])

    def test_completed_refusal_is_exit_1_and_byte_identical(self):
        self._seed_local("one", "two", "three")
        code, out, err = self._run(["local", "done", "2"])
        self.assertEqual(code, 0, out + err)
        before = self._space_file("local").read_bytes()

        code, out, err = self._run(["local", "move", "2", "1"])
        self.assertEqual(code, 1)
        self.assertIn(
            "Cannot move completed task #2. "
            "Only pending/active tasks can be reordered.", out)
        self.assertEqual(self._space_file("local").read_bytes(), before)

    def test_invalid_ids_and_positions_are_usage_errors(self):
        self._seed_local("one", "two", "three")
        cases = (
            (["local", "move", "99", "1"], "No task with id 99"),
            (["local", "move", "1", "99"], "out of range"),
            (["local", "move", "nope", "1"], "Invalid task ID"),
            (["local", "swap", "1", "99"], "No task with id 99"),
            (["local", "reorder", "1", "99"], "No task with id 99"),
            (["local", "reorder"], "Usage:"),
        )
        for argv, needle in cases:
            with self.subTest(argv=argv):
                code, out, err = self._run(argv)
                self.assertEqual(code, 2, out + err)
                self.assertIn(needle, out + err)

    def test_note_travels_with_moved_task(self):
        self._seed_local("one", "two", "three")
        code, out, err = self._run(["local", "note", "1", "keep-me"])
        self.assertEqual(code, 0, out + err)

        code, out, err = self._run(["local", "move", "1", "bottom"])
        self.assertEqual(code, 0, out + err)

        code, out, err = self._run(["local", "list"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("3. one", out)
        self.assertIn("* Note: keep-me", out)
        note_pos = out.index("* Note: keep-me")
        self.assertGreater(note_pos, out.index("3. one"))


class TestDailyReorderCli(_Harness):
    """Reordering works identically in daily and period journals."""

    def test_today_move_five_to_six(self):
        code, out, err = self._run(["today", "init"])
        self.assertEqual(code, 0, out + err)
        for number in range(1, 7):
            code, out, err = self._run(["today", "add", f"task{number}"])
            self.assertEqual(code, 0, out + err)

        code, out, err = self._run(["today", "move", "5", "6"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Moved task #5 -> position 6", out)

        code, out, err = self._run(["today", "list"])
        self.assertEqual(code, 0, out + err)
        order = [f"task{n}" for n in (1, 2, 3, 4, 6, 5)]
        positions = [out.index(title) for title in order]
        self.assertEqual(positions, sorted(positions))

    def test_week_reorder_renders_refreshed_listing(self):
        code, out, err = self._run(["week", "init"])
        self.assertEqual(code, 0, out + err)
        for title in ("one", "two", "three"):
            code, out, err = self._run(["week", "add", title])
            self.assertEqual(code, 0, out + err)

        code, out, err = self._run(["week", "reorder", "3", "1"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Reordered active tasks: 3, 1", out)
        self.assertIn("## Current Sprint (", out)
        self.assertIn("1. three", out)
        self.assertIn("2. one", out)

    def test_today_completed_row_is_refused(self):
        code, out, err = self._run(["today", "init"])
        self.assertEqual(code, 0, out + err)
        for title in ("one", "two", "three"):
            code, out, err = self._run(["today", "add", title])
            self.assertEqual(code, 0, out + err)

        path = self._space_file("today")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "- [ ] two", "- [x] two"),
            encoding="utf-8",
        )
        before = path.read_bytes()

        code, out, err = self._run(["today", "move", "2", "1"])
        self.assertEqual(code, 1)
        self.assertIn("Cannot move completed task #2", out)
        self.assertEqual(path.read_bytes(), before)

    def test_today_notes_survive_reorder(self):
        code, out, err = self._run(["today", "init"])
        self.assertEqual(code, 0, out + err)
        for title in ("one", "two", "three"):
            code, out, err = self._run(["today", "add", title])
            self.assertEqual(code, 0, out + err)
        code, out, err = self._run(["today", "note", "1", "daily-note"])
        self.assertEqual(code, 0, out + err)

        code, out, err = self._run(["today", "swap", "1", "3"])
        self.assertEqual(code, 0, out + err)

        code, out, err = self._run(["today", "list"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("3. one", out)
        self.assertIn("* Note: daily-note", out)


if __name__ == "__main__":
    unittest.main()
