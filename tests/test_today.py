"""Tests for the v0.12.0 daily journal space ``today``.

Covered surface:

- ``ck today init``: explicit creation, idempotency, legacy refusal;
- ``ck today migrate [--name <new>]``: legacy space migration;
- reserved-name handling in ``ck space create/rename/delete today``;
- lazy local-midnight rollover: completed tasks are deleted, open and
  focused tasks are carried over, notes follow the re-indexing;
- the bare ``ck today`` shortcut and the dated status header;
- ``save`` / ``log`` refusals for daily spaces.
"""

from __future__ import annotations

# Filesystem isolation safety net (see tests/__init__).
import tests  # noqa: F401

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import spaces
from cklib.cli import main


def _run_main(argv):
    """Run the CLI in-process; return (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), \
            mock.patch("sys.stdin.isatty", return_value=False), \
            mock.patch("sys.stdout.isatty", return_value=False):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


class _TodayHome(unittest.TestCase):
    """Pin the global config + sandbox toggles into a per-test tmp dir."""

    _ENV_KEYS = ("CK_CONFIG_DIR", "CK_SANDBOX", "CK_DEV",
                 "CK_SANDBOX_ACTIVE", "CK_SANDBOX_ROOT")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        config_dir = root / "config"

        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for key in ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                    "CK_SANDBOX_ROOT"):
            os.environ.pop(key, None)
        os.environ["CK_CONFIG_DIR"] = str(config_dir)
        os.environ["CK_SANDBOX_ROOT"] = str(root / ".sandbox")

        self._saved_config = {
            name: getattr(ckconfig, name)
            for name in ("GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                         "GLOBAL_STATE_FILE", "LEGACY_GLOBAL_CONFIG_FILE")
        }
        self._saved_registry = {
            name: getattr(ckregistry, name)
            for name in ("GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                         "LEGACY_GLOBAL_CONFIG_FILE")
        }
        ckconfig.GLOBAL_CONFIG_DIR = config_dir
        ckconfig.GLOBAL_REGISTRY_FILE = config_dir / "projects.json"
        ckconfig.GLOBAL_STATE_FILE = config_dir / "state.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = root / ".ckrc"
        for name in self._saved_registry:
            setattr(ckregistry, name, getattr(ckconfig, name))

    def tearDown(self):
        for name, value in self._saved_config.items():
            setattr(ckconfig, name, value)
        for name, value in self._saved_registry.items():
            setattr(ckregistry, name, value)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---- helpers ------------------------------------------------------ #

    def state_file(self) -> Path:
        return spaces.global_state_path()

    def state(self) -> dict:
        return json.loads(self.state_file().read_text(encoding="utf-8"))

    def write_state(self, payload: dict) -> None:
        self.state_file().parent.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(
            json.dumps(payload), encoding="utf-8")

    def today_path(self) -> Path:
        return spaces.space_path("today")

    def write_legacy_today(self) -> Path:
        path = self.today_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# today\n\n## Current Sprint\n- [ ] legacy task\n\n"
            "## Completed\n",
            encoding="utf-8",
        )
        return path

    def init_today(self) -> str:
        code, out, _ = _run_main(["today", "init"])
        self.assertEqual(code, 0, out)
        return out

    def add(self, *titles: str) -> None:
        for title in titles:
            code, out, _ = _run_main(["today", "add", title])
            self.assertEqual(code, 0, out)

    def set_yesterday(self) -> str:
        state = self.state()
        state["today_date"] = (
            date.today() - timedelta(days=1)).isoformat()
        self.write_state(state)
        return state["today_date"]

    def file_md5(self) -> str:
        return hashlib.md5(
            self.today_path().read_bytes()).hexdigest()


class TestTodayInit(_TodayHome):

    def test_init_creates_file_state_and_order(self):
        today = date.today().isoformat()
        code, out, _ = _run_main(["today", "init"])

        self.assertEqual(code, 0, out)
        self.assertIn(f"Initialized daily space 'today' for {today}.", out)
        self.assertTrue(self.today_path().is_file())
        self.assertEqual(self.state()["today_date"], today)
        self.assertEqual(self.state()["spaces_order"][-1], "today")

    def test_init_appends_today_to_an_existing_order(self):
        spaces.write_space_order(["work", "project"])

        self.init_today()

        self.assertEqual(
            self.state()["spaces_order"], ["work", "project", "today"])

    def test_init_is_idempotent_and_never_rewrites(self):
        self.init_today()
        before = self.file_md5()

        code, out, _ = _run_main(["today", "init"])

        self.assertEqual(code, 0, out)
        self.assertIn("Already initialized for", out)
        self.assertEqual(self.file_md5(), before)

    def test_init_refuses_legacy_today(self):
        self.write_legacy_today()

        code, out, _ = _run_main(["today", "init"])

        self.assertEqual(code, 1, out)
        self.assertIn("[err] Legacy 'today' space detected.", out)
        self.assertIn("ck today migrate", out)
        self.assertIsNone(spaces.read_today_date())

    def test_init_requires_no_arguments(self):
        code, out, _ = _run_main(["today", "init", "extra"])
        self.assertEqual(code, 2, out)
        self.assertIn("takes no arguments", out)


class TestReservedTodayName(_TodayHome):

    def test_create_today_without_legacy_points_to_init(self):
        code, out, _ = _run_main(["space", "create", "today"])

        self.assertEqual(code, 1, out)
        self.assertIn("[err] 'today' is reserved.", out)
        self.assertIn("ck today init", out)
        self.assertFalse(self.today_path().exists())

    def test_create_today_with_legacy_points_to_migrate(self):
        self.write_legacy_today()

        code, out, _ = _run_main(["space", "create", "today"])

        self.assertEqual(code, 1, out)
        self.assertIn("Run `ck today migrate` first.", out)

    def test_create_today_when_active_is_plain_reserved(self):
        self.init_today()

        code, out, _ = _run_main(["space", "create", "today"])

        self.assertEqual(code, 1, out)
        self.assertIn("[err] 'today' is reserved.", out)
        self.assertNotIn("migrate", out)

    def test_rename_legacy_today_is_allowed(self):
        self.write_legacy_today()

        code, out, _ = _run_main(["space", "rename", "today", "backlog"])

        self.assertEqual(code, 0, out)
        self.assertTrue(spaces.space_path("backlog").is_file())
        self.assertFalse(self.today_path().exists())

    def test_rename_active_today_is_refused(self):
        self.init_today()

        code, out, _ = _run_main(["space", "rename", "today", "backlog"])

        self.assertEqual(code, 1, out)
        self.assertIn("Cannot rename active daily space.", out)
        self.assertIn("ck today migrate <new>", out)
        self.assertTrue(self.today_path().is_file())

    def test_rename_into_today_is_refused(self):
        spaces.create_space("work")

        code, out, _ = _run_main(["space", "rename", "work", "today"])

        self.assertEqual(code, 1, out)
        self.assertIn("Cannot rename to reserved name 'today'.", out)
        self.assertTrue(spaces.space_path("work").is_file())
        self.assertFalse(self.today_path().exists())

    def test_delete_active_today_clears_state(self):
        self.init_today()

        code, out, _ = _run_main(["space", "delete", "today", "-f"])

        self.assertEqual(code, 0, out)
        self.assertIn("Deleted space 'today'", out)
        self.assertFalse(self.today_path().exists())
        self.assertNotIn("today_date", self.state())

    def test_delete_uninitialized_today_is_not_found(self):
        code, out, _ = _run_main(["space", "delete", "today", "-f"])

        self.assertEqual(code, 1, out)
        self.assertIn("[err] No space 'today'.", out)


class TestTodayMigrate(_TodayHome):

    def test_migrate_defaults_to_todo(self):
        self.write_legacy_today()

        code, out, _ = _run_main(["today", "migrate"])

        self.assertEqual(code, 0, out)
        self.assertIn("Renaming legacy 'today' \u2192 'todo'.", out)
        self.assertIn("Run `ck today init`", out)
        self.assertFalse(self.today_path().exists())
        self.assertTrue(spaces.space_path("todo").is_file())
        self.assertIn("legacy task",
                      spaces.space_path("todo").read_text(encoding="utf-8"))

    def test_migrate_accepts_custom_name(self):
        self.write_legacy_today()

        code, out, _ = _run_main(
            ["today", "migrate", "--name", "backlog"])

        self.assertEqual(code, 0, out)
        self.assertTrue(spaces.space_path("backlog").is_file())
        self.assertFalse(spaces.space_path("todo").exists())

    def test_migrate_without_legacy_is_an_error(self):
        code, out, _ = _run_main(["today", "migrate"])

        self.assertEqual(code, 1, out)
        self.assertIn("[err] No legacy 'today' space to migrate.", out)

    def test_migrate_active_daily_is_an_error(self):
        self.init_today()

        code, out, _ = _run_main(["today", "migrate"])

        self.assertEqual(code, 1, out)
        self.assertIn(
            "[err] 'today' is already a daily journal. Nothing to migrate.",
            out)

    def test_migrate_refuses_taken_target(self):
        self.write_legacy_today()
        spaces.create_space("todo")

        code, out, _ = _run_main(["today", "migrate"])

        self.assertEqual(code, 1, out)
        self.assertIn("[err] Space 'todo' already exists.", out)


class TestTodayRollover(_TodayHome):

    def test_rollover_drops_completed_and_carries_open(self):
        self.init_today()
        self.add("one", "two", "three")
        code, _, _ = _run_main(["today", "done", "2"])
        self.assertEqual(code, 0)
        yesterday = self.set_yesterday()

        code, out, _ = _run_main(["today", "st"])

        self.assertEqual(code, 0, out)
        self.assertIn(
            f"Rolled over {yesterday} \u2192 {date.today().isoformat()}: "
            "removed 1 completed, carried over 2.", out)
        self.assertEqual(self.state()["today_date"],
                         date.today().isoformat())
        parsed = spaces.SpaceManager("today").load()
        self.assertEqual(parsed.total, 2)
        self.assertEqual([t.title for t in parsed.tasks], ["one", "three"])
        self.assertEqual([t.status.value for t in parsed.tasks],
                         ["open", "open"])

    def test_rollover_is_idempotent_within_the_same_day(self):
        self.init_today()
        self.add("one", "two")
        _run_main(["today", "done", "1"])
        self.set_yesterday()
        _run_main(["today", "st"])

        code, out, _ = _run_main(["today", "st"])

        self.assertEqual(code, 0, out)
        self.assertNotIn("Rolled over", out)

    def test_rollover_clears_focus_on_completed_task(self):
        self.init_today()
        self.add("one", "two", "three")
        _run_main(["today", "focus", "2"])
        _run_main(["today", "done", "2"])
        self.set_yesterday()

        _run_main(["today", "st"])

        parsed = spaces.SpaceManager("today").load()
        self.assertEqual(parsed.focused, [])
        self.assertEqual([t.title for t in parsed.tasks], ["one", "three"])

    def test_rollover_preserves_focus_on_open_task(self):
        self.init_today()
        self.add("one", "two")
        _run_main(["today", "focus", "2"])
        self.set_yesterday()

        _run_main(["today", "st"])

        parsed = spaces.SpaceManager("today").load()
        self.assertEqual(len(parsed.focused), 1)
        self.assertEqual(parsed.focused[0].title, "two")

    def test_rollover_drops_notes_of_deleted_task_and_remaps_others(self):
        self.init_today()
        self.add("done task", "kept task")
        _run_main(["today", "note", "2", "keep me"])
        _run_main(["today", "note", "1", "drop me"])
        _run_main(["today", "done", "1"])
        self.set_yesterday()

        _run_main(["today", "st"])

        state = json.loads(spaces.state_path("today").read_text(
            encoding="utf-8"))
        self.assertEqual(list(state["notes"]), ["1"])
        self.assertEqual(state["notes"]["1"][0]["text"], "keep me")

    def test_rollover_never_creates_history(self):
        self.init_today()
        self.add("finished", "carried")
        _run_main(["today", "done", "1"])
        self.set_yesterday()

        _run_main(["today", "st"])

        self.assertFalse(spaces.history_path("today").exists())

    def test_rollover_recreates_missing_file(self):
        self.init_today()
        self.today_path().unlink()
        self.set_yesterday()

        code, out, _ = _run_main(["today", "list"])

        self.assertEqual(code, 0, out)
        self.assertIn("[!] today.md was missing, recreated empty.", out)
        self.assertIn("Rolled over", out)
        self.assertTrue(self.today_path().is_file())

    def test_rollover_fires_before_any_task_command(self):
        self.init_today()
        self.add("finished")
        _run_main(["today", "done", "1"])
        self.set_yesterday()

        code, out, _ = _run_main(["today", "add", "new day"])

        self.assertEqual(code, 0, out)
        self.assertIn("Rolled over", out)
        parsed = spaces.SpaceManager("today").load()
        self.assertEqual([t.title for t in parsed.tasks], ["new day"])


class TestTodaySurface(_TodayHome):

    def test_save_is_refused(self):
        self.init_today()

        code, out, _ = _run_main(["today", "save"])

        self.assertEqual(code, 1, out)
        self.assertIn(
            "[err] 'save' is not available for daily spaces.", out)

    def test_log_is_refused(self):
        self.init_today()

        code, out, _ = _run_main(["today", "log"])

        self.assertEqual(code, 1, out)
        self.assertIn(
            "[err] 'log' is not available for daily spaces.", out)

    def test_bare_today_is_the_status_shortcut(self):
        self.init_today()
        self.add("first task")

        code_bare, out_bare, _ = _run_main(["today"])
        code_st, out_st, _ = _run_main(["today", "st"])

        self.assertEqual(code_bare, 0, out_bare)
        self.assertEqual(code_st, 0, out_st)
        self.assertEqual(out_bare, out_st)

    def test_status_header_carries_date_and_weekday(self):
        self.init_today()

        code, out, _ = _run_main(["today", "st"])

        self.assertEqual(code, 0, out)
        first = out.splitlines()[0]
        today = self.state()["today_date"]
        self.assertTrue(first.startswith(
            f"## Current Sprint ({today}, "), first)
        self.assertTrue(first.endswith(")"), first)
        weekday = first[len(f"## Current Sprint ({today}, "):-1]
        self.assertTrue(weekday.strip())

    def test_status_file_keeps_plain_header(self):
        self.init_today()
        _run_main(["today", "st"])

        text = self.today_path().read_text(encoding="utf-8")
        self.assertIn("## Current Sprint\n", text)
        self.assertNotIn("## Current Sprint (", text)

    def test_uninitialized_today_asks_for_init(self):
        code, out, _ = _run_main(["today", "st"])

        self.assertEqual(code, 1, out)
        self.assertIn("Daily space 'today' is not initialized.", out)
        self.assertIn("ck today init", out)


if __name__ == "__main__":
    unittest.main()
