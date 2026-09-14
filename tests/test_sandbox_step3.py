"""Tests for Step 3: core write interception integration.

Covers:

- CLI commands run in dev mode (``CK_SANDBOX=1``, ``ck-dev``) write
  ONLY to ``.sandbox/`` — real project files, ``.ck/`` trees, the
  registry, state.json, lock files, backups stay untouched
  (content AND mtime).
- Reads still come from the real state (dashboard / status reflect
  production; sandbox appends seed from the real history).
- ``stdout`` stays clean of debug logs (they go to stderr/dev.log
  only, and only when CK_DEBUG=1 / -v).
- The ``ck-dev`` wrapper sets CK_SANDBOX=1 and delegates.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import sandbox
from cklib.cli import main
from cklib.core import ContextKeeper
from cklib.sandbox import sandbox_root


REPO_ROOT = Path(__file__).resolve().parent.parent


class _DevModeBase(unittest.TestCase):
    """Deterministic dev-mode environment.

    - Fresh ``.sandbox/`` per test (removed before AND after).
    - CK_* toggles popped; ``sys.argv`` pinned so pytest flags leak
      nowhere.
    - Global registry/config redirected into a per-test temp HOME.
    """

    def setUp(self):
        sandbox.clean_sandbox(quiet=True)
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in ("CK_SANDBOX", "CK_DEV", "CK_DEBUG")
        }
        self._saved_argv = sandbox.sys.argv
        sandbox.sys.argv = ["ck"]

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._orig_dir = ckconfig.GLOBAL_CONFIG_DIR
        self._orig_file = ckconfig.GLOBAL_REGISTRY_FILE
        self._orig_state = ckconfig.GLOBAL_STATE_FILE
        self._orig_legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE

        new_dir = fake_home / ".config" / "ck"
        ckconfig.GLOBAL_CONFIG_DIR = new_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
        ckconfig.GLOBAL_STATE_FILE = new_dir / "state.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"
        ckregistry.GLOBAL_CONFIG_DIR = new_dir
        ckregistry.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
        ckregistry.GLOBAL_STATE_FILE = new_dir / "state.json"
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"

        def _restore_config():
            ckconfig.GLOBAL_CONFIG_DIR = self._orig_dir
            ckconfig.GLOBAL_REGISTRY_FILE = self._orig_file
            ckconfig.GLOBAL_STATE_FILE = self._orig_state
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
            ckregistry.GLOBAL_CONFIG_DIR = self._orig_dir
            ckregistry.GLOBAL_REGISTRY_FILE = self._orig_file
            ckregistry.GLOBAL_STATE_FILE = self._orig_state
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
        self.addCleanup(_restore_config)

    def tearDown(self):
        sandbox.clean_sandbox(quiet=True)
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        sandbox.sys.argv = self._saved_argv


class _SnapshottedProject(_DevModeBase):
    """Adds a real on-disk project with mtime snapshots."""

    def make_project(self, name: str = "proj") -> tuple:
        root = Path(self._tmp.name) / name
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init(register=True)
        ck.add_task("alpha task")
        ck.add_task("beta task")
        return root, ck

    def snapshot_tree(self, root: Path) -> dict:
        """content + mtime_ns for every file under root (recursive)."""
        snap = {}
        for p in sorted(root.rglob("*")):
            if p.is_file():
                st = p.stat()
                try:
                    content = p.read_bytes()
                except OSError:
                    content = b"<unreadable>"
                snap[str(p.relative_to(root))] = (content, st.st_mtime_ns)
        return snap


class TestAddTaskInterception(_SnapshottedProject):
    """`ck add` in dev mode never touches the real project."""

    def test_add_writes_only_to_sandbox(self):
        root, ck = self.make_project()
        before = self.snapshot_tree(root)

        os.environ["CK_SANDBOX"] = "1"
        new_id = ck.add_task("dev-mode task")
        self.assertEqual(new_id, 4)  # init=1, alpha=2, beta=3

        # Real project: byte-identical, mtimes unchanged.
        after = self.snapshot_tree(root)
        self.assertEqual(after, before)

        # The task landed in the sandboxed PLAN.md.
        from cklib.sandbox import resolve_write_path
        sandboxed = resolve_write_path(ck.plan_file)
        self.assertTrue(sandboxed.exists())
        self.assertIn("dev-mode task",
                      sandboxed.read_text(encoding="utf-8"))

    def test_start_and_done_intercepted(self):
        root, ck = self.make_project()
        before = self.snapshot_tree(root)

        os.environ["CK_SANDBOX"] = "1"
        ck.start(2)   # alpha task (init=1, alpha=2, beta=3)
        ck.done("1")  # the default init task

        self.assertEqual(self.snapshot_tree(root), before)
        from cklib.sandbox import resolve_write_path
        sandboxed = resolve_write_path(ck.plan_file)
        self.assertIn("- [>] alpha task",
                      sandboxed.read_text(encoding="utf-8"))
        self.assertIn("- [x] Описать первую задачу",
                      sandboxed.read_text(encoding="utf-8"))
        # Read-your-writes: BOTH mutations composed in one copy.
        self.assertIn("- [ ] beta task",
                      sandboxed.read_text(encoding="utf-8"))


class TestRegistryInterception(_SnapshottedProject):
    """Global registry writes land in .sandbox/config/."""

    def test_register_in_dev_mode(self):
        root, ck = self.make_project()
        reg_file = ckconfig.GLOBAL_REGISTRY_FILE
        self.assertTrue(reg_file.exists())  # real init wrote it
        before = self.snapshot_tree(reg_file.parent)

        os.environ["CK_SANDBOX"] = "1"
        entry = ck.register(path=root, name="renamed-proj")

        # Real registry untouched.
        self.assertEqual(self.snapshot_tree(reg_file.parent), before)
        self.assertIn("renamed-proj", entry.name)

        # Sandbox registry has the update.
        sandboxed = sandbox.resolve_write_path(reg_file)
        self.assertTrue(sandboxed.exists())
        self.assertIn("renamed-proj",
                      sandboxed.read_text(encoding="utf-8"))

    def test_add_task_sync_writes_sandbox_registry_only(self):
        """`ck add` syncs the active-task pointer — that registry
        write must be intercepted too (the synced title is the
        ACTIVE task's, i.e. task 1 after init, refreshed by the add
        of task 3)."""
        root, ck = self.make_project()
        reg_file = ckconfig.GLOBAL_REGISTRY_FILE
        before = self.snapshot_tree(reg_file.parent)

        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("another dev task")

        self.assertEqual(self.snapshot_tree(reg_file.parent), before)
        sandboxed = sandbox.resolve_write_path(reg_file)
        self.assertTrue(sandboxed.exists())
        # The sandbox copy mirrors the real registry (seeded read)
        # and carries the synced active-task pointer.
        self.assertIn("proj", sandboxed.read_text(encoding="utf-8"))
        self.assertIn("active_task_id", sandboxed.read_text(encoding="utf-8"))

    def test_lock_files_not_created_in_production(self):
        """Dev-session *.lock files land in the sandbox, not the real
        .ck/ or ~/.config/ck/ trees. (Production-mode locks created
        BEFORE the dev session are fine — they're the normal lock
        protocol; only NEW lock creation must be redirected.)"""
        root, ck = self.make_project()
        prod_locks_before = {
            str(p.relative_to(root)) for p in root.rglob("*.lock")
        }
        before = self.snapshot_tree(root)
        before_reg = self.snapshot_tree(
            ckconfig.GLOBAL_REGISTRY_FILE.parent)

        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("lock test")

        self.assertEqual(self.snapshot_tree(root), before)
        self.assertEqual(
            self.snapshot_tree(ckconfig.GLOBAL_REGISTRY_FILE.parent),
            before_reg,
        )
        # No NEW locks appeared in production trees.
        prod_locks_after = {
            str(p.relative_to(root)) for p in root.rglob("*.lock")
        }
        self.assertEqual(prod_locks_after, prod_locks_before)
        # The dev-session lock lives in the sandbox.
        sandbox_locks = list(sandbox_root().rglob("*.lock"))
        self.assertTrue(sandbox_locks, "dev lock not found in sandbox")


class TestStateAndHistoryInterception(_SnapshottedProject):
    """state.json + HISTORY.md writes are redirected."""

    def test_save_state_redirected(self):
        root, ck = self.make_project()
        state_file = root / ".ck" / "state.json"
        before = self.snapshot_tree(root)

        os.environ["CK_SANDBOX"] = "1"
        state = ck._read_state()
        state["current_step"] = "dev step"
        ck._write_state(state)

        self.assertEqual(self.snapshot_tree(root), before)
        sandboxed = sandbox.resolve_write_path(state_file)
        self.assertTrue(sandboxed.exists())
        self.assertIn("dev step", sandboxed.read_text(encoding="utf-8"))

    def test_history_append_seeded_from_real(self):
        root, ck = self.make_project()
        history = root / ".ck" / "HISTORY.md"
        history.write_text("### 2026-01-01 10:00 | [1] seed\n- first\n",
                           encoding="utf-8")
        before = self.snapshot_tree(root)

        os.environ["CK_SANDBOX"] = "1"
        ck.edit_note(1, "dev note")

        # Real history untouched.
        self.assertEqual(self.snapshot_tree(root), before)

        # Sandbox copy contains BOTH the seed and the new note.
        sandboxed = sandbox.resolve_write_path(history)
        text = sandboxed.read_text(encoding="utf-8")
        self.assertIn("seed", text)
        self.assertIn("dev note", text)

    def test_init_in_dev_mode_writes_sandbox(self):
        """A fresh `init` in dev mode creates .ck/ ONLY in the
        sandbox — the real directory stays empty."""
        fresh = Path(self._tmp.name) / "fresh-proj"
        fresh.mkdir()

        os.environ["CK_SANDBOX"] = "1"
        ck = ContextKeeper(root=fresh)
        ck.init()

        # Real project directory: completely empty.
        self.assertEqual(list(fresh.iterdir()), [])
        # Sandbox has the whole .ck/ tree.
        sandboxed_root = sandbox.resolve_write_path(fresh / ".ck")
        self.assertTrue(sandboxed_root.is_dir())
        self.assertTrue((sandboxed_root / "PLAN.md").exists())


class TestReadsComeFromRealState(_SnapshottedProject):
    """Read operations must keep using the real paths."""

    def test_status_reads_real_plan(self):
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"

        # No sandbox copy yet: reads reflect the REAL plan.
        self.assertIn("alpha task", ck.tasks())
        self.assertIn("beta task", ck.tasks())

        # A dev add changes only the sandbox — reads are UNCHANGED.
        listing_before = ck.tasks()
        ck.add_task("sandbox-only task")
        self.assertEqual(ck.tasks(), listing_before)

        # The sandbox copy, though, has the new task.
        from cklib.sandbox import resolve_write_path
        self.assertIn("sandbox-only task",
                      resolve_write_path(ck.plan_file)
                      .read_text(encoding="utf-8"))

    def test_dashboard_reads_real_registry(self):
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        out = ck.dashboard()
        self.assertIn("proj", out)


class TestStdoutHygiene(_DevModeBase):
    """stdout must stay free of debug logs in dev mode."""

    def _run_cli(self, argv: list, cwd: Path) -> tuple:
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        orig = os.getcwd()
        os.chdir(cwd)
        try:
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                code = main(argv)
        finally:
            os.chdir(orig)
        return code, buf_out.getvalue(), buf_err.getvalue()

    def test_dev_add_stdout_clean_without_debug(self):
        root = Path(self._tmp.name) / "p"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()

        os.environ["CK_SANDBOX"] = "1"
        code, out, err = self._run_cli(["add", "clean stdout task"], root)

        self.assertEqual(code, 0)
        self.assertNotIn("[DEBUG]", out)
        self.assertNotIn("Intercepted", out)
        # Only the normal user-facing output.
        self.assertIn("Added task", out)
        # No debug log file was written either (debug disabled).
        self.assertFalse((sandbox_root() / "dev.log").exists())

    def test_dev_add_debug_logs_go_to_stderr_not_stdout(self):
        root = Path(self._tmp.name) / "p"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()

        os.environ["CK_SANDBOX"] = "1"
        os.environ["CK_DEBUG"] = "1"
        try:
            code, out, err = self._run_cli(["add", "debug task"], root)
        finally:
            os.environ.pop("CK_DEBUG", None)

        self.assertEqual(code, 0)
        # stdout: only user-facing output, zero debug lines.
        self.assertNotIn("[DEBUG]", out)
        self.assertIn("Added task", out)
        # stderr: the interception traces.
        self.assertIn("[DEBUG]", err)
        self.assertIn("Intercepted write", err)
        # dev.log got the same traces.
        log = (sandbox_root() / "dev.log").read_text(encoding="utf-8")
        self.assertIn("Intercepted write", log)


class TestCkDevWrapper(_DevModeBase):
    """The ck-dev executable sets CK_SANDBOX=1 and delegates."""

    def test_wrapper_sets_env_and_redirects_writes(self):
        root = Path(self._tmp.name) / "p"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()
        before = (ck.plan_file.read_bytes(),
                  ck.plan_file.stat().st_mtime_ns)

        # Run ck-dev as a subprocess (env isolation from this test).
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "ck-dev"), "add", "via ck-dev"],
            capture_output=True, text=True, timeout=60,
            cwd=str(root),
            env={**os.environ, "HOME": str(Path(self._tmp.name))},
        )
        try:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Added task", result.stdout)
            self.assertNotIn("[DEBUG]", result.stdout)

            # Real PLAN.md untouched.
            self.assertEqual(ck.plan_file.read_bytes(), before[0])
            self.assertEqual(ck.plan_file.stat().st_mtime_ns, before[1])

            # The write landed in the sandbox.
            self.assertTrue(sandbox_root().exists())
            found = [
                p for p in sandbox_root().rglob("PLAN.md")
                if b"via ck-dev" in p.read_bytes()
            ]
            self.assertTrue(found, "task not found in sandbox PLAN.md")
        finally:
            sandbox.clean_sandbox(quiet=True)

    def test_console_entrypoint_registered(self):
        """pyproject registers ck-dev -> cklib.cli:main; the env
        activation happens via the wrapper/entrypoint name."""
        import tomllib
        with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
            data = tomllib.load(fh)
        scripts = data["project"]["scripts"]
        self.assertEqual(scripts.get("ck-dev"), "cklib.cli:main")
        self.assertEqual(scripts.get("ck-clean"),
                         "cklib.cli:clean_sandbox_entrypoint")


class TestDevOffProductionNormal(_DevModeBase):
    """With dev mode OFF, everything writes to production as before."""

    def test_add_writes_real_plan_when_dev_off(self):
        root = Path(self._tmp.name) / "p"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()

        # CK_SANDBOX not set (base class popped it).
        self.assertNotIn("CK_SANDBOX", os.environ)
        ck.add_task("production task")
        self.assertIn("production task",
                      ck.plan_file.read_text(encoding="utf-8"))
        # Nothing in the sandbox.
        self.assertFalse(sandbox_root().exists())


if __name__ == "__main__":
    unittest.main()
