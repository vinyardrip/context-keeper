"""v0.13.0 split-layout spec tests (PLAN/HISTORY split, UUIDs, focus).

Pins the core behaviors of the MAJOR refactor in one place:

- UUID stamps: every task carries a stable ``<!-- id: uuid -->``
  comment (``add`` generates, re-index never regenerates, ``done``
  graduates the UUID into HISTORY.md, no UUID ever leaks into a
  rendered title);
- PLAN/HISTORY split: ``ck done`` REMOVES the task from PLAN.md and
  appends a timestamped ``- [x] <ts>: <title>`` ledger entry to
  HISTORY.md (with its notes); the remaining IDs re-index;
- start/focus split: ``ck start`` REFUSES to steal an existing focus
  (exit 1 + ``ck focus`` redirect); ``ck focus <NEW>`` parks the old
  task as ``[~]`` (notes preserved); ``start 0``/``focus 0`` reset to
  OPEN (never paused);
- delete re-index: the confirmation prints the REFRESHED listing so
  the next ``ck done N`` always points at what the user just saw;
- daily journal: ``ck today done`` DELETES permanently (no
  HISTORY.md exists for the journal);
- migration: ``ck migrate`` defaults to a dry-run preview (nothing
  written), ``--apply`` backs the whole ``.ck/`` up to
  ``.ck_backup_<version>/`` then splits the layout (HISTORY.md
  ledger, inline notes, UUIDs, metadata set, state.json deleted);
  write commands on an unmigrated project refuse with the
  ``ck migrate`` hint.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import ui as ckui
from cklib.cli import main
from cklib.core import ContextKeeper, FocusExistsError
from cklib.models import TaskStatus
from cklib.parser import parse_plan_file

_VERSION_RE = re.compile(r"<!-- id: ([0-9a-z]+) -->")


def _uuids(text: str) -> list[str]:
    return _VERSION_RE.findall(text)


class _IsolatedHome:
    """Pin the global registry to a per-test tmp HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
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

    def _cleanup(self):
        self._tmp.cleanup()
        ckconfig.GLOBAL_CONFIG_DIR = self._orig_dir
        ckconfig.GLOBAL_REGISTRY_FILE = self._orig_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
        ckregistry.GLOBAL_CONFIG_DIR = self._orig_dir
        ckregistry.GLOBAL_REGISTRY_FILE = self._orig_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy


def _clean_env(**overrides):
    """Env patcher: CK color/sandbox toggles removed by default."""
    base = {
        k: v for k, v in os.environ.items()
        if k not in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE",
                     "CK_SANDBOX", "CK_DEV")
    }
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=True)


class _SplitHarness(_IsolatedHome, unittest.TestCase):
    """Shared project scaffold."""

    def _ck(self, plan: str | None = None) -> ContextKeeper:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name) / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()
        if plan is not None:
            ck.plan_file.write_text(plan, encoding="utf-8")
        return ck

    def _run_cli(self, argv, cwd: Path, *, expected_code=0) -> str:
        """Run the CLI bound to ``cwd``; ANSI-stripped stdout."""
        buf = io.StringIO()
        orig_cwd = Path.cwd()
        os.chdir(cwd)
        try:
            with _clean_env(), redirect_stdout(buf):
                code = main(argv)
            self.assertEqual(code, expected_code,
                             f"exit {code} for {argv!r}")
        finally:
            os.chdir(orig_cwd)
        return ckui.strip_ansi(buf.getvalue())


# ---------------------------------------------------------------------------
# 1. UUID stamps
# ---------------------------------------------------------------------------


class TestUuidStamps(_SplitHarness):
    def test_add_writes_uuid_comment(self):
        ck = self._ck()
        new_id = ck.add_task("wire up the parser")
        text = ck.plan_file.read_text(encoding="utf-8")
        stamp = _VERSION_RE.search(text)
        self.assertIsNotNone(stamp)
        self.assertTrue(text.startswith("# ") or "- [ ] wire" in text)
        self.assertRegex(stamp.group(1), r"^[0-9a-z]{8}$")
        # The task line carries the comment: `- [ ] title <!-- id: x -->`
        self.assertRegex(
            text, r"- \[ \] wire up the parser <!-- id: [0-9a-z]{8} -->")
        self.assertEqual(new_id, 1)

    def test_two_tasks_never_share_a_uuid(self):
        ck = self._ck("# P\n## Current Sprint\n")
        ck.add_task("first")
        ck.add_task("second")
        stamps = _uuids(ck.plan_file.read_text(encoding="utf-8"))
        self.assertEqual(len(stamps), 2)
        self.assertNotEqual(stamps[0], stamps[1])

    def test_reindex_never_regenerates_uuid(self):
        """Deleting a neighbor keeps the survivor's stable reference."""
        ck = self._ck()
        ck.add_task("keep me")
        ck.add_task("drop me")
        ck.add_task("tail")
        keep_uuid = _uuids(ck.plan_file.read_text(
            encoding="utf-8"))[0]

        ck.delete_task(2)  # drop me — survivors re-index: keep me -> 1

        text = ck.plan_file.read_text(encoding="utf-8")
        self.assertIn("- [ ] keep me", text)
        self.assertIn(keep_uuid, text)
        self.assertNotIn("drop me", text)
        # The survivor is now id 1 but keeps its original UUID.
        tl = parse_plan_file(ck.plan_file)
        self.assertEqual(tl.by_id(1).uuid, keep_uuid)

    def test_done_graduates_uuid_into_history(self):
        ck = self._ck()
        ck.add_task("graduate me")
        task_uuid = parse_plan_file(
            ck.plan_file).by_id(1).uuid

        ck.done("1")

        history = ck.history_file.read_text(encoding="utf-8")
        self.assertRegex(
            history,
            r"- \[x\] \d{4}-\d{2}-\d{2} \d{2}:\d{2}: graduate me"
            rf" <!-- id: {task_uuid} -->",
        )

    def test_uuid_never_leaks_into_rendered_views(self):
        ck = self._ck()
        ck.add_task("displayed task")
        for view in (ck.status(), ck.tasks()):
            self.assertNotRegex(view, r"[0-9a-z]{8}\b.*id:")
            self.assertNotIn("<!--", view)


# ---------------------------------------------------------------------------
# 2. done -> HISTORY.md (plan active-only)
# ---------------------------------------------------------------------------


class TestDoneToHistory(_SplitHarness):
    PLAN = (
        "# P\n"
        "## Current Sprint\n"
        "- [ ] alpha\n"
        "- [>] beta\n"
        "- [ ] gamma\n"
    )

    def test_done_removes_task_from_plan_and_appends_history(self):
        ck = self._ck(self.PLAN)
        result = ck.done("2")

        self.assertEqual(result, [2])
        plan = ck.plan_file.read_text(encoding="utf-8")
        self.assertNotIn("beta", plan)
        self.assertNotIn("[x]", plan)  # the plan holds ACTIVE work only
        history = ck.history_file.read_text(encoding="utf-8")
        self.assertRegex(
            history, r"- \[x\] \d{4}-\d{2}-\d{2} \d{2}:\d{2}: beta\n")
        # IDs re-indexed: gamma is now id 2.
        self.assertEqual(
            [t.title for t in parse_plan_file(ck.plan_file).tasks],
            ["alpha", "gamma"])

    def test_done_carries_notes_into_history(self):
        ck = self._ck(self.PLAN)
        ck.set_note("mid-refactor")  # binds to the focused beta
        ck.done("2")
        history = ck.history_file.read_text(encoding="utf-8")
        self.assertRegex(
            history,
            r"\* Note \(\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\): mid-refactor")
        self.assertNotIn("* Note",
                         ck.plan_file.read_text(encoding="utf-8"))

    def test_done_reindexes_before_following_spec(self):
        """IDs shift after EVERY done: a follow-up spec must target
        the re-indexed positions (single call per logical step)."""
        ck = self._ck()
        ck.add_task("one")
        ck.add_task("two")
        ck.add_task("three")
        ck.done("2")  # "two" graduates; three re-indexes to 2
        self.assertEqual([t.title for t in parse_plan_file(
            ck.plan_file).tasks], ["one", "three"])
        ck.done("2")  # now points at "three"
        self.assertEqual([t.title for t in parse_plan_file(
            ck.plan_file).tasks], ["one"])

    def test_done_cli_output_and_history_listing(self):
        ck = self._ck(self.PLAN)
        out = self._run_cli(["done", "2"], ck.root)
        self.assertIn("[ok] Marked done: 2", out)
        # The history ledger is queryable.
        titles = [e.title for e in ck.history_entries()]
        self.assertEqual(titles, ["beta"])

    def test_history_entry_format_matches_spec(self):
        """`- [x] <ts>: <title> <!-- id: uuid -->` — exact shape."""
        ck = self._ck()
        ck.add_task("single ledger row")
        task_uuid = parse_plan_file(ck.plan_file).by_id(1).uuid
        ck.done("1")
        history = ck.history_file.read_text(encoding="utf-8")
        self.assertRegex(
            history,
            r"## Completed\n"
            r"- \[x\] \d{4}-\d{2}-\d{2} \d{2}:\d{2}: single ledger row"
            rf" <!-- id: {task_uuid} -->\n",
        )


# ---------------------------------------------------------------------------
# 3. start / focus split
# ---------------------------------------------------------------------------


class TestFocusSplit(_SplitHarness):
    PLAN = (
        "# P\n"
        "## Current Sprint\n"
        "- [>] first\n"
        "- [ ] second\n"
        "- [ ] third\n"
    )

    def test_start_refuses_to_steal_existing_focus(self):
        ck = self._ck(self.PLAN)
        with self.assertRaises(FocusExistsError) as ctx:
            ck.start(3)
        self.assertEqual(ctx.exception.task_id, 1)
        # Total refusal: task 3 was never focused.
        tl = parse_plan_file(ck.plan_file)
        self.assertEqual(tl.by_id(3).status, TaskStatus.OPEN)
        self.assertEqual(tl.by_id(1).status, TaskStatus.FOCUSED)

    def test_start_refusal_cli_exit_1_with_redirect(self):
        ck = self._ck(self.PLAN)
        out = self._run_cli(["start", "3"], ck.root, expected_code=1)
        self.assertIn("ERROR: Task #1 is already focused.", out)
        self.assertIn("Use: ck focus 3 to pause it and switch.", out)

    def test_focus_parks_old_task_as_paused(self):
        ck = self._ck(self.PLAN)
        result = ck.focus(3)

        self.assertEqual(result.task_id, 3)
        self.assertEqual(result.demoted_id, 1)
        tl = parse_plan_file(ck.plan_file)
        self.assertEqual(tl.by_id(1).status, TaskStatus.PAUSED)
        self.assertEqual(tl.by_id(3).status, TaskStatus.FOCUSED)
        # The parked task renders in the Paused block.
        out = self._run_cli(["st"], ck.root)
        self.assertIn("    [~] Paused:", out)
        self.assertIn("- [1] first", out)

    def test_focus_preserves_the_parked_task_note(self):
        ck = self._ck(self.PLAN)
        ck.set_note("half done")  # binds to the focused task 1
        result = ck.focus(3)

        self.assertTrue(result.had_note)
        tl = parse_plan_file(ck.plan_file)
        self.assertEqual(tl.by_id(1).notes[-1]["text"], "half done")
        self.assertEqual(tl.by_id(1).status, TaskStatus.PAUSED)

    def test_start_zero_resets_to_open_not_paused(self):
        ck = self._ck(self.PLAN)
        ck.set_note("wip")
        result = ck.start(0)

        self.assertEqual(result.task_id, 0)
        self.assertEqual(result.demoted_id, 1)
        tl = parse_plan_file(ck.plan_file)
        # RESET returns the task to OPEN — never parked as [~].
        self.assertEqual(tl.by_id(1).status, TaskStatus.OPEN)
        self.assertEqual(tl.focused, [])
        self.assertEqual(tl.by_id(1).notes[-1]["text"], "wip")

    def test_focus_zero_resets_the_same_way(self):
        ck = self._ck(self.PLAN)
        ck.focus(0)
        tl = parse_plan_file(ck.plan_file)
        self.assertEqual(tl.by_id(1).status, TaskStatus.OPEN)

    def test_start_resumes_a_paused_task(self):
        """Resume works only when NO focus is held; with a focus,
        ``start`` refuses outright (use ``ck focus`` to switch)."""
        ck = self._ck(self.PLAN)
        ck.focus(2)   # first parked as [~], second focused
        ck.focus(0)   # release focus: second resets to [ ]
        self.assertEqual(parse_plan_file(
            ck.plan_file).by_id(1).status, TaskStatus.PAUSED)

        ck.start(1)   # resume the paused task (no focus held now)

        tl = parse_plan_file(ck.plan_file)
        self.assertEqual(tl.by_id(1).status, TaskStatus.FOCUSED)
        self.assertEqual(tl.by_id(2).status, TaskStatus.OPEN)

    def test_start_refuses_resume_while_focus_is_held(self):
        """A paused target is NOT special-cased: any existing focus
        makes ``start`` refuse before the resume logic runs."""
        ck = self._ck(self.PLAN)
        ck.focus(2)   # first parked as [~], second holds focus
        with self.assertRaises(FocusExistsError) as ctx:
            ck.start(1)   # resume attempt while focus is held
        self.assertEqual(ctx.exception.task_id, 2)
        tl = parse_plan_file(ck.plan_file)
        self.assertEqual(tl.by_id(1).status, TaskStatus.PAUSED)
        self.assertEqual(tl.by_id(2).status, TaskStatus.FOCUSED)

    def test_parked_state_is_a_real_plan_marker(self):
        """``[~]`` is written to PLAN.md, not a virtual view state."""
        ck = self._ck(self.PLAN)
        ck.focus(3)
        self.assertRegex(
            ck.plan_file.read_text(encoding="utf-8"),
            r"- \[~\] first\b")


# ---------------------------------------------------------------------------
# 4. delete re-index output
# ---------------------------------------------------------------------------


class TestDeleteReindex(_SplitHarness):
    def test_delete_prints_refreshed_listing(self):
        ck = self._ck()
        for title in ("alpha", "beta", "gamma"):
            ck.add_task(title)

        out = self._run_cli(["delete", "2", "-f"], ck.root)

        self.assertIn("Deleted task #2: beta", out)
        self.assertIn("2 task(s) remaining, IDs re-indexed", out)
        # v0.13.0: the refreshed listing follows the confirmation, so
        # the next `ck done N` points at what the user just saw.
        self.assertIn("[ ] 1. alpha", out)
        self.assertIn("[ ] 2. gamma", out)
        self.assertNotIn("beta", out.split("re-indexed", 1)[1])

    def test_delete_really_reindexes(self):
        ck = self._ck()
        for title in ("alpha", "beta", "gamma"):
            ck.add_task(title)
        ck.delete_task(2)
        self.assertEqual(
            [t.title for t in parse_plan_file(ck.plan_file).tasks],
            ["alpha", "gamma"])

    def test_delete_archives_notes_into_history(self):
        ck = self._ck()
        ck.add_task("noted")
        ck.add_task("other")
        ck.set_note("do not lose me", task_id=1)

        ck.delete_task(1)

        history = ck.history_file.read_text(encoding="utf-8")
        self.assertIn("do not lose me", history)


# ---------------------------------------------------------------------------
# 5. daily journal: done is permanent
# ---------------------------------------------------------------------------


class TestTodayDonePermanent(_SplitHarness):
    def test_today_done_deletes_without_history(self):
        from cklib import spaces

        ck = self._ck()
        out = self._run_cli(["today", "init"], ck.root)
        self.assertIn("initialized", out.lower())
        out = self._run_cli(["today", "add", "errand"], ck.root)
        self.assertIn("Added task to today space (id=1): errand", out)

        out = self._run_cli(["today", "done", "1"], ck.root)
        self.assertIn("Marked done: 1", out)

        today_text = spaces.space_path("today").read_text(
            encoding="utf-8")
        self.assertNotIn("errand", today_text)
        # The journal has NO history ledger: nothing was written.
        self.assertFalse(
            spaces.space_path("today.HISTORY").exists())
        self.assertFalse((ck.ck_path / "HISTORY.md").exists())


# ---------------------------------------------------------------------------
# 6. migrate: legacy project -> v0.13.0 split layout
# ---------------------------------------------------------------------------


_LEGACY_PLAN = (
    "# legacy\n"
    "## Current Sprint\n"
    "- [ ] active one\n"
    "- [>] focused one\n"
    "\n"
    "## Completed\n"
    "- [x] finished early\n"
    "- [x] shipped yesterday\n"
)
_LEGACY_STATE = json.dumps({
    "notes": {
        "1": [{"text": "legacy scratch",
               "created": "2026-01-01T00:00:00+00:00"}],
    },
}, indent=2)


class _LegacyProject(_SplitHarness):
    """A pre-0.13 ``.ck/`` project (PLAN with [x] rows + state.json)."""

    def _legacy_ck(self) -> ContextKeeper:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name) / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True)
        ck.plan_file.write_text(_LEGACY_PLAN, encoding="utf-8")
        (ck.ck_path / "state.json").write_text(
            _LEGACY_STATE, encoding="utf-8")
        return ck


class TestMigrateDryRun(_LegacyProject):
    def test_default_is_dry_run_nothing_written(self):
        ck = self._legacy_ck()
        before = {
            p.name: p.read_bytes()
            for p in ck.ck_path.iterdir() if p.is_file()
        }

        out = self._run_cli(["migrate"], ck.root)

        self.assertIn("Migration plan", out)
        self.assertIn("move 2 completed task(s)", out)
        self.assertIn("keep 2 active task(s)", out)
        self.assertIn("inline 1 legacy process note(s)", out)
        self.assertIn("Dry run: nothing written. Re-run with --apply.",
                      out)
        # Nothing changed on disk: same files, same bytes.
        after = {
            p.name: p.read_bytes()
            for p in ck.ck_path.iterdir() if p.is_file()
        }
        self.assertEqual(after, before)
        self.assertFalse((ck.ck_path / "version").exists())
        self.assertFalse((ck.ck_path / "HISTORY.md").exists())

    def test_apply_flag_switches_to_real_migration(self):
        ck = self._legacy_ck()
        out = self._run_cli(["migrate", "--apply"], ck.root)
        self.assertIn("Backup written", out)
        self.assertIn("state.json", out)
        self.assertTrue((ck.ck_path / "version").exists())


class TestMigrateApply(_LegacyProject):
    def _apply(self) -> ContextKeeper:
        ck = self._legacy_ck()
        out = self._run_cli(["migrate", "--apply"], ck.root)
        self.assertNotIn("ERROR", out)
        return ck

    def test_backup_dir_captures_the_original_layout(self):
        ck = self._apply()
        backup = ck.ck_path.parent / f".ck_backup_{ckconfig.VERSION}"
        self.assertTrue(backup.is_dir())
        self.assertIn(
            "finished early",
            (backup / "PLAN.md").read_text(encoding="utf-8"))
        self.assertIn(
            "legacy scratch",
            (backup / "state.json").read_text(encoding="utf-8"))

    def test_plan_holds_active_tasks_only_with_uuids(self):
        ck = self._apply()
        plan = ck.plan_file.read_text(encoding="utf-8")
        self.assertNotIn("- [x]", plan)
        self.assertNotIn("finished early", plan)
        self.assertIn("- [ ] active one", plan)
        self.assertIn("- [>] focused one", plan)
        # Every surviving task carries a stable UUID stamp.
        tl = parse_plan_file(ck.plan_file)
        for task in tl.tasks:
            self.assertRegex(task.uuid, r"^[0-9a-z]{8}$")
        # The legacy note travels INLINE under its task (the UUID
        # comment sits on the task line itself).
        self.assertRegex(
            plan, r"- \[ \] active one <!-- id: [0-9a-z]{8} -->\n"
                  r"  \* Note \(2026-01-01 00:00 UTC\): legacy scratch\n")

    def test_history_carries_the_completed_rows(self):
        ck = self._apply()
        history = ck.history_file.read_text(encoding="utf-8")
        self.assertRegex(
            history, r"- \[x\] \d{4}-\d{2}-\d{2} \d{2}:\d{2}: "
            r"finished early <!-- id: [0-9a-z]{8} -->")
        self.assertRegex(
            history, r"- \[x\] \d{4}-\d{2}-\d{2} \d{2}:\d{2}: "
            r"shipped yesterday <!-- id: [0-9a-z]{8} -->")

    def test_state_json_deleted_and_metadata_created(self):
        ck = self._apply()
        self.assertFalse((ck.ck_path / "state.json").exists())
        for name in ("JOURNAL.md", "version", "meta.json",
                     "links.json", "AGENTS.md"):
            self.assertTrue((ck.ck_path / name).is_file(), name)

    def test_migrated_project_passes_the_write_gate(self):
        """After --apply write commands work without any gate error."""
        ck = self._apply()
        out = self._run_cli(["add", "post-migration task"], ck.root)
        self.assertIn("Added task (id=3)", out)

    def test_second_migrate_run_is_idempotent(self):
        ck = self._apply()
        out = self._run_cli(["migrate"], ck.root)
        self.assertIn(
            f"already at v{ckconfig.VERSION}", out)
        self.assertIn("nothing to migrate", out)

    def test_write_gate_refuses_unmigrated_project(self):
        ck = self._legacy_ck()
        out = self._run_cli(["add", "blocked"], ck.root,
                            expected_code=2)
        self.assertIn("pre-0.13 schema", out)
        self.assertIn("Run `ck migrate` to upgrade it, then retry", out)


if __name__ == "__main__":
    unittest.main()
