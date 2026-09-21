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

# Filesystem isolation safety net: importing the tests package
# pins CK_SANDBOX_ROOT to an OS-temp directory (see tests/__init__),
# so no test in this module can create or wipe the repository's own
# .sandbox/ — under ANY runner, including bare `unittest discover`.
import tests  # noqa: F401

import io
import os
import subprocess
import sys
import tempfile
import time
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

    def _run_cli(self, argv: list, cwd: Path) -> tuple:
        """Run ``cklib.cli.main`` in-process with captured streams.

        Shared by stdout-hygiene and dev-mode read-path tests: the
        chdir pins context resolution to ``cwd`` and is restored
        afterwards; stdout/stderr are captured so tests can assert
        on user-facing output while debug traces stay on stderr."""
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
        self.assertIn("- [x] Describe the first task",
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
        """Read-after-write contract (dev mode, .ck-only project).

        Regression guard for the missing-root-plan promotion bug:
        this project has ONLY ``.ck/PLAN.md`` (no root ``PLAN.md``),
        so a dev add used to be invisible to every read command —
        ``root_plan.is_file()`` skipped the promotion and ``st``
        kept rendering the stale ``.ck`` content. Reads must now
        REFLECT the dev mutation (and the read creates the root
        plan — see ``TestPromotionWithoutRootPlan``).
        """
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        self.assertFalse((root / "PLAN.md").is_file())

        # No sandbox copy yet: reads reflect the REAL plan.
        self.assertIn("alpha task", ck.tasks())
        self.assertIn("beta task", ck.tasks())

        # A dev add IS reflected by the next read (read-after-write):
        # the read promotes the newer mirror into the root plan.
        ck.add_task("sandbox-only task")
        listing = ck.tasks()
        self.assertIn("sandbox-only task", listing)
        self.assertIn("alpha task", listing)
        self.assertIn("beta task", listing)

        # The sandbox copy carries the new task too.
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
    """The ck-dev executable sets CK_SANDBOX=1 and delegates.

    The subprocess runs against a throwaway CHECKOUT COPY
    (tests/_isolated_checkout.py) so the wrapper's git-root-anchored
    ``.sandbox/`` lands inside this test's OS temp directory — the
    repository's own manual ``.sandbox/`` is never written to.
    """

    def test_wrapper_sets_env_and_redirects_writes(self):
        from tests._isolated_checkout import make_checkout_copy

        try:
            checkout = make_checkout_copy(
                Path(self._tmp.name) / "checkout-copy")
        except RuntimeError as e:
            self.skipTest(f"cannot build isolated checkout copy: {e}")

        root = Path(self._tmp.name) / "p"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()
        before = (ck.plan_file.read_bytes(),
                  ck.plan_file.stat().st_mtime_ns)

        # Run the copy's ck-dev as a subprocess (env isolation from
        # this test); its git root — and sandbox anchor — is `checkout`.
        result = subprocess.run(
            [sys.executable, str(checkout / "ck-dev"), "add", "via ck-dev"],
            capture_output=True, text=True, timeout=60,
            cwd=str(root),
            env={**os.environ, "HOME": str(Path(self._tmp.name))},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Added task", result.stdout)
        self.assertNotIn("[DEBUG]", result.stdout)

        # Real PLAN.md untouched.
        self.assertEqual(ck.plan_file.read_bytes(), before[0])
        self.assertEqual(ck.plan_file.stat().st_mtime_ns, before[1])

        # The write landed in the COPY's sandbox — never the repo's.
        copy_sandbox = checkout / ".sandbox"
        self.assertTrue(copy_sandbox.exists())
        found = [
            p for p in copy_sandbox.rglob("PLAN.md")
            if b"via ck-dev" in p.read_bytes()
        ]
        self.assertTrue(found, "task not found in sandbox PLAN.md")
        # Regression guard: nothing leaked into the repository's
        # manual .sandbox/ (if present).
        repo_sandbox = REPO_ROOT / ".sandbox"
        leaked = [
            p for p in repo_sandbox.rglob("PLAN.md")
            if b"via ck-dev" in p.read_bytes()
        ] if repo_sandbox.exists() else []
        self.assertFalse(leaked, "write escaped into the repo .sandbox/")

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


class TestSandboxMirrorMtimeInvalidation(_SnapshottedProject):
    """Stale sandbox PLAN.md mirrors are invalidated on external edits.

    The read-your-writes mirror is a CACHE of the real PLAN.md.
    When the real file changes on disk underneath a dev session
    (external editor, ``git pull``/``checkout``, ``cat > PLAN.md``),
    the mirror must be re-seeded from the real file before the next
    mutation reads it — otherwise every subsequent dev command
    composes on content that no longer exists.

    Invalidation signal: the real file's ``st_mtime_ns`` strictly
    exceeding the mirror's. A mirror that is equal/newer (i.e.
    freshly written by a dev mutation) must NEVER be re-seeded, or
    read-your-writes would be destroyed.

    The SAME check runs on the READ/DISPLAY path
    (``st``/``tasks``/``list``/``st --all``): a manual edit must be
    reflected by read commands immediately AND heal the mirror,
    without any mutating command in between.
    """

    def _dev_plan(self, root: Path) -> Path:
        from cklib.sandbox import resolve_write_path
        return resolve_write_path(root / ".ck" / "PLAN.md")

    def test_stale_mirror_reseeded_on_mutation(self):
        """Real PLAN.md edited externally after a dev write -> the
        next dev mutation composes on the NEW content (mirror was
        re-seeded), while the real file stays untouched."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("dev task")           # seeds the mirror
        mirror = self._dev_plan(root)
        self.assertTrue(mirror.is_file())
        self.assertIn("dev task", mirror.read_text(encoding="utf-8"))

        # External edit of the real file (later mtime).
        real = root / ".ck" / "PLAN.md"
        real.write_text(
            "# proj\n\n## Current Sprint\n- [ ] externally added task\n\n"
            "## Completed\n",
            encoding="utf-8",
        )
        os.utime(real, ns=(time.time_ns() + 2_000_000,) * 2)
        before_real = self.snapshot_tree(root)

        # The next dev mutation must pick up the external content.
        new_id = ck.add_task("post-edit dev task")
        self.assertEqual(new_id, 2)  # ID derived from the NEW plan
        mirror_text = mirror.read_text(encoding="utf-8")
        self.assertIn("externally added task", mirror_text)
        self.assertIn("post-edit dev task", mirror_text)

        # The real file was only read, never written.
        self.assertEqual(self.snapshot_tree(root), before_real)

    def test_fresh_mirror_not_clobbered_by_reseed(self):
        """A mirror at the same mtime as the real file (i.e. written
        by a dev mutation with unchanged source) is NEVER re-seeded —
        read-your-writes must survive chained dev commands."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("first dev task")
        ck.add_task("second dev task")
        mirror = self._dev_plan(root)
        text = mirror.read_text(encoding="utf-8")
        self.assertIn("first dev task", text)
        self.assertIn("second dev task", text)
        # Same-mtime mirror survived both chained mutations.
        self.assertNotIn("first dev task",
                         (root / ".ck" / "PLAN.md").read_text(
                             encoding="utf-8"))

    def test_reads_reflect_external_edit_and_heal_mirror(self):
        """Manual PLAN.md edit + read commands ONLY (no mutation):
        ``list``/``st``/``full_plan_text`` reflect the updated
        tasks immediately AND the stale mirror is re-seeded as a
        side effect of the read."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("dev task")           # seeds the mirror
        mirror = self._dev_plan(root)

        # Manual disk edit of the real file (later mtime).
        real = root / ".ck" / "PLAN.md"
        real.write_text(
            "# proj\n\n## Current Sprint\n- [ ] externally added task\n\n"
            "## Completed\n",
            encoding="utf-8",
        )
        os.utime(real, ns=(time.time_ns() + 2_000_000,) * 2)
        before_real = self.snapshot_tree(root)

        keeper = ContextKeeper(root=root)
        # READS ONLY from here on — no mutating command in between.
        out = keeper.tasks()
        self.assertIn("externally added task", out)
        self.assertIn("externally added task", keeper.status())
        self.assertIn("externally added task", keeper.full_plan_text())

        # The read healed the stale mirror (re-seeded from disk).
        self.assertIn("externally added task",
                      mirror.read_text(encoding="utf-8"))
        # The real file was only read, never written.
        self.assertEqual(self.snapshot_tree(root), before_real)

    def test_cli_read_commands_reflect_edit_without_mutation(self):
        """CLI level: ``ck-dev st`` / ``tasks`` / ``list`` after a
        manual ``cat > PLAN.md`` — no mutation first. stdout is
        clean and the sandbox copy converges."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("sandbox-only task")
        mirror = self._dev_plan(root)

        real = root / ".ck" / "PLAN.md"
        real.write_text(
            "# proj\n\n## Current Sprint\n- [ ] externally added task\n\n"
            "## Completed\n",
            encoding="utf-8",
        )
        os.utime(real, ns=(time.time_ns() + 2_000_000,) * 2)

        for argv, marker in (
            (["st"], "externally added task"),
            (["list"], "externally added task"),
        ):
            code, out, err = self._run_cli(argv, root)
            self.assertEqual(code, 0, err)
            self.assertIn(marker, out)
            self.assertNotIn("[DEBUG]", out)

        # The reads healed the mirror: it now carries the external
        # content, and the superseded sandbox-only task is gone (the
        # real file is authoritative once it changed on disk).
        healed = mirror.read_text(encoding="utf-8")
        self.assertIn("externally added task", healed)
        self.assertNotIn("sandbox-only task", healed)

    def test_read_heal_preserves_fresh_mirror_content(self):
        """Reads must NOT clobber a mirror that is equal/newer than
        the real file: dev-only tasks written by the current session
        survive ``st``/``tasks``/``list`` (read-your-writes on the
        display path)."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("sandbox-only task")   # mirror now NEWER than real
        mirror = self._dev_plan(root)
        real_before = (root / ".ck" / "PLAN.md").read_text(
            encoding="utf-8")

        keeper = ContextKeeper(root=root)
        keeper.tasks()
        keeper.status()

        # Dev-only content survived both reads; the real file was
        # not cross-contaminated either.
        self.assertIn("sandbox-only task",
                      mirror.read_text(encoding="utf-8"))
        self.assertEqual(
            (root / ".ck" / "PLAN.md").read_text(encoding="utf-8"),
            real_before,
        )

    def test_mirror_only_project_no_stale_crash(self):
        """A project bootstrapped entirely in the sandbox (no real
        .ck/ tree) still resolves through the mirror; the sync is a
        no-op when there is no real file to compare against."""
        fresh = Path(self._tmp.name) / "mirror-only"
        fresh.mkdir()
        os.environ["CK_SANDBOX"] = "1"
        ck = ContextKeeper(root=fresh)
        ck.init()
        mirror = self._dev_plan(fresh)
        self.assertTrue(mirror.is_file())
        self.assertFalse((fresh / ".ck" / "PLAN.md").is_file())
        # Reads + mutations compose through the mirror as before.
        self.assertIn("Describe the first task", ck.tasks())
        new_id = ck.add_task("mirror-only task")
        self.assertIn("mirror-only task", mirror.read_text(encoding="utf-8"))
        self.assertGreaterEqual(new_id, 1)

    def test_sync_noop_outside_dev_mode(self):
        """Without dev mode the sync helper is a plain no-op (no
        sandbox exists, nothing to invalidate, nothing created)."""
        root, ck = self.make_project()
        self.assertNotIn("CK_SANDBOX", os.environ)
        from cklib.core import _sync_sandbox_plan_mirror
        _sync_sandbox_plan_mirror(root)
        self.assertFalse(sandbox_root().exists())

    def test_sync_debug_trace_goes_to_devlog(self):
        """With CK_DEBUG=1 the re-seed event is traceable in
        .sandbox/dev.log (never on stdout)."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("dev task")
        real = root / ".ck" / "PLAN.md"
        real.write_text(
            "# proj\n\n## Current Sprint\n- [ ] externally added task\n\n"
            "## Completed\n",
            encoding="utf-8",
        )
        os.utime(real, ns=(time.time_ns() + 2_000_000,) * 2)

        os.environ["CK_DEBUG"] = "1"
        ck.add_task("post-edit dev task")
        log = (sandbox_root() / "dev.log").read_text(encoding="utf-8")
        self.assertIn("Re-seeded stale PLAN.md mirror", log)


class TestRootPlanDisplayPath(_SnapshottedProject):
    """ROOT-level PLAN.md precedence on the read path (stale-read fix).

    A hand-authored ``<project_root>/PLAN.md`` must win over the
    ``.ck/PLAN.md`` copy: ``_plan_display_path()`` returns the ROOT
    path (never ``.ck/PLAN.md`` when the root file exists), so the
    read commands (``st``/``tasks``/``list``/``st --all``) reflect
    root-plan edits immediately. A dev-mode mirror stale against a
    newer root plan is re-seeded from the ROOT plan on the same read
    (mtime check), and only a project with NO root plan falls back to
    displaying ``.ck/PLAN.md``.
    """

    ROOT_PLAN = (
        "# proj\n\n"
        "## Current Sprint\n"
        "- [ ] freshly authored root task\n\n"
        "## Completed\n"
    )

    def _write_root_plan(self, root: Path) -> Path:
        """Manually overwrite ``<root>/PLAN.md`` with a strictly newer
        mtime (same-clock writes would tie and skip the re-seed)."""
        root_plan = root / "PLAN.md"
        root_plan.write_text(self.ROOT_PLAN, encoding="utf-8")
        os.utime(root_plan, ns=(time.time_ns() + 2_000_000,) * 2)
        return root_plan

    # ---- path resolution ---------------------------------------------- #

    def test_display_path_returns_root_plan_not_ck_copy(self):
        """The displayed path is <root>/PLAN.md — never .ck/PLAN.md
        when the root file exists (the stale-read regression)."""
        root, ck = self.make_project()
        root_plan = self._write_root_plan(root)

        # Path-targeting sanity: the keeper still binds the .ck/ copy
        # for mutations; only the DISPLAY path changed.
        self.assertEqual(ck.root, root)
        self.assertEqual(ck.plan_file, root / ".ck" / "PLAN.md")

        displayed = ck._plan_display_path()
        self.assertEqual(displayed, root / "PLAN.md")
        self.assertNotEqual(displayed, ck.plan_file)

    def test_display_path_falls_back_to_ck_plan_without_root_file(self):
        """No root PLAN.md on disk: the .ck/ copy is displayed."""
        root, ck = self.make_project()
        self.assertFalse((root / "PLAN.md").exists())
        self.assertEqual(ck._plan_display_path(), ck.plan_file)

    def test_dev_mode_display_path_targets_real_project_root(self):
        """Under CK_SANDBOX=1 the display path still targets the REAL
        project directory root (<root>/PLAN.md), never the sandboxed
        ``.sandbox/projects/<hash>/.ck/PLAN.md`` mirror."""
        root, ck = self.make_project()
        root_plan = self._write_root_plan(root)
        os.environ["CK_SANDBOX"] = "1"

        keeper = ContextKeeper(root=root)
        displayed = keeper._plan_display_path()
        self.assertEqual(displayed, root_plan)
        self.assertNotEqual(displayed, keeper.plan_file)
        from cklib.sandbox import resolve_write_path
        self.assertNotEqual(
            displayed, resolve_write_path(keeper.plan_file, create=False)
        )

    # ---- content reflection (in-process) ------------------------------- #

    def test_ck_st_reflects_root_plan_content_immediately(self):
        root, ck = self.make_project()
        self._write_root_plan(root)
        os.environ["CK_SANDBOX"] = "1"

        code, out, err = self._run_cli(["st"], root)
        self.assertEqual(code, 0, err)
        self.assertIn("freshly authored root task", out)
        # The stale .ck/ scaffold task is gone from the display.
        self.assertNotIn("Describe the first task", out)

    def test_ck_tasks_and_full_plan_reflect_root_plan(self):
        root, ck = self.make_project()
        self._write_root_plan(root)
        self.assertIn("freshly authored root task", ck.tasks())
        self.assertIn("freshly authored root task", ck.full_plan_text())
        self.assertNotIn("Describe the first task", ck.tasks())

    # ---- content reflection (ck-dev subprocess) ------------------------ #

    def test_ck_dev_st_reflects_root_plan_immediately(self):
        """End-to-end: `ck-dev st` (real wrapper subprocess) shows the
        freshly authored root-plan content with NO mutating command in
        between — and the read stays read-only."""
        from tests._isolated_checkout import make_checkout_copy
        try:
            checkout = make_checkout_copy(
                Path(self._tmp.name) / "checkout-copy")
        except RuntimeError as e:
            self.skipTest(f"cannot build isolated checkout copy: {e}")

        root, ck = self.make_project()
        self._write_root_plan(root)
        before = self.snapshot_tree(root)

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("CK_SANDBOX", "CK_SANDBOX_ROOT", "CK_DEV",
                         "CK_DEBUG")
        }
        env["HOME"] = str(Path(self._tmp.name))
        result = subprocess.run(
            [sys.executable, str(checkout / "ck-dev"), "st"],
            capture_output=True, text=True, timeout=60,
            cwd=str(root), env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("freshly authored root task", result.stdout)
        self.assertNotIn("Describe the first task", result.stdout)
        # Read-only: the real project was never modified.
        self.assertEqual(self.snapshot_tree(root), before)

    # ---- mirror healing from the root plan ----------------------------- #

    def test_stale_ck_mirror_healed_from_newer_root_plan(self):
        """Dev mirror + newer root plan: the display read re-seeds the
        mirror FROM the root plan (mtime check) while still returning
        the root path itself."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("sandbox-only task")   # seeds the mirror
        from cklib.sandbox import resolve_write_path
        mirror = resolve_write_path(ck.plan_file, create=False)
        self.assertIn("sandbox-only task",
                      mirror.read_text(encoding="utf-8"))

        root_plan = self._write_root_plan(root)
        keeper = ContextKeeper(root=root)
        self.assertEqual(keeper._plan_display_path(), root_plan)

        # Healed from the ROOT plan: superseded dev-only task gone.
        healed = mirror.read_text(encoding="utf-8")
        self.assertIn("freshly authored root task", healed)
        self.assertNotIn("sandbox-only task", healed)

    def test_dev_mutation_composes_on_newer_root_plan(self):
        """After a root-plan edit the next dev mutation composes on the
        NEW content (mirror re-seeded from the root plan); the real
        project files are never written back."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("sandbox-only task")   # seeds the mirror
        self._write_root_plan(root)
        before = self.snapshot_tree(root)

        new_id = ck.add_task("post-edit dev task")
        self.assertEqual(new_id, 2)  # IDs derived from the NEW plan

        from cklib.sandbox import resolve_write_path
        text = resolve_write_path(ck.plan_file, create=False) \
            .read_text(encoding="utf-8")
        self.assertIn("freshly authored root task", text)
        self.assertIn("post-edit dev task", text)
        self.assertNotIn("sandbox-only task", text)
        # Real project untouched (root plan only read, never written).
        self.assertEqual(self.snapshot_tree(root), before)


class TestReadAfterWritePromotion(_SnapshottedProject):
    """Bidirectional mirror sync: dev mutations reach the ROOT plan.

    Regression guard for the unidirectional-mirror bug: ``ck-dev add``
    (and every other mutation) writes the sandboxed mirror, while
    ``_plan_display_path()`` prefers the project-root ``PLAN.md``.
    With a one-way sync (root -> mirror only) the dev mutation was
    invisible to every read command — the stale root plan shadowed
    the mirror forever.

    The sync must be strictly ``st_mtime_ns``-driven in BOTH
    directions:

    - MIRROR NEWER (the read-after-write case: the last write was a
      dev mutation) → the mirror content is PROMOTED into the root
      plan, so ``ck-dev st``/``ck-dev list`` AND the root file itself
      show the new task immediately (``.ck/PLAN.md`` stays immutable
      — the production-immutability contract holds; convergence
      flows through the promoted root plan);
    - ROOT PLAN NEWER (manual edit) → the mirror is re-seeded from
      it (covered by ``TestRootPlanDisplayPath`` and
      ``TestSandboxMirrorMtimeInvalidation``).
    """

    ROOT_PLAN = (
        "# proj\n\n"
        "## Current Sprint\n"
        "- [ ] freshly authored root task\n\n"
        "## Completed\n"
    )

    def _write_root_plan(self, root: Path) -> Path:
        """Hand-author ``<root>/PLAN.md`` (the manual-edit step).

        No mtime bump needed: the root plan is written strictly after
        ``make_project`` created ``.ck/PLAN.md``, so on ns-resolution
        filesystems it is already the newest real plan — exactly the
        state a user is in after editing the root plan by hand.
        """
        root_plan = root / "PLAN.md"
        root_plan.write_text(self.ROOT_PLAN, encoding="utf-8")
        return root_plan

    def test_dev_mutation_promoted_to_root_plan_on_read(self):
        """In-process: manual root plan -> dev add -> next read
        promotes the mirror content into the root plan so the task
        shows up in ``tasks()``/``status()`` AND in the root file."""
        root, ck = self.make_project()
        root_plan = self._write_root_plan(root)
        os.environ["CK_SANDBOX"] = "1"

        ck.add_task("promoted task")  # mutation -> sandboxed mirror

        # The mutation alone stayed in the sandbox: the root plan is
        # untouched until a READ promotes the newer mirror.
        self.assertNotIn("promoted task",
                         root_plan.read_text(encoding="utf-8"))

        keeper = ContextKeeper(root=root)
        # Display still targets the root plan (precedence unchanged).
        self.assertEqual(keeper._plan_display_path(), root_plan)

        # PROMOTION: root plan now carries the dev mutation AND the
        # manually authored content it was composed on.
        text = root_plan.read_text(encoding="utf-8")
        self.assertIn("promoted task", text)
        self.assertIn("freshly authored root task", text)
        # The real .ck/ tree stayed immutable.
        self.assertNotIn("promoted task",
                         (root / ".ck" / "PLAN.md").read_text(
                             encoding="utf-8"))

        # Read commands render the promoted content immediately.
        self.assertIn("promoted task", keeper.tasks())
        self.assertIn("promoted task", keeper.status())

    def test_promotion_traced_in_dev_log(self):
        """With CK_DEBUG=1 the promotion event is traceable in
        .sandbox/dev.log (never on stdout) — observability parity
        with the re-seed direction."""
        root, ck = self.make_project()
        self._write_root_plan(root)
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("traced task")

        os.environ["CK_DEBUG"] = "1"
        try:
            ContextKeeper(root=root)._plan_display_path()
        finally:
            os.environ.pop("CK_DEBUG", None)

        log = (sandbox_root() / "dev.log").read_text(encoding="utf-8")
        self.assertIn("Promoted newer PLAN.md mirror", log)

    def test_promotion_converges_without_mtime_ping_pong(self):
        """Repeated reads after a promotion are converged no-ops: the
        equality guards must prevent the mirror/root mtime race from
        flip-flopping the sync direction on every read."""
        root, ck = self.make_project()
        root_plan = self._write_root_plan(root)
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("converged task")

        keeper = ContextKeeper(root=root)
        keeper._plan_display_path()  # promotes
        promoted = root_plan.read_text(encoding="utf-8")
        root_ns = root_plan.stat().st_mtime_ns
        from cklib.sandbox import resolve_write_path
        mirror = resolve_write_path(ck.plan_file, create=False)
        mirror_ns = mirror.stat().st_mtime_ns

        for _ in range(3):
            self.assertEqual(keeper._plan_display_path(), root_plan)
            keeper.tasks()

        # Content AND mtimes stable across repeated reads.
        self.assertEqual(root_plan.read_text(encoding="utf-8"), promoted)
        self.assertEqual(root_plan.stat().st_mtime_ns, root_ns)
        self.assertEqual(mirror.stat().st_mtime_ns, mirror_ns)

    def test_ck_dev_add_then_st_and_list_show_new_task(self):
        """End-to-end (real ``ck-dev`` subprocess): manual root-plan
        edit -> ``ck-dev add "new task"`` -> ``ck-dev st`` and
        ``ck-dev list`` both show the new task immediately AND the
        root ``PLAN.md`` contains it (read-after-write holds)."""
        from tests._isolated_checkout import make_checkout_copy
        try:
            checkout = make_checkout_copy(
                Path(self._tmp.name) / "checkout-copy")
        except RuntimeError as e:
            self.skipTest(f"cannot build isolated checkout copy: {e}")

        root, ck = self.make_project()
        root_plan = self._write_root_plan(root)

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("CK_SANDBOX", "CK_SANDBOX_ROOT", "CK_DEV",
                         "CK_DEBUG")
        }
        env["HOME"] = str(Path(self._tmp.name))

        add = subprocess.run(
            [sys.executable, str(checkout / "ck-dev"),
             "add", "brand-new task"],
            capture_output=True, text=True, timeout=60,
            cwd=str(root), env=env,
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertIn("Added task", add.stdout)
        self.assertNotIn("[DEBUG]", add.stdout)

        # The mutation alone stays sandboxed; promotion happens on
        # the read path (strict mtime comparison), not on write.
        self.assertNotIn("brand-new task",
                         root_plan.read_text(encoding="utf-8"))

        for argv in (["st"], ["list"], ["st", "-l"]):
            out = subprocess.run(
                [sys.executable, str(checkout / "ck-dev"), *argv],
                capture_output=True, text=True, timeout=60,
                cwd=str(root), env=env,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            # Read-your-writes: the freshly added task is visible.
            self.assertIn("brand-new task", out.stdout)
            self.assertNotIn("[DEBUG]", out.stdout)

        # The root plan itself was promoted: manual content AND the
        # dev mutation composed together.
        text = root_plan.read_text(encoding="utf-8")
        self.assertIn("brand-new task", text)
        self.assertIn("freshly authored root task", text)
        # The real .ck/ tree stayed immutable (only the sandbox copy
        # and the non-sandbox-protected root plan were written).
        self.assertNotIn("brand-new task",
                         (root / ".ck" / "PLAN.md").read_text(
                             encoding="utf-8"))


class TestPromotionWithoutRootPlan(_SnapshottedProject):
    """Promotion must CREATE the root plan when only .ck/PLAN.md exists.

    Regression guard for the missing-root-plan bug: a project that
    was bootstrapped entirely in dev mode (only ``.ck/PLAN.md`` on
    disk — e.g. inside ``.sandbox/projects/<hash>/``) had NO root
    ``PLAN.md``, so the promotion condition
    ``root_plan.is_file() and ...`` silently skipped every dev
    mutation and ``ck-dev st``/``list`` never showed the added task.

    The fix: when the mirror is newer and no root plan exists, the
    READ path creates it from the mirror content (first-ever
    materialization). The MUTATION path still never creates the
    root plan (dev mutations must not materialize production files
    behind the user's back — see ``TestAddTaskInterception``).
    """

    def test_read_creates_missing_root_plan_from_mirror(self):
        """In-process: .ck-only project -> dev add -> next read
        creates the root plan carrying the added task."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        self.assertFalse((root / "PLAN.md").is_file())

        ck.add_task("brand-new task")   # mutation -> sandboxed mirror

        # The mutation alone stays in the sandbox: no root plan yet.
        self.assertFalse((root / "PLAN.md").is_file())

        keeper = ContextKeeper(root=root)
        # PROMOTION: the read materializes the root plan.
        self.assertEqual(keeper._plan_display_path(),
                         root / "PLAN.md")
        self.assertTrue((root / "PLAN.md").is_file())

        text = (root / "PLAN.md").read_text(encoding="utf-8")
        self.assertIn("brand-new task", text)
        # The composition baseline (the real .ck plan content) is
        # preserved too — the mirror was seeded from it before the
        # dev mutation.
        self.assertIn("alpha task", text)
        # The real .ck/ tree stayed immutable.
        self.assertNotIn("brand-new task",
                         (root / ".ck" / "PLAN.md").read_text(
                             encoding="utf-8"))

        # Read commands render the promoted content immediately.
        self.assertIn("brand-new task", keeper.tasks())
        # The compact status block hides tail tasks, but its progress
        # counters prove the promoted root plan was parsed (4 tasks).
        self.assertIn("0/4 tasks done", keeper.status())
        self.assertIn("brand-new task", keeper.full_plan_text())

    def test_promotion_without_root_plan_converges(self):
        """Repeated reads after the first-time promotion are no-ops:
        no mtime churn, no ping-pong with the mirror."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("converged task")

        keeper = ContextKeeper(root=root)
        keeper._plan_display_path()   # creates + promotes
        root_plan = root / "PLAN.md"
        promoted = root_plan.read_text(encoding="utf-8")
        root_ns = root_plan.stat().st_mtime_ns
        from cklib.sandbox import resolve_write_path
        mirror = resolve_write_path(ck.plan_file, create=False)
        mirror_ns = mirror.stat().st_mtime_ns

        for _ in range(3):
            self.assertEqual(keeper._plan_display_path(), root_plan)
            keeper.tasks()

        self.assertEqual(root_plan.read_text(encoding="utf-8"), promoted)
        self.assertEqual(root_plan.stat().st_mtime_ns, root_ns)
        self.assertEqual(mirror.stat().st_mtime_ns, mirror_ns)

    def test_promotion_traced_in_dev_log_without_root_plan(self):
        """The first-time creation event is traceable in dev.log with
        CK_DEBUG=1 (observability parity with the promotion of an
        existing root plan)."""
        root, ck = self.make_project()
        os.environ["CK_SANDBOX"] = "1"
        ck.add_task("traced creation task")

        os.environ["CK_DEBUG"] = "1"
        try:
            ContextKeeper(root=root)._plan_display_path()
        finally:
            os.environ.pop("CK_DEBUG", None)

        log = (sandbox_root() / "dev.log").read_text(encoding="utf-8")
        self.assertIn("Created root PLAN.md from newer mirror", log)

    def test_dev_off_never_creates_root_plan(self):
        """Without dev mode the read path stays side-effect free: no
        root plan is created from the real .ck/ plan."""
        root, ck = self.make_project()
        self.assertNotIn("CK_SANDBOX", os.environ)
        keeper = ContextKeeper(root=root)
        self.assertEqual(keeper._plan_display_path(), ck.plan_file)
        self.assertFalse((root / "PLAN.md").is_file())

    def test_ck_dev_add_then_st_and_list_show_new_task_without_root_plan(self):
        """End-to-end (real ``ck-dev`` subprocess): .ck-only project
        -> ``ck-dev add "new task"`` -> ``ck-dev st`` and
        ``ck-dev list`` both show the new task immediately AND the
        root ``PLAN.md`` now exists carrying it."""
        from tests._isolated_checkout import make_checkout_copy
        try:
            checkout = make_checkout_copy(
                Path(self._tmp.name) / "checkout-copy")
        except RuntimeError as e:
            self.skipTest(f"cannot build isolated checkout copy: {e}")

        root, ck = self.make_project()
        self.assertFalse((root / "PLAN.md").is_file())

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("CK_SANDBOX", "CK_SANDBOX_ROOT", "CK_DEV",
                         "CK_DEBUG")
        }
        env["HOME"] = str(Path(self._tmp.name))

        add = subprocess.run(
            [sys.executable, str(checkout / "ck-dev"),
             "add", "brand-new task"],
            capture_output=True, text=True, timeout=60,
            cwd=str(root), env=env,
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertIn("Added task", add.stdout)
        self.assertNotIn("[DEBUG]", add.stdout)

        # The mutation alone stayed sandboxed: still no root plan.
        self.assertFalse((root / "PLAN.md").is_file())

        for argv, marker in (
            (["list"], "brand-new task"),
            (["st", "-l"], "brand-new task"),
            # The compact status block hides tail tasks, but its
            # progress counters prove the promoted root plan (4 tasks
            # after the add) was parsed.
            (["st"], "0/4 tasks done"),
        ):
            out = subprocess.run(
                [sys.executable, str(checkout / "ck-dev"), *argv],
                capture_output=True, text=True, timeout=60,
                cwd=str(root), env=env,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            # Read-your-writes: the freshly added task is visible.
            self.assertIn(marker, out.stdout)
            self.assertNotIn("[DEBUG]", out.stdout)

        # The read promoted (first created) the root plan: it now
        # exists and carries manual baseline AND the dev mutation.
        self.assertTrue((root / "PLAN.md").is_file())
        text = (root / "PLAN.md").read_text(encoding="utf-8")
        self.assertIn("brand-new task", text)
        self.assertIn("alpha task", text)
        # The real .ck/ tree stayed immutable (only the sandbox copy
        # and the non-sandbox-protected root plan were written).
        self.assertNotIn("brand-new task",
                         (root / ".ck" / "PLAN.md").read_text(
                             encoding="utf-8"))


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
