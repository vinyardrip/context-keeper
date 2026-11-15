"""Tests for append-only process notes (v0.13.0 inline model).

Covers the required scenarios: append semantics, missing text,
``--clear`` / ``--list``, inline round-trip through plan rewrites,
HISTORY archiving on ``ck done``, space parity, read-only stability,
and the v0.13.0 write-gate for unmigrated projects.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib import spaces
from cklib import ui as ckui
from cklib.cli import main
from cklib.core import ContextKeeper, format_note_time


class _Harness(unittest.TestCase):
    PLAN = "# P\n## Current Sprint\n- [ ] task\n"

    def _project(self, plan: str = PLAN) -> ContextKeeper:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name) / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(plan, encoding="utf-8")
        # v0.13.0 write-gate: raw fixtures carry the current marker.
        ck.ck_path.joinpath("version").write_text("0.13.0\n",
                                                  encoding="utf-8")
        return ck

    def _run(self, argv, cwd: Path) -> tuple[int, str]:
        buf = io.StringIO()
        old = Path.cwd()
        os.chdir(cwd)
        try:
            with redirect_stdout(buf):
                code = main(argv)
        finally:
            os.chdir(old)
        return code, ckui.strip_ansi(buf.getvalue())


class TestProjectNotes(_Harness):
    def test_note_appends_two_timestamped_entries(self):
        ck = self._project()
        self.assertEqual(self._run(["note", "1", "first"], ck.root)[0], 0)
        self.assertEqual(self._run(["note", "1", "second"], ck.root)[0], 0)

        entries = ck.get_note_entries(1)
        self.assertEqual([e["text"] for e in entries], ["first", "second"])
        self.assertTrue(entries[0]["created"].endswith("+00:00"))
        self.assertTrue(entries[1]["created"].endswith("+00:00"))
        # Inline in PLAN.md, one indented note line per entry.
        plan = ck.plan_file.read_text(encoding="utf-8")
        self.assertEqual(plan.count("* Note ("), 2)

    def test_note_without_text_is_clean_error(self):
        ck = self._project()
        code, out = self._run(["note", "1"], ck.root)
        self.assertEqual(code, 2)
        self.assertIn("[err] Note text required.", out)
        self.assertIn("Usage: ck note", out)

    def test_clear_removes_all_notes(self):
        ck = self._project()
        self._run(["note", "1", "first"], ck.root)
        self._run(["note", "1", "second"], ck.root)

        code, out = self._run(["note", "1", "--clear"], ck.root)

        self.assertEqual(code, 0)
        self.assertIn("* Cleared 2 note(s)", out)
        self.assertEqual(ck.get_note_entries(1), [])
        plan = ck.plan_file.read_text(encoding="utf-8")
        self.assertNotIn("* Note", plan)

    def test_list_prints_both_notes_with_timestamps(self):
        ck = self._project()
        self._run(["note", "1", "first"], ck.root)
        self._run(["note", "1", "second"], ck.root)

        code, out = self._run(["note", "1", "--list"], ck.root)

        self.assertEqual(code, 0)
        self.assertIn("[1] task", out)
        self.assertIn("first", out)
        self.assertIn("second", out)
        self.assertRegex(out, r"\d{2}:\d{2} UTC")

    def test_note_round_trips_through_plan_rewrite(self):
        ck = self._project()
        self._run(["note", "1", "kept note"], ck.root)

        # A rewrite-inducing mutation must not lose the inline note.
        self.assertEqual(self._run(["add", "second task"], ck.root)[0], 0)

        entries = ck.get_note_entries(1)
        self.assertEqual([e["text"] for e in entries], ["kept note"])
        plan = ck.plan_file.read_text(encoding="utf-8")
        self.assertIn("kept note", plan)
        self.assertIn("second task", plan)

    def test_done_archives_every_note(self):
        ck = self._project()
        self._run(["note", "1", "first"], ck.root)
        self._run(["note", "1", "second"], ck.root)

        code, _ = self._run(["done", "1"], ck.root)

        self.assertEqual(code, 0)
        history = ck.history_file.read_text(encoding="utf-8")
        self.assertIn("first", history)
        self.assertIn("second", history)
        self.assertEqual(history.count("* Note ("), 2)
        self.assertNotIn("* Note: first", history)
        self.assertNotIn("* Note: second", history)
        self.assertLess(history.index("first"), history.index("second"))
        # The graduated task left the plan (and its notes with it).
        plan = ck.plan_file.read_text(encoding="utf-8")
        self.assertNotIn("* Note", plan)
        self.assertNotIn("- [ ] task", plan)

    def test_done_single_note_keeps_one_line(self):
        ck = self._project()
        self._run(["note", "1", "only"], ck.root)

        code, _ = self._run(["done", "1"], ck.root)

        self.assertEqual(code, 0)
        history = ck.history_file.read_text(encoding="utf-8")
        # One note line, stamped, directly under the entry.
        self.assertRegex(history, r"\* Note \(\d{4}-\d{2}-\d{2} \d{2}:\d{2}"
                                  r" UTC\): only")

    def test_format_note_time_today_and_older(self):
        now = datetime(2026, 11, 3, 18, 14, tzinfo=timezone.utc)

        self.assertEqual(
            format_note_time("2026-11-03T09:05:00+00:00", now),
            "09:05 UTC",
        )
        self.assertEqual(
            format_note_time("2026-11-01T23:59:00+00:00", now),
            "2026-11-01 23:59 UTC",
        )

    def test_write_gate_refuses_unmigrated_project(self):
        ck = self._project()
        ck.ck_path.joinpath("version").unlink()

        code, out = self._run(["note", "1", "too late"], ck.root)

        self.assertEqual(code, 2)
        self.assertIn("Run `ck migrate`", out)
        plan = ck.plan_file.read_text(encoding="utf-8")
        self.assertNotIn("too late", plan)

    def test_read_only_notes_keeps_plan_hash(self):
        ck = self._project()
        self._run(["note", "1", "stable"], ck.root)
        before = hashlib.sha256(
            ck.plan_file.read_bytes()).hexdigest()

        code, out = self._run(["notes"], ck.root)

        self.assertEqual(code, 0)
        self.assertIn("stable", out)
        after = hashlib.sha256(
            ck.plan_file.read_bytes()).hexdigest()
        self.assertEqual(before, after)


class TestSpaceNotes(_Harness):
    def _seed(self, *titles: str) -> None:
        for title in titles:
            code, out = self._run(["local", "add", title], Path.cwd())
            self.assertEqual(code, 0, out)

    def test_space_note_appends_two_entries(self):
        self._seed("space task")
        self.assertEqual(
            self._run(["local", "note", "1", "a"], Path.cwd())[0], 0)
        self.assertEqual(
            self._run(["local", "note", "1", "b"], Path.cwd())[0], 0)

        state = json.loads(
            spaces.state_path("local").read_text(encoding="utf-8"))
        self.assertEqual(
            [e["text"] for e in state["notes"]["1"]], ["a", "b"])
        self.assertTrue(state["notes"]["1"][0]["created"])
        self.assertEqual(
            spaces.SpaceManager("local").get_note(1), "b")

    def test_space_clear_removes_notes(self):
        self._seed("space task")
        self._run(["local", "note", "1", "a"], Path.cwd())
        self._run(["local", "note", "1", "b"], Path.cwd())

        code, out = self._run(
            ["local", "note", "1", "--clear"], Path.cwd())

        self.assertEqual(code, 0)
        self.assertIn("* Cleared 2 note(s)", out)
        state = json.loads(
            spaces.state_path("local").read_text(encoding="utf-8"))
        self.assertNotIn("1", state.get("notes", {}))


if __name__ == "__main__":
    unittest.main()
