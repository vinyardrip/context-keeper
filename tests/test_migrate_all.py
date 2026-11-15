"""Batch migrations: ``ck migrate --spaces`` and ``ck migrate --all``.

Covered surface (v0.13.1):

- ``--spaces`` dry-run previews every legacy space and writes NOTHING
  (byte-for-byte snapshot of the config tree);
- ``--spaces --apply`` splits REGULAR spaces: active ``[ ]`` / ``[>]``
  rows stay in ``<name>.md`` with stable UUIDs and inline sidecar
  notes, completed ``[x]`` rows move to ``<name>.HISTORY.md`` as
  timestamped + UUID-stamped ledger rows, the sidecar ``<name>.json``
  is deleted, and the whole spaces directory is backed up to
  ``spaces_backup_<tag>/`` first;
- DAILY spaces (``today``) drop their ``[x]`` rows permanently (the
  journal keeps no history) and keep their active tasks migrated;
- ``<space>.HISTORY.md`` archives are never treated as spaces;
- ``--all`` migrates the spaces AND every project in the global
  registry, reporting ``[ok]`` / ``[!]`` per project, skipping paths
  that vanished from disk, and continuing the batch after a failure;
- dry-run of ``--all`` writes nothing either.

Config isolation follows the suite-wide contract: the global config
directory is pinned into a per-test OS-temp directory (never the real
``~/.config/ck/``).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.cli import build_parser, main
from cklib.migrations.spaces_v0_12_to_v0_13 import SPACES_BACKUP_TAG
from cklib.parser import parse_plan_file

_ENV_KEYS = ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE", "CK_SANDBOX_ROOT")

_REGULAR_SPACE = (
    "# todo\n"
    "\n"
    "## Current Sprint\n"
    "\n"
    "- [ ] active one\n"
    "- [>] focused one\n"
    "\n"
    "## Completed\n"
    "- [x] finished early\n"
    "- [x] shipped yesterday\n"
)
# Legacy sidecar shape: a single STRING per task (pre-0.10 layout).
_REGULAR_SIDECAR = {
    "notes": {
        "1": "scratch for active one",
        "3": "note on finished early",
    },
    "updated_at": "2026-10-21T12:17:34",
}
_DAILY_SPACE = (
    "# today\n"
    "\n"
    "## Current Sprint\n"
    "\n"
    "- [x] old errand\n"
    "- [ ] still open\n"
    "\n"
    "## Completed\n"
    "- [x] done errand\n"
)
_DAILY_SIDECAR = {
    "notes": {"2": "open note"},
    "updated_at": "2026-10-21T12:17:34",
}
_BUILTIN_SPACE = (
    "# local\n"
    "\n"
    "## Current Sprint\n"
    "\n"
    "## Completed\n"
)

_LEGACY_PLAN = (
    "# legacy\n"
    "## Current Sprint\n"
    "- [ ] active one\n"
    "\n"
    "## Completed\n"
    "- [x] finished early\n"
)
_LEGACY_STATE = json.dumps({
    "notes": {
        "1": [{"text": "legacy scratch",
               "created": "2026-01-01T00:00:00+00:00"}],
    },
}, indent=2)


class _IsolatedConfig(unittest.TestCase):
    """Pin the global config (registry + spaces) to a per-test tmp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Path(self._tmp.name) / "ck-config"

        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        for key in ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE"):
            os.environ.pop(key, None)
        os.environ["CK_SANDBOX_ROOT"] = str(
            Path(self._tmp.name) / ".sandbox")
        os.environ["CK_CONFIG_DIR"] = str(self.cfg)

        self._orig = (ckconfig.GLOBAL_CONFIG_DIR,
                      ckconfig.GLOBAL_REGISTRY_FILE,
                      ckconfig.LEGACY_GLOBAL_CONFIG_FILE)
        ckconfig.GLOBAL_CONFIG_DIR = self.cfg
        ckconfig.GLOBAL_REGISTRY_FILE = self.cfg / "projects.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self.cfg.parent / ".ckrc"
        ckregistry.GLOBAL_CONFIG_DIR = ckconfig.GLOBAL_CONFIG_DIR
        ckregistry.GLOBAL_REGISTRY_FILE = ckconfig.GLOBAL_REGISTRY_FILE
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = (
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE)

    def tearDown(self):
        (ckconfig.GLOBAL_CONFIG_DIR,
         ckconfig.GLOBAL_REGISTRY_FILE,
         ckconfig.LEGACY_GLOBAL_CONFIG_FILE) = self._orig
        ckregistry.GLOBAL_CONFIG_DIR = ckconfig.GLOBAL_CONFIG_DIR
        ckregistry.GLOBAL_REGISTRY_FILE = ckconfig.GLOBAL_REGISTRY_FILE
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = (
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ------------------------------------------------------------------ #
    # Fixtures
    # ------------------------------------------------------------------ #

    @property
    def spaces(self) -> Path:
        return self.cfg / "spaces"

    def seed_spaces(self, *, daily: bool = True) -> None:
        """A legacy spaces tree: built-ins pre-created (so the CLI
        startup auto-init writes nothing) + regular and daily spaces
        with their sidecars."""
        self.spaces.mkdir(parents=True)
        (self.spaces / "local.md").write_text(
            _BUILTIN_SPACE, encoding="utf-8")
        (self.spaces / "remote.md").write_text(
            _BUILTIN_SPACE.replace("# local", "# remote"),
            encoding="utf-8")
        (self.spaces / "todo.md").write_text(
            _REGULAR_SPACE, encoding="utf-8")
        (self.spaces / "todo.json").write_text(
            json.dumps(_REGULAR_SIDECAR, indent=2), encoding="utf-8")
        if daily:
            (self.spaces / "today.md").write_text(
                _DAILY_SPACE, encoding="utf-8")
            (self.spaces / "today.json").write_text(
                json.dumps(_DAILY_SIDECAR, indent=2), encoding="utf-8")

    def snapshot(self) -> dict:
        """Every file under the config dir: path -> bytes."""
        out: dict = {}
        for path in sorted(self.cfg.rglob("*")):
            if path.is_file():
                out[str(path.relative_to(self.cfg))] = path.read_bytes()
        return out

    def make_legacy_project(self, name: str, *, plan=_LEGACY_PLAN,
                            state=_LEGACY_STATE) -> Path:
        root = Path(self._tmp.name) / name
        ck = root / ".ck"
        ck.mkdir(parents=True)
        (ck / "PLAN.md").write_text(plan, encoding="utf-8")
        (ck / "state.json").write_text(state, encoding="utf-8")
        return root

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _run_cli(self, argv) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue()

    def backup_dir(self, root: Path) -> Path:
        return root / f".ck_backup_{ckconfig.VERSION}"


# ---------------------------------------------------------------------------
# 1. --spaces: dry run writes nothing
# ---------------------------------------------------------------------------


class TestSpacesDryRun(_IsolatedConfig):
    def test_dry_run_writes_nothing(self):
        self.seed_spaces()
        before = self.snapshot()

        code, out = self._run_cli(["migrate", "--spaces", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("Spaces migration plan", out)
        self.assertIn("todo: move 2 completed task(s)", out)
        self.assertIn("inline 2 note(s)", out)
        self.assertIn(
            "today (daily): delete 2 completed task(s) permanently", out)
        self.assertIn("Dry run: nothing written. Re-run with --apply.", out)
        self.assertIn("local: nothing to migrate", out)
        self.assertEqual(self.snapshot(), before,
                         "dry run must not write anything")
        self.assertFalse(
            (self.cfg / f"spaces_backup_{SPACES_BACKUP_TAG}").exists())

    def test_default_invocation_is_a_dry_run(self):
        self.seed_spaces()
        before = self.snapshot()

        code, out = self._run_cli(["migrate", "--spaces"])

        self.assertEqual(code, 0)
        self.assertIn("Dry run: nothing written", out)
        self.assertEqual(self.snapshot(), before)

    def test_already_migrated_spaces_report_nothing_to_do(self):
        self.seed_spaces()
        self._run_cli(["migrate", "--spaces", "--apply"])
        after = self.snapshot()

        code, out = self._run_cli(["migrate", "--spaces", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("every space is already at v0.13", out)
        self.assertIn("nothing to migrate", out)
        self.assertEqual(self.snapshot(), after)


# ---------------------------------------------------------------------------
# 2. --spaces: regular and daily spaces migrate correctly
# ---------------------------------------------------------------------------


class TestSpacesApply(_IsolatedConfig):
    def setUp(self):
        super().setUp()
        self.seed_spaces()
        self._code, self._out = self._run_cli(
            ["migrate", "--spaces", "--apply"])

    def test_apply_reports_success_and_backup(self):
        self.assertEqual(self._code, 0, self._out)
        self.assertIn("Backup written", self._out)
        self.assertIn("done.", self._out)
        backup = self.cfg / f"spaces_backup_{SPACES_BACKUP_TAG}"
        self.assertTrue(backup.is_dir())
        # The backup captures the ORIGINAL legacy layout.
        self.assertEqual(
            (backup / "todo.md").read_text(encoding="utf-8"),
            _REGULAR_SPACE)
        self.assertEqual(
            json.loads((backup / "todo.json").read_text(encoding="utf-8")),
            _REGULAR_SIDECAR)
        self.assertEqual(
            (backup / "today.md").read_text(encoding="utf-8"),
            _DAILY_SPACE)

    def test_regular_space_keeps_active_tasks_with_uuids_and_notes(self):
        text = (self.spaces / "todo.md").read_text(encoding="utf-8")
        self.assertNotIn("- [x]", text)
        self.assertNotIn("finished early", text)
        self.assertNotIn("## Completed", text)
        self.assertIn("- [ ] active one", text)
        self.assertIn("- [>] focused one", text)
        tl = parse_plan_file(self.spaces / "todo.md")
        for task in tl.tasks:
            self.assertRegex(task.uuid, r"^[0-9a-z]{8}$")
        active = next(t for t in tl.tasks if t.title == "active one")
        self.assertEqual(
            [e["text"] for e in active.notes],
            ["scratch for active one"])

    def test_regular_space_completed_rows_move_to_history(self):
        history = self.spaces / "todo.HISTORY.md"
        self.assertTrue(history.is_file())
        text = history.read_text(encoding="utf-8")
        self.assertIn("## Completed", text)
        self.assertIn("finished early", text)
        self.assertIn("shipped yesterday", text)
        entries = parse_plan_file  # noqa: F841 (readability anchor)
        from cklib.parser import parse_history
        ledger = parse_history(text)
        self.assertEqual(
            [e.title for e in ledger],
            ["finished early", "shipped yesterday"])
        for entry in ledger:
            self.assertRegex(entry.uuid, r"^[0-9a-z]{8}$")
            self.assertRegex(
                entry.timestamp, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        # The completed task's process note graduates into the
        # durable record, inlined beneath its ledger row.
        self.assertIn("* Note", text)
        self.assertIn("note on finished early", text)

    def test_sidecars_are_deleted(self):
        self.assertFalse((self.spaces / "todo.json").exists())
        self.assertFalse((self.spaces / "today.json").exists())
        self.assertEqual(
            [p.name for p in self.spaces.glob("*.json")], [])

    def test_daily_space_drops_completed_rows_without_history(self):
        text = (self.spaces / "today.md").read_text(encoding="utf-8")
        self.assertNotIn("- [x]", text)
        self.assertNotIn("old errand", text)
        self.assertNotIn("done errand", text)
        self.assertNotIn("## Completed", text)
        self.assertIn("- [ ] still open", text)
        self.assertFalse((self.spaces / "today.HISTORY.md").exists())
        tl = parse_plan_file(self.spaces / "today.md")
        self.assertEqual([t.title for t in tl.tasks], ["still open"])
        self.assertRegex(tl.tasks[0].uuid, r"^[0-9a-z]{8}$")
        self.assertEqual(
            [e["text"] for e in tl.tasks[0].notes], ["open note"])

    def test_history_archives_are_never_treated_as_spaces(self):
        archive = self.spaces / "work.HISTORY.md"
        archive.write_text(
            "### 2026-01-01 10:00 | [1] old task\n  * Note: kept\n",
            encoding="utf-8")
        before = archive.read_bytes()

        self._run_cli(["migrate", "--spaces", "--apply"])

        self.assertEqual(archive.read_bytes(), before)
        self.assertFalse((self.spaces / "work.md").exists())

    def test_second_apply_is_a_no_op_without_another_backup(self):
        backup = self.cfg / f"spaces_backup_{SPACES_BACKUP_TAG}"
        self.assertTrue(backup.is_dir())

        code, out = self._run_cli(["migrate", "--spaces", "--apply"])

        self.assertEqual(code, 0)
        self.assertIn("every space is already at v0.13", out)
        self.assertNotIn("Backup written", out)
        # Exactly ONE backup directory (no _2 dedupe copy).
        self.assertEqual(
            [p.name for p in self.cfg.glob("spaces_backup_*")],
            [f"spaces_backup_{SPACES_BACKUP_TAG}"])

    def test_spaces_keep_working_after_migration(self):
        """The migrated space is readable/routable through the CLI."""
        code, out = self._run_cli(["todo", "list"])
        self.assertEqual(code, 0)
        self.assertIn("[ ] 1. active one", out)
        self.assertIn("scratch for active one", out)
        self.assertNotIn("finished early", out)


# ---------------------------------------------------------------------------
# 3. --all: dry run previews the whole batch
# ---------------------------------------------------------------------------


class TestAllDryRun(_IsolatedConfig):
    def setUp(self):
        super().setUp()
        self.seed_spaces()
        self.legacy = self.make_legacy_project("project-legacy")
        self.done = self.make_legacy_project("project-done", plan=(
            "# done\n## Current Sprint\n- [ ] fine\n"))
        (self.done / ".ck" / "state.json").unlink()
        (self.done / ".ck" / "version").write_text(
            f"{ckconfig.VERSION}\n", encoding="utf-8")
        self.gone = self.make_legacy_project("project-gone")
        shutil.rmtree(self.gone)
        for path in (self.legacy, self.done, self.gone):
            ckregistry.register_project(path)
        self.before = self.snapshot()

    def test_dry_run_previews_projects_without_writing(self):
        code, out = self._run_cli(["migrate", "--all", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("Spaces migration plan", out)
        self.assertIn("Dry run: nothing written", out)
        self.assertIn("Registered projects: 3", out)
        self.assertIn(
            "[i] Project project-legacy: legacy layout, "
            "migration planned", out)
        self.assertIn(
            f"[!] Project project-done: already at v{ckconfig.VERSION}",
            out)
        self.assertIn(
            "[!] Project project-gone: path missing, skipped", out)
        self.assertIn(
            "Migration plan for", out)  # per-project detailed preview
        self.assertEqual(self.snapshot(), self.before,
                         "dry run must not write anything")
        self.assertFalse(
            (self.cfg / f"spaces_backup_{SPACES_BACKUP_TAG}").exists())
        self.assertFalse(
            (self.legacy / f".ck_backup_{ckconfig.VERSION}").exists())


# ---------------------------------------------------------------------------
# 4. --all: apply migrates spaces + every registered project
# ---------------------------------------------------------------------------


class TestAllApply(_IsolatedConfig):
    def setUp(self):
        super().setUp()
        self.seed_spaces()
        self.legacy = self.make_legacy_project("project-legacy")
        self.done = self.make_legacy_project("project-done", plan=(
            "# done\n## Current Sprint\n- [ ] fine\n"))
        (self.done / ".ck" / "state.json").unlink()
        (self.done / ".ck" / "version").write_text(
            f"{ckconfig.VERSION}\n", encoding="utf-8")
        self.gone = self.make_legacy_project("project-gone")
        shutil.rmtree(self.gone)
        for path in (self.legacy, self.done, self.gone):
            ckregistry.register_project(path)

    def test_apply_migrates_projects_and_reports_summary(self):
        code, out = self._run_cli(["migrate", "--all", "--apply"])

        self.assertEqual(code, 0, out)
        self.assertIn("[ok] Project project-legacy: migrated", out)
        self.assertIn(
            f"[!] Project project-done: already at v{ckconfig.VERSION}",
            out)
        self.assertIn(
            "[!] Project project-gone: path missing, skipped", out)
        self.assertIn("Backup written", out)

    def test_spaces_are_migrated_first(self):
        self._run_cli(["migrate", "--all", "--apply"])
        self.assertFalse((self.spaces / "todo.json").exists())
        self.assertTrue((self.spaces / "todo.HISTORY.md").is_file())
        self.assertNotIn(
            "- [x]",
            (self.spaces / "todo.md").read_text(encoding="utf-8"))
        self.assertFalse((self.spaces / "today.json").exists())

    def test_legacy_project_gets_the_v013_layout(self):
        self._run_cli(["migrate", "--all", "--apply"])
        ck = self.legacy / ".ck"
        self.assertTrue((ck / "version").is_file())
        self.assertEqual(
            (ck / "version").read_text(encoding="utf-8").strip(),
            ckconfig.VERSION)
        self.assertFalse((ck / "state.json").exists())
        plan = (ck / "PLAN.md").read_text(encoding="utf-8")
        self.assertNotIn("- [x]", plan)
        self.assertIn("- [ ] active one", plan)
        # The active task's legacy note was inlined INTO the plan.
        self.assertIn("legacy scratch", plan)
        history = (ck / "HISTORY.md").read_text(encoding="utf-8")
        self.assertIn("finished early", history)

    def test_backups_are_created_for_spaces_and_projects(self):
        self._run_cli(["migrate", "--all", "--apply"])
        spaces_backup = self.cfg / f"spaces_backup_{SPACES_BACKUP_TAG}"
        self.assertTrue(spaces_backup.is_dir())
        self.assertTrue(
            (spaces_backup / "todo.json").is_file())
        project_backup = self.legacy / f".ck_backup_{ckconfig.VERSION}"
        self.assertTrue(project_backup.is_dir())
        self.assertTrue((project_backup / "state.json").is_file())

    def test_migrated_project_reads_back_clean(self):
        self._run_cli(["migrate", "--all", "--apply"])
        # From OUTSIDE the project directory: registry-routed status.
        code, out = self._run_cli(["st", "-l", "project-legacy"])
        self.assertEqual(code, 0, out)
        self.assertIn("[ ] 1. active one", out)

    def test_registry_survives_the_batch(self):
        self._run_cli(["migrate", "--all", "--apply"])
        names = [p.name for p in ckregistry.list_projects()]
        self.assertEqual(
            names,
            ["project-gone", "project-done", "project-legacy"])


# ---------------------------------------------------------------------------
# 5. --all: failures never abort the batch
# ---------------------------------------------------------------------------


class TestAllFailureIsolation(_IsolatedConfig):
    def setUp(self):
        super().setUp()
        self.seed_spaces()
        # A project whose .ck/ has no PLAN.md: the migration fails for
        # it, the batch must still process the next one.
        self.broken = self.make_legacy_project("project-broken")
        (self.broken / ".ck" / "PLAN.md").unlink()
        self.good = self.make_legacy_project("project-good")
        for path in (self.broken, self.good):
            ckregistry.register_project(path)

    def test_broken_project_fails_without_stopping_the_batch(self):
        code, out = self._run_cli(["migrate", "--all", "--apply"])

        self.assertEqual(code, 1)
        self.assertIn("[!] Project project-broken:", out)
        self.assertIn("[ok] Project project-good: migrated", out)
        # The healthy project was fully migrated anyway.
        self.assertTrue(
            (self.good / ".ck" / "version").is_file())
        self.assertFalse((self.good / ".ck" / "state.json").exists())

    def test_empty_registry_reports_nothing_to_migrate(self):
        import shutil as _shutil
        for path in (self.broken, self.good):
            ckregistry.remove_project(path)
            _shutil.rmtree(path)

        code, out = self._run_cli(["migrate", "--all", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("No registered projects; nothing to migrate.", out)


# ---------------------------------------------------------------------------
# 6. argparse surface accepts the new flags
# ---------------------------------------------------------------------------


class TestArgparseFlags(_IsolatedConfig):
    def test_parser_accepts_spaces_and_all_flags(self):
        parser = build_parser()
        args = parser.parse_args(
            ["migrate", "--spaces", "--all", "--apply", "--dry-run"])
        self.assertTrue(args.spaces)
        self.assertTrue(args.all)
        self.assertTrue(args.apply)
        self.assertTrue(args.dry_run)


if __name__ == "__main__":
    unittest.main()
