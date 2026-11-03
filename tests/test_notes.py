"""Tests for append-only process notes (v0.10.0).

Covers the 10 required scenarios: append semantics, missing text,
``--clear`` / ``--list``, legacy string migration, round-trip,
HISTORY archiving, space parity, and read-only state stability.
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

    def _state(self, ck: ContextKeeper) -> dict:
        return json.loads(ck.state_file.read_text(encoding="utf-8"))


class TestProjectNotes(_Harness):
    def test_note_appends_two_timestamped_entries(self):
        ck = self._project()
        self.assertEqual(self._run(["note", "1", "first"], ck.root)[0], 0)
        self.assertEqual(self._run(["note", "1", "second"], ck.root)[0], 0)

        entries = self._state(ck)["notes"]["1"]
        self.assertEqual([e["text"] for e in entries], ["first", "second"])
        self.assertNotEqual(entries[0]["created"], entries[1]["created"])
        self.assertTrue(entries[0]["created"].endswith("+00:00"))

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
        state = self._state(ck)
        self.assertNotIn("1", state.get("notes", {}))
        self.assertNotIn("active_task", state)

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

    def test_legacy_string_migrates_in_memory(self):
        ck = self._project()
        ck.state_file.write_text(
            '{"notes": {"1": "legacy single"}}', encoding="utf-8")

        code, out = self._run(["notes"], ck.root)

        self.assertEqual(code, 0)
        self.assertIn("legacy single", out)
        entries = ck.get_note_entries(1)
        self.assertEqual([e["text"] for e in entries], ["legacy single"])
        self.assertTrue(entries[0]["created"].endswith("+00:00"))

    def test_append_after_legacy_persists_two_entries(self):
        ck = self._project()
        ck.state_file.write_text(
            '{"notes": {"1": "legacy single"}}', encoding="utf-8")

        self._run(["note", "1", "new"], ck.root)

        entries = self._state(ck)["notes"]["1"]
        self.assertEqual(
            [e["text"] for e in entries], ["legacy single", "new"])

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
        self.assertNotIn("1", self._state(ck).get("notes", {}))

    def test_done_single_note_keeps_legacy_line(self):
        ck = self._project()
        self._run(["note", "1", "only"], ck.root)

        code, _ = self._run(["done", "1"], ck.root)

        self.assertEqual(code, 0)
        history = ck.history_file.read_text(encoding="utf-8")
        self.assertIn("* Note: only", history)
        self.assertNotIn("* Note (", history)

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

    def test_legacy_active_note_migrates_and_write_drops_key(self):
        ck = self._project("# P\n## Current Sprint\n- [>] task\n")
        ck.state_file.write_text(json.dumps({
            "active_task": {"id": 1, "title": "task", "note": "legacy text"},
        }), encoding="utf-8")
        before = hashlib.sha256(ck.state_file.read_bytes()).hexdigest()

        code, out = self._run(["st"], ck.root)

        self.assertEqual(code, 0)
        self.assertIn("* Note: legacy text", out)
        # The migration happens in memory only: the read never writes.
        after = hashlib.sha256(ck.state_file.read_bytes()).hexdigest()
        self.assertEqual(before, after)

        self.assertEqual(self._run(["note", "1", "fresh"], ck.root)[0], 0)
        state = self._state(ck)
        self.assertEqual(
            [e["text"] for e in state["notes"]["1"]],
            ["legacy text", "fresh"],
        )
        self.assertNotIn("note", state["active_task"])

    def test_ledger_wins_over_legacy_active_note_read_only(self):
        ck = self._project("# P\n## Current Sprint\n- [>] task\n")
        ck.state_file.write_text(json.dumps({
            "active_task": {
                "id": 1, "title": "task", "note": "legacy-scalar",
            },
            "notes": {
                "1": [{
                    "text": "ledger-array",
                    "created": "2026-11-03T10:00:00+00:00",
                }],
            },
        }), encoding="utf-8")
        before = hashlib.sha256(ck.state_file.read_bytes()).hexdigest()

        code, out = self._run(["st"], ck.root)

        self.assertEqual(code, 0)
        self.assertIn("* Note: ledger-array", out)
        self.assertNotIn("legacy-scalar", out)
        after = hashlib.sha256(ck.state_file.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_read_only_notes_keeps_state_hash(self):
        ck = self._project()
        self._run(["note", "1", "stable"], ck.root)
        before = hashlib.sha256(
            ck.state_file.read_bytes()).hexdigest()

        code, out = self._run(["notes"], ck.root)

        self.assertEqual(code, 0)
        self.assertIn("stable", out)
        after = hashlib.sha256(ck.state_file.read_bytes()).hexdigest()
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
