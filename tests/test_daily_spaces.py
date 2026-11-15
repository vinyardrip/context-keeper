"""Tests for the extended DAILY_SPACES family (v0.14.0).

The v0.12.0 daily journal ``today`` gains period siblings: ``week``
(ISO week), ``month`` and ``year``. Guards:

- ``current_period`` formats: ``YYYY-MM-DD`` / ISO ``YYYY-Www`` (ISO
  week-year, so the New Year week keeps one identity) / ``YYYY-MM`` /
  ``YYYY``;
- ``daily_section_header`` renders period-appropriate dated headers
  (locale weekday for ``today``, locale month name for ``month``,
  bare stamps for ``week`` / ``year``);
- lifecycle parity with ``today``: explicit ``init`` (idempotent,
  state key ``<name>_date``), flat list view, no HISTORY on ``done``,
  ``save`` / ``log`` refused, bare ``ck <name>`` == list;
- reserved names: ``space create`` / ``rename`` refuse the family;
  an ACTIVE period journal is never renamable (the ``<name>_date``
  state key would orphan);
- lazy rollover on period change: completed rows deleted, open tasks
  carried with re-indexed IDs and remapped notes, missing file
  recreated; ``today``'s behavior is unchanged.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib import spaces
from cklib.cli import main


def _cfg_dir() -> Path:
    """The isolated CK_CONFIG_DIR of the current test (conftest pins it)."""
    return Path(os.environ["CK_CONFIG_DIR"])


class _Harness(unittest.TestCase):
    def _run(self, argv, cwd=None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = Path.cwd()
        if cwd is not None:
            os.chdir(cwd)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(argv)
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def _state(self) -> dict:
        path = _cfg_dir() / "state.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _set_state(self, **kwargs) -> None:
        state = self._state()
        state.update(kwargs)
        (_cfg_dir() / "state.json").write_text(
            json.dumps(state), encoding="utf-8")


class TestPeriodLogic(unittest.TestCase):
    def test_current_period_formats(self):
        cases = [
            ("today", datetime(2026, 11, 16, 23, 59), "2026-11-16"),
            ("week", datetime(2026, 11, 16), "2026-W47"),
            ("week", datetime(2026, 1, 1), "2026-W01"),
            # Jan 1 2027 is a Friday belonging to ISO week 2026-W53:
            # the ISO WEEK year, not the calendar year, identifies it.
            ("week", datetime(2027, 1, 1), "2026-W53"),
            ("month", datetime(2026, 11, 30), "2026-11"),
            ("year", datetime(2026, 12, 31, 23, 59), "2026"),
        ]
        for space, moment, expected in cases:
            with self.subTest(space=space, moment=moment):
                self.assertEqual(
                    spaces.current_period(space, now=moment), expected)

    def test_daily_spaces_family(self):
        self.assertEqual(
            spaces.DAILY_SPACES,
            {"today", "week", "month", "year"})

    def test_daily_date_key(self):
        self.assertEqual(spaces.daily_date_key("today"), "today_date")
        self.assertEqual(spaces.daily_date_key("week"), "week_date")
        self.assertEqual(spaces.daily_date_key("year"), "year_date")

    def test_header_formats(self):
        self.assertIn(
            "## Current Sprint (2026-11-16, ",
            spaces.daily_section_header("today", "2026-11-16"))
        self.assertEqual(
            spaces.daily_section_header("week", "2026-W47"),
            "## Current Sprint (2026-W47)")
        self.assertIn(
            "## Current Sprint (2026-11, ",
            spaces.daily_section_header("month", "2026-11"))
        self.assertEqual(
            spaces.daily_section_header("year", "2026"),
            "## Current Sprint (2026)")
        # Unparseable stamp degrades to the bare form, never crashes.
        self.assertEqual(
            spaces.daily_section_header("month", "nonsense"),
            "## Current Sprint (nonsense)")


class TestDailyLifecycle(_Harness):
    def test_init_creates_period_journal_and_state_key(self):
        for space, period in (("week", None), ("month", None),
                              ("year", None)):
            with self.subTest(space=space):
                code, out, _err = self._run([space, "init"])
                self.assertEqual(code, 0)
                self.assertIn(f"Initialized daily space '{space}'", out)
                self.assertTrue(
                    (_cfg_dir() / "spaces" / f"{space}.md").is_file())
                self.assertEqual(
                    self._state().get(f"{space}_date"),
                    spaces.current_period(space))
                # Idempotent second init.
                code, out, _err = self._run([space, "init"])
                self.assertEqual(code, 0)
                self.assertIn("Already initialized", out)

    def test_bare_command_is_list_shortcut(self):
        self._run(["week", "init"])
        self._run(["week", "add", "first thing"])
        code, out, _err = self._run(["week"])
        self.assertEqual(code, 0)
        self.assertIn("## Current Sprint (", out)
        self.assertIn("first thing", out)

    def test_actions_before_init_are_a_clean_error(self):
        for space in ("week", "month", "year"):
            with self.subTest(space=space):
                code, out, _err = self._run([space, "list"])
                self.assertEqual(code, 1)
                self.assertIn(f"Daily space '{space}' is not initialized",
                              out)
                self.assertIn(f"ck {space} init", out)

    def test_done_deletes_without_history(self):
        self._run(["week", "init"])
        self._run(["week", "add", "temp task"])
        code, out, _err = self._run(["week", "done", "1"])
        self.assertEqual(code, 0)
        # Journals keep NO history: no <space>.HISTORY.md is created.
        self.assertFalse(
            (_cfg_dir() / "spaces" / "week.HISTORY.md").is_file())
        text = (_cfg_dir() / "spaces" / "week.md").read_text(
            encoding="utf-8")
        self.assertNotIn("temp task", text)

    def test_save_and_log_are_refused(self):
        self._run(["week", "init"])
        for action in ("save", "log"):
            with self.subTest(action=action):
                code, out, _err = self._run(["week", action])
                self.assertEqual(code, 1)
                self.assertIn("not available for daily spaces", out)

    def test_migrate_is_today_only(self):
        self._run(["week", "init"])
        code, out, _err = self._run(["week", "migrate"])
        self.assertEqual(code, 2)
        self.assertIn("LEGACY 'today' space only", out)

    def test_unknown_action_lists_usage(self):
        self._run(["week", "init"])
        code, out, _err = self._run(["week", "bogus"])
        self.assertEqual(code, 2)
        self.assertIn("Unknown `ck week` action", out)

    def test_create_rename_guards(self):
        self._run(["week", "init"])
        code, out, _err = self._run(["space", "create", "week"])
        self.assertEqual(code, 1)
        self.assertIn("'week' is reserved", out)
        code, out, _err = self._run(["space", "rename", "week", "x"])
        self.assertEqual(code, 1)
        self.assertIn("Cannot rename active daily space 'week'", out)
        code, out, _err = self._run(["space", "rename", "x", "week"])
        self.assertEqual(code, 1)
        self.assertIn("Cannot rename to reserved name 'week'", out)
        # The state key is untouched by the refused rename.
        self.assertIn("week_date", self._state())

    def test_delete_clears_period_key(self):
        self._run(["week", "init"])
        self.assertIn("week_date", self._state())
        code, out, _err = self._run(["space", "delete", "week", "-f"])
        self.assertEqual(code, 0)
        self.assertNotIn("week_date", self._state())

    def test_today_flows_still_work(self):
        code, out, _err = self._run(["today", "init"])
        self.assertEqual(code, 0)
        self.assertIn("Initialized daily space 'today'", out)
        code, out, _err = self._run(["today", "add", "daily task"])
        self.assertEqual(code, 0)
        code, out, _err = self._run(["today"])
        self.assertEqual(code, 0)
        self.assertIn("daily task", out)


class TestDailyRollover(_Harness):
    def _seed(self, space: str, *, old_period: str, body: str) -> Path:
        code, _out, _err = self._run([space, "init"])
        self.assertEqual(code, 0)
        self._set_state(**{spaces.daily_date_key(space): old_period})
        path = spaces.space_path(space)
        path.write_text(body, encoding="utf-8")
        return path

    def test_week_rollover_deletes_done_carries_open(self):
        path = self._seed(
            "week", old_period="2026-W01",
            body="## Current Sprint\n"
                 "- [x] finished last week\n"
                 "- [ ] still open\n"
                 "- [>] in progress\n")
        code, out, _err = self._run(["week", "list"])
        self.assertEqual(code, 0)
        self.assertIn("Rolled over 2026-W01 →", out)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("finished last week", text)
        self.assertIn("still open", text)
        self.assertIn("in progress", text)
        self.assertEqual(
            spaces.read_daily_date("week"), spaces.current_period("week"))

    def test_month_and_year_rollover(self):
        for space, old in (("month", "2026-01"), ("year", "2025")):
            with self.subTest(space=space):
                path = self._seed(
                    space, old_period=old,
                    body="## Current Sprint\n"
                         "- [x] stale done\n"
                         "- [ ] fresh open\n")
                result = spaces.maybe_rollover_daily(space)
                self.assertTrue(result["rolled"])
                self.assertEqual(result["old"], old)
                self.assertEqual(result["new"],
                                 spaces.current_period(space))
                self.assertEqual(result["removed"], 1)
                self.assertEqual(result["carried"], 1)
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("stale done", text)
                self.assertIn("fresh open", text)

    def test_notes_follow_reindexed_ids(self):
        path = self._seed(
            "week", old_period="2026-W01",
            body="## Current Sprint\n"
                 "- [x] done one\n"
                 "- [ ] survivor one\n"
                 "- [ ] survivor two\n")
        mgr = spaces.SpaceManager("week")
        mgr.set_note(2, "keep me")
        result = spaces.maybe_rollover_daily("week")
        self.assertTrue(result["rolled"])
        # Survivor one was id 2 -> now id 1: its note must follow.
        notes = mgr._read_state().get("notes", {})
        self.assertIn("keep me", json.dumps(notes))
        self.assertNotIn("done one", path.read_text(encoding="utf-8"))

    def test_same_period_is_a_noop(self):
        self._seed("week", old_period=spaces.current_period("week"),
                   body="## Current Sprint\n- [ ] thing\n")
        result = spaces.maybe_rollover_daily("week")
        self.assertFalse(result["rolled"])

    def test_missing_file_is_recreated(self):
        self._seed("week", old_period=spaces.current_period("week"),
                   body="## Current Sprint\n- [ ] thing\n")
        spaces.space_path("week").unlink()
        code, out, _err = self._run(["week", "list"])
        self.assertEqual(code, 0)
        self.assertIn("week.md was missing, recreated empty", out)

    def test_today_rollover_entry_point_unchanged(self):
        path = self._seed(
            "today", old_period="2026-01-01",
            body="## Current Sprint\n"
                 "- [x] yesterday done\n"
                 "- [ ] today open\n")
        result = spaces.maybe_rollover_today()
        self.assertTrue(result["rolled"])
        self.assertEqual(result["old"], "2026-01-01")
        self.assertEqual(
            result["new"], datetime.now().date().isoformat())
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("yesterday done", text)
        self.assertIn("today open", text)


if __name__ == "__main__":
    unittest.main()
