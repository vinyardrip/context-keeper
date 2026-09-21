"""Tests for the .sandbox/ infrastructure and ck-clean (Step 1).

Covers:

- ``ensure_sandbox_dir``: creation, structure (projects/, config/),
  idempotency.
- ``clean_sandbox``: removal, idempotency (missing dir), symlink
  guard (link removed, target untouched), error paths.
- ``ck-clean`` execution: the ``ck dev clean`` dispatch, the console
  entrypoint, and the executable wrapper script.

ISOLATION: every filesystem operation in this module targets the
per-test sandbox anchor pinned by the conftest autouse fixture
(``CK_SANDBOX_ROOT`` under the OS temp directory) — the repository's
own ``.sandbox/`` (the manual dev environment) is never created,
modified, or deleted here. The default-anchoring test below is a
pure path computation with the override cleared.
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
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from cklib import sandbox
from cklib.sandbox import (
    SANDBOX_ROOT_ENV,
    clean_sandbox,
    ensure_sandbox_dir,
    is_within_sandbox,
    project_hash,
    sandbox_config_dir,
    sandbox_project_dir,
    sandbox_root,
)
from cklib import cli


REPO_ROOT = Path(__file__).resolve().parent.parent


class TestSandboxRoot(unittest.TestCase):
    """The sandbox root is anchored at the repository root."""

    def test_sandbox_root_is_repo_root_child(self):
        # DEFAULT anchoring (no override): pure path computation,
        # no filesystem access. The conftest fixture pins the
        # override for every test, so clear it here to probe the
        # default.
        env = {k: v for k, v in os.environ.items()
               if k != SANDBOX_ROOT_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(sandbox_root(), REPO_ROOT / ".sandbox")

    def test_override_pins_anchor_outside_repo(self):
        # The conftest fixture's anchor: absolute, under the OS temp
        # directory, never the repository's .sandbox/.
        root = sandbox_root()
        self.assertTrue(root.is_absolute())
        self.assertNotEqual(root, REPO_ROOT / ".sandbox")
        self.assertNotIn(str(REPO_ROOT), str(root))

    def test_sandbox_root_is_absolute_and_lexical(self):
        root = sandbox_root()
        self.assertTrue(root.is_absolute())
        # LEXICAL by design: a symlinked .sandbox must stay visible
        # as a link so clean_sandbox removes the link, not the
        # pointed-to tree.
        self.assertEqual(root.name, ".sandbox")


class TestEnsureSandboxDir(unittest.TestCase):
    """Creation and structure of .sandbox/."""

    def setUp(self):
        # Guarantee a pristine state before each creation test.
        clean_sandbox(quiet=True)

    def tearDown(self):
        clean_sandbox(quiet=True)

    def test_creates_root_and_subdirs(self):
        root = ensure_sandbox_dir()
        self.assertTrue(root.is_dir())
        self.assertTrue((root / "projects").is_dir())
        self.assertTrue((root / "config").is_dir())

    def test_returns_root_path(self):
        self.assertEqual(ensure_sandbox_dir(), sandbox_root())

    def test_idempotent_double_creation(self):
        ensure_sandbox_dir()
        root2 = ensure_sandbox_dir()  # must not raise
        self.assertTrue(root2.is_dir())

    def test_creation_preserves_existing_content(self):
        ensure_sandbox_dir()
        marker = sandbox_root() / "projects" / "keep.txt"
        marker.write_text("data", encoding="utf-8")
        ensure_sandbox_dir()  # re-run must not wipe
        self.assertEqual(marker.read_text(encoding="utf-8"), "data")


class TestSandboxProjectDir(unittest.TestCase):
    """Per-project redirected write targets."""

    def setUp(self):
        clean_sandbox(quiet=True)

    def tearDown(self):
        clean_sandbox(quiet=True)

    def test_project_dir_lives_under_sandbox(self):
        d = sandbox_project_dir("/some/real/project")
        self.assertTrue(is_within_sandbox(d))
        self.assertIn("projects", d.parts)

    def test_project_dir_created_on_demand(self):
        d = sandbox_project_dir("/some/real/project")
        self.assertTrue(d.is_dir())

    def test_same_project_maps_to_same_dir(self):
        a = sandbox_project_dir("/some/real/project")
        b = sandbox_project_dir("/some/real/project")
        self.assertEqual(a, b)

    def test_hash_distinguishes_same_named_projects(self):
        a = sandbox_project_dir("/loc/a/project")
        b = sandbox_project_dir("/loc/b/project")
        self.assertNotEqual(a, b)
        # Both carry the project name as a readable prefix.
        self.assertTrue(a.name.startswith("project-"))
        self.assertTrue(b.name.startswith("project-"))

    def test_project_hash_is_stable_and_path_sensitive(self):
        self.assertEqual(project_hash("/x/p"), project_hash("/x/p"))
        self.assertNotEqual(project_hash("/x/p"), project_hash("/y/p"))

    def test_config_dir_under_sandbox(self):
        d = sandbox_config_dir()
        self.assertTrue(is_within_sandbox(d))
        self.assertEqual(d.name, "config")


class TestCleanSandbox(unittest.TestCase):
    """clean_sandbox: removal, idempotency, edge cases."""

    def setUp(self):
        clean_sandbox(quiet=True)

    def tearDown(self):
        clean_sandbox(quiet=True)

    def test_clean_removes_populated_tree(self):
        root = ensure_sandbox_dir()
        deep = root / "projects" / "proj-123" / ".ck"
        deep.mkdir(parents=True)
        (deep / "PLAN.md").write_text("- [ ] x\n", encoding="utf-8")
        (root / "dev.log").write_text("log\n", encoding="utf-8")

        self.assertTrue(clean_sandbox(quiet=True))
        self.assertFalse(root.exists())

    def test_clean_when_missing_is_success(self):
        # setUp removed it; nothing exists — no error, True.
        self.assertTrue(clean_sandbox(quiet=True))
        self.assertFalse(sandbox_root().exists())

    def test_double_clean_is_idempotent(self):
        ensure_sandbox_dir()
        self.assertTrue(clean_sandbox(quiet=True))
        self.assertTrue(clean_sandbox(quiet=True))

    def test_clean_removes_sandbox_file(self):
        # .sandbox exists as a regular FILE (user artifact): removed.
        root = sandbox_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        root.write_text("junk", encoding="utf-8")
        self.assertTrue(clean_sandbox(quiet=True))
        self.assertFalse(root.exists())

    def test_clean_symlink_removes_link_only(self):
        # .sandbox is a SYMLINK to a directory: only the link is
        # removed; the pointed-to tree must remain untouched.
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "precious"
            target.mkdir()
            (target / "keep.txt").write_text("vital", encoding="utf-8")
            root = sandbox_root()
            root.parent.mkdir(parents=True, exist_ok=True)
            root.symlink_to(target)

            self.assertTrue(clean_sandbox(quiet=True))
            self.assertFalse(root.exists() or root.is_symlink())
            # The target tree is untouched.
            self.assertTrue((target / "keep.txt").exists())
            self.assertEqual(
                (target / "keep.txt").read_text(encoding="utf-8"), "vital"
            )

    def test_clean_output_message_on_stdout(self):
        ensure_sandbox_dir()
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = clean_sandbox()
        self.assertTrue(ok)
        self.assertIn("Sandbox environment cleared successfully.", buf.getvalue())
        self.assertIn("[ck-clean]", buf.getvalue())

    def test_clean_missing_dir_message(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = clean_sandbox()
        self.assertTrue(ok)
        self.assertIn("nothing to clean", buf.getvalue())

    def test_clean_permission_error_returns_false(self):
        # Make .sandbox un-removable: parent dir read-only. On some
        # filesystems root bypasses this; skip if the removal
        # actually succeeds.
        ensure_sandbox_dir()
        (sandbox_root() / "projects" / "sub").mkdir(exist_ok=True)
        parent = sandbox_root().parent
        mode = parent.stat().st_mode
        try:
            parent.chmod(0o500)
            err = io.StringIO()
            with redirect_stderr(err):
                ok = clean_sandbox(quiet=True)
            if ok:
                self.skipTest("filesystem permits removal despite parent mode")
            self.assertFalse(ok)
            self.assertIn("[ck-clean]", err.getvalue())
        finally:
            parent.chmod(mode)


class TestCleanViaCli(unittest.TestCase):
    """`ck dev clean` dispatch + console entrypoint + wrapper."""

    def setUp(self):
        clean_sandbox(quiet=True)

    def tearDown(self):
        clean_sandbox(quiet=True)

    def test_dev_clean_removes_sandbox(self):
        ensure_sandbox_dir()
        self.assertTrue((sandbox_root() / "projects").is_dir())

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli._run_dev_command("clean")
        self.assertEqual(code, 0)
        self.assertFalse(sandbox_root().exists())
        self.assertIn("cleared successfully", buf.getvalue())

    def test_dev_clean_missing_sandbox_exit_zero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli._run_dev_command("clean")
        self.assertEqual(code, 0)
        self.assertIn("nothing to clean", buf.getvalue())

    def test_dev_without_action_prints_usage(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli._run_dev_command(None)
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck dev <setup|clean>", buf.getvalue())

    def test_dev_unknown_action_prints_usage(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli._run_dev_command("explode")
        self.assertEqual(code, 2)
        self.assertIn("Unknown dev action", buf.getvalue())

    def test_console_entrypoint(self):
        ensure_sandbox_dir()
        code = cli.clean_sandbox_entrypoint()
        self.assertEqual(code, 0)
        self.assertFalse(sandbox_root().exists())

    def test_main_dev_clean_dispatch(self):
        """`ck dev clean` via the full main() argument path."""
        ensure_sandbox_dir()
        code = cli.main(["dev", "clean"])
        self.assertEqual(code, 0)
        self.assertFalse(sandbox_root().exists())

    def test_main_dev_bare_prints_usage(self):
        code = cli.main(["dev"])
        self.assertEqual(code, 2)

    def _run_ck_clean(self) -> "subprocess.CompletedProcess":
        """Run the real ./ck-clean wrapper as a subprocess.

        The child env explicitly carries the isolated sandbox anchor
        (pinned by the conftest fixture), so the wrapper removes the
        per-test temp sandbox — never the repository's ``.sandbox/``.
        """
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("CK_SANDBOX", "CK_DEV", "CK_DEBUG")
        }
        env["CK_SANDBOX_ROOT"] = str(sandbox_root())
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "ck-clean")],
            capture_output=True, text=True, timeout=30, env=env,
        )

    def test_ck_clean_executable_script(self):
        """The ./ck-clean wrapper runs end-to-end as a subprocess."""
        ensure_sandbox_dir()
        (sandbox_root() / "dev.log").write_text("x", encoding="utf-8")
        result = self._run_ck_clean()
        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "Sandbox environment cleared successfully.", result.stdout
        )
        self.assertFalse(sandbox_root().exists())

    def test_ck_clean_executable_when_missing(self):
        clean_sandbox(quiet=True)
        result = self._run_ck_clean()
        self.assertEqual(result.returncode, 0)
        self.assertIn("nothing to clean", result.stdout)


if __name__ == "__main__":
    unittest.main()
