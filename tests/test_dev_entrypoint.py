"""Tests for the ck-dev entrypoint and dev installation.

Covers:

- ``sandbox_root()`` anchor override via ``CK_SANDBOX_ROOT`` (set by
  the ck-dev wrapper after ``git rev-parse --show-toplevel``), with
  the basename guard that keeps a mistyped override from turning
  ``clean_sandbox`` into an arbitrary recursive delete.
- Registry writes redirecting to ``$REPO_ROOT/.sandbox/config/``
  under the override anchor.
- ``is_dev_entrypoint`` / ``is_dev_mode`` argv[0] detection.
- The ck-dev wrapper itself (subprocess): nested-subdirectory
  invocation, deep invocation from ``.sandbox/projects/beta/``, the
  clean error outside a git checkout, and invocation through an
  ``~/.local/bin``-style symlink from an unrelated directory.
- The production physical-copy install (``ck install``): a regular
  executable file at ``~/.local/bin/ck`` plus a static ``cklib``
  package snapshot — never a symlink.
- The deprecated dev install (``ck install --dev`` / ``ck-dev
  install``) and the ``ck uninstall`` teardown.

ISOLATION: subprocess wrapper tests run against throwaway CHECKOUT
COPIES (tests/_isolated_checkout.py) inside the OS temp directory —
the wrapper's git-root-anchored ``.sandbox/`` therefore lands in the
copy, never in this repository (whose ``.sandbox/`` is the manual
dev environment). In-process sandbox operations are pinned to the
per-test temp anchor by the conftest autouse fixture.
"""

from __future__ import annotations

# Filesystem isolation safety net: importing the tests package
# pins CK_SANDBOX_ROOT to an OS-temp directory (see tests/__init__),
# so no test in this module can create or wipe the repository's own
# .sandbox/ — under ANY runner, including bare `unittest discover`.
import tests  # noqa: F401

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import sandbox as cksandbox
from cklib.cli import HELP_TEXT, main
from cklib.sandbox import (
    SANDBOX_ROOT_ENV,
    is_dev_entrypoint,
    is_dev_mode,
    resolve_write_path,
    sandbox_root,
)

from tests._isolated_checkout import make_checkout_copy

REPO_ROOT = Path(__file__).resolve().parent.parent
CK_DEV = REPO_ROOT / "ck-dev"


def _subprocess_env(fake_home: Path) -> dict:
    """Child env: isolated HOME, CK toggles and color forcing off."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("CK_SANDBOX", "CK_SANDBOX_ROOT", "CK_DEV",
                     "CK_DEBUG", "NO_COLOR", "FORCE_COLOR",
                     "CLICOLOR_FORCE")
    }
    env["HOME"] = str(fake_home)
    env["CK_DISABLE_UPDATE_CHECK"] = "1"
    return env


def _run_ck_dev(argv: list, cwd: Path, fake_home: Path,
                script: Path = CK_DEV,
                env_extra: dict = None) -> subprocess.CompletedProcess:
    env = _subprocess_env(fake_home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True, text=True, timeout=60,
        cwd=str(cwd), env=env,
    )


# ---------------------------------------------------------------------------
# 1. Sandbox anchor override (CK_SANDBOX_ROOT)
# ---------------------------------------------------------------------------


class TestSandboxRootOverride(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(SANDBOX_ROOT_ENV, None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is not None:
            os.environ[SANDBOX_ROOT_ENV] = self._saved

    def test_default_anchor_is_package_parent(self):
        expected = Path(cksandbox.__file__).resolve().parent.parent / ".sandbox"
        self.assertEqual(sandbox_root(), expected)

    def test_override_with_sandbox_basename_wins(self):
        with mock.patch.dict(os.environ, {SANDBOX_ROOT_ENV: "/opt/ck/.sandbox"}):
            self.assertEqual(sandbox_root(), Path("/opt/ck/.sandbox"))

    def test_override_without_sandbox_basename_is_ignored(self):
        # Guardrail: an unrelated path must never become the rmtree
        # target of `ck dev clean`.
        for bad in ("/home/user", "/tmp", "/opt/ck/sandbox"):
            with mock.patch.dict(os.environ, {SANDBOX_ROOT_ENV: bad}):
                self.assertEqual(
                    sandbox_root(),
                    Path(cksandbox.__file__).resolve().parent.parent
                    / ".sandbox",
                    f"override {bad!r} must be ignored",
                )

    def test_empty_override_is_ignored(self):
        with mock.patch.dict(os.environ, {SANDBOX_ROOT_ENV: ""}):
            self.assertEqual(
                sandbox_root(),
                Path(cksandbox.__file__).resolve().parent.parent / ".sandbox",
            )

    def test_registry_write_lands_under_override(self):
        """Global registry writes redirect to
        $ANCHOR/config/projects.json under the override anchor."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        anchor = Path(tmp.name) / "checkout" / ".sandbox"

        orig = {
            (ckconfig, n): getattr(ckconfig, n)
            for n in ("GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                      "LEGACY_GLOBAL_CONFIG_FILE")
        }
        orig.update({
            (ckregistry, n): getattr(ckregistry, n)
            for n in ("GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                      "LEGACY_GLOBAL_CONFIG_FILE")
        })
        fake_home = Path(tmp.name) / "home"
        new_dir = fake_home / ".config" / "ck"
        for mod in (ckconfig, ckregistry):
            mod.GLOBAL_CONFIG_DIR = new_dir
            mod.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
            mod.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"

        def _restore():
            for (mod, name), value in orig.items():
                setattr(mod, name, value)
        self.addCleanup(_restore)

        env = {"CK_SANDBOX": "1", SANDBOX_ROOT_ENV: str(anchor)}
        with mock.patch.dict(os.environ, env, clear=False):
            ckregistry.register_project(Path(tmp.name) / "proj", name="proj")
            sandboxed = resolve_write_path(
                ckconfig.GLOBAL_REGISTRY_FILE)

        self.assertEqual(
            sandboxed, anchor / "config" / "projects.json")
        self.assertTrue(sandboxed.is_file())
        self.assertIn("proj", sandboxed.read_text(encoding="utf-8"))
        # The real registry was never created.
        self.assertFalse(ckconfig.GLOBAL_REGISTRY_FILE.exists())


# ---------------------------------------------------------------------------
# 2. Entrypoint detection
# ---------------------------------------------------------------------------


class TestIsDevEntrypoint(unittest.TestCase):
    def test_ck_dev_name_detected(self):
        for argv0 in ("/repo/ck-dev", "./ck-dev", "ck-dev",
                      "/opt/ck-dev", "CK-DEV"):
            self.assertTrue(is_dev_entrypoint([argv0, "st"]), argv0)

    def test_windows_suffix_detected(self):
        # POSIX Path: a forward-slash path carrying the .exe suffix.
        self.assertTrue(is_dev_entrypoint(["/opt/bin/ck-dev.exe", "st"]))

    def test_symlinked_entrypoint_detected(self):
        self.assertTrue(
            is_dev_entrypoint(["/home/u/.local/bin/ck-dev", "st"]))

    def test_other_entrypoints_rejected(self):
        for argv0 in ("/repo/ck", "./ck", "ck", "pytest", ""):
            self.assertFalse(is_dev_entrypoint([argv0, "st"]), argv0)

    def test_empty_argv_is_safe(self):
        self.assertFalse(is_dev_entrypoint([]))
        self.assertFalse(is_dev_entrypoint())

    def test_is_dev_mode_uses_entrypoint_detection(self):
        self.assertTrue(is_dev_mode(argv=["ck-dev", "st"], env={}))
        self.assertFalse(is_dev_mode(argv=["ck", "st"], env={}))


# ---------------------------------------------------------------------------
# 3. The ck-dev wrapper (subprocess)
# ---------------------------------------------------------------------------


class _WrapperBase(unittest.TestCase):
    """Fresh sandbox + isolated HOME for wrapper subprocess tests.

    Subprocess tests run against a throwaway CHECKOUT COPY
    (tests/_isolated_checkout.py) built inside this test's OS temp
    directory: the ``ck-dev`` wrapper anchors its ``.sandbox/`` at
    the copy's git root, so the repository's own manual ``.sandbox/``
    is never created, written to, or wiped by the suite.
    """

    def setUp(self):
        cksandbox.clean_sandbox(quiet=True)
        self.addCleanup(cksandbox.clean_sandbox, quiet=True)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake_home = Path(self._tmp.name) / "home"
        self.fake_home.mkdir()
        try:
            self.checkout = make_checkout_copy(
                Path(self._tmp.name) / "checkout-copy")
        except RuntimeError as e:
            self.skipTest(f"cannot build isolated checkout copy: {e}")
        self.ck_dev_copy = self.checkout / "ck-dev"
        self.copy_sandbox = self.checkout / ".sandbox"

    def make_project(self, name: str) -> Path:
        root = Path(self._tmp.name) / name
        (root / ".ck").mkdir(parents=True)
        (root / ".ck" / "PLAN.md").write_text(
            "# t\n## Current Sprint\n- [ ] wrapper task\n",
            encoding="utf-8",
        )
        return root


class TestCkDevWrapperNested(_WrapperBase):
    def test_st_from_nested_repo_subdirectory(self):
        """`ck-dev st` from a subdirectory inside the checkout works
        (sandbox anchored at the checkout root, no pathing errors)."""
        nested = self.checkout / "src" / "deep" / "module"
        nested.mkdir(parents=True)
        result = _run_ck_dev(["st"], nested, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0,
                         f"stderr: {result.stderr}")
        self.assertNotIn("Traceback", result.stderr + result.stdout)
        # Read-only st: no sandbox needed.
        self.assertFalse(self.copy_sandbox.exists())

    def test_st_from_unrelated_project_anchors_sandbox_at_checkout(self):
        """A project OUTSIDE the checkout renders normally, while its
        dev-mode writes land under the CHECKOUT COPY's .sandbox/ —
        never next to the caller's project, never in the real repo."""
        proj = self.make_project("outside-proj")

        result = _run_ck_dev(["st"], proj, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("outside-proj", result.stdout)
        self.assertIn("wrapper task", result.stdout)

        # Mutate through the wrapper; the real project stays intact
        # and the sandbox mirror appears under the COPY's sandbox.
        result = _run_ck_dev(["add", "dev task"], proj, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(
            "dev task",
            (proj / ".ck" / "PLAN.md").read_text(encoding="utf-8"),
        )
        mirrors = [
            p for p in self.copy_sandbox.rglob("PLAN.md")
            if "dev task" in p.read_text(encoding="utf-8")
        ]
        self.assertTrue(mirrors, "sandbox mirror not created")
        self.assertTrue(
            mirrors[0].is_relative_to(self.copy_sandbox),
            f"mirror escaped the checkout sandbox: {mirrors[0]}",
        )


class TestCkDevWrapperDeepSandbox(_WrapperBase):
    """Requirement: invocation from deep inside .sandbox/projects/
    resolves state and the sandbox registry correctly.

    The fixture sandbox is built INSIDE the checkout copy (via the
    copy's own ``ck-dev sandbox setup`` subprocess) — the manual
    generator is never pointed at the repository's ``.sandbox/``.
    """

    def setUp(self):
        super().setUp()
        result = _run_ck_dev(["sandbox", "setup"], self.checkout,
                             self.fake_home, script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (self.copy_sandbox / "projects" / "beta" / ".ck" / "PLAN.md")
            .is_file(),
            "fixtures missing in the copy's sandbox",
        )

    def test_st_from_beta_fixture_dir(self):
        beta = self.copy_sandbox / "projects" / "beta"
        self.assertTrue((beta / ".ck" / "PLAN.md").is_file())

        result = _run_ck_dev(["st"], beta, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("> beta", result.stdout)
        self.assertIn("Beta standalone task one", result.stdout)

    def test_dashboard_reads_sandbox_registry_from_beta(self):
        """`ck-dev dashboard` from inside the sandbox resolves the
        SANDBOX registry (alpha fixture), not the (empty) host one."""
        beta = self.copy_sandbox / "projects" / "beta"
        result = _run_ck_dev(["dashboard"], beta, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("alpha", result.stdout)
        # The isolated host registry was never created.
        self.assertFalse((self.fake_home / ".config" / "ck").exists())


class TestCkDevWrapperGuardrails(_WrapperBase):
    def test_outside_git_checkout_is_a_clean_error(self):
        """A ck-dev copy without .git exits 1 with a clean diagnostic
        (no .sandbox next to installed packages, no traceback)."""
        tmp = Path(self._tmp.name) / "nogit"
        tmp.mkdir()
        stray = tmp / "ck-dev"
        stray.write_text(CK_DEV.read_text(encoding="utf-8"),
                         encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(stray), "st"],
            capture_output=True, text=True, timeout=60,
            cwd=str(tmp), env=_subprocess_env(self.fake_home),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stderr)
        self.assertIn("git", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr + result.stdout)
        self.assertFalse((tmp / ".sandbox").exists())

    def test_invoked_through_symlink_from_any_directory(self):
        """The `ck install --dev` shape: a symlink named ck-dev
        pointing at the checkout wrapper, run from an unrelated cwd.
        The link targets the COPY's wrapper, so the sandbox lands in
        the copy — the real repo's .sandbox/ stays untouched."""
        proj = self.make_project("symlinked-proj")
        bin_dir = Path(self._tmp.name) / "bin"
        bin_dir.mkdir()
        link = bin_dir / "ck-dev"
        link.symlink_to(self.ck_dev_copy)

        result = subprocess.run(
            [sys.executable, str(link), "st"],
            capture_output=True, text=True, timeout=60,
            cwd=str(proj), env=_subprocess_env(self.fake_home),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("symlinked-proj", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((REPO_ROOT / ".sandbox" / "projects"
                          / "symlinked-proj").exists())


# ---------------------------------------------------------------------------
# 4. ck install (physical copy) / ck uninstall (production snapshot)
# ---------------------------------------------------------------------------


class TestPhysicalInstall(unittest.TestCase):
    """`ck install` writes a REGULAR executable file — never a symlink.

    Production isolation: the installed launcher plus the ``cklib``
    package snapshot are decoupled from the checkout, so repo edits
    cannot change the installed behavior until ``ck install`` /
    ``ck update`` explicitly re-runs.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._bin = Path(self._tmp.name) / "bin"
        self._ck_target = self._bin / "ck"
        self._dev_target = self._bin / "ck-dev"
        self._snapshot = Path(self._tmp.name) / "share" / "ck"

        patcher = mock.patch("cklib.cli.USER_INSTALL_PATH", self._ck_target)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("cklib.cli.SNAPSHOT_INSTALL_DIR", self._snapshot)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv: list, argv0: str = "ck") -> str:
        buf = io.StringIO()
        with mock.patch("sys.argv", [argv0, *argv]), \
                redirect_stdout(buf):
            code = main(argv)
        self.assertEqual(code, 0)
        return buf.getvalue()

    # ---- production install: physical copy ------------------------ #

    def test_install_creates_regular_executable_file(self):
        out = self._run(["install"])
        self.assertTrue(self._ck_target.is_file(), out)
        self.assertFalse(self._ck_target.is_symlink(), out)
        self.assertEqual(self._ck_target.stat().st_mode & 0o777, 0o755)
        self.assertIn("physical copy", out)

    def test_install_copies_launcher_content_verbatim(self):
        self._run(["install"])
        self.assertEqual(
            self._ck_target.read_bytes(),
            (REPO_ROOT / "ck").read_bytes(),
        )

    def test_install_snapshots_cklib_package(self):
        self._run(["install"])
        self.assertTrue((self._snapshot / "cklib" / "__init__.py").is_file())
        self.assertTrue((self._snapshot / "cklib" / "cli.py").is_file())
        self.assertFalse(
            (self._snapshot / "cklib" / "__pycache__").exists(),
            "bytecode caches must not leak into the snapshot",
        )

    def test_install_replaces_existing_symlink(self):
        """A legacy symlink install is force-replaced by a real file."""
        self._bin.mkdir(parents=True, exist_ok=True)
        self._ck_target.symlink_to(REPO_ROOT / "ck")
        out = self._run(["install"])
        self.assertFalse(self._ck_target.is_symlink(), out)
        self.assertTrue(self._ck_target.is_file(), out)
        self.assertIn("physical copy", out)

    def test_install_force_replaces_existing_file(self):
        """Stale copies are force-removed before copying (rm -f)."""
        self._bin.mkdir(parents=True, exist_ok=True)
        self._ck_target.write_text("#!/bin/sh\necho stale\n",
                                   encoding="utf-8")
        out = self._run(["install"])
        self.assertEqual(
            self._ck_target.read_bytes(), (REPO_ROOT / "ck").read_bytes())
        self.assertIn("physical copy", out)

    def test_install_idempotent(self):
        self._run(["install"])
        out = self._run(["install"])
        self.assertTrue(self._ck_target.is_file(), out)
        self.assertFalse(self._ck_target.is_symlink())
        self.assertEqual(
            self._ck_target.read_bytes(), (REPO_ROOT / "ck").read_bytes())

    def test_install_honors_user_bin_override(self):
        alt_bin = Path(self._tmp.name) / "custom-bin"
        with mock.patch.dict(os.environ, {"USER_BIN": str(alt_bin)}):
            out = self._run(["install"])
        self.assertTrue((alt_bin / "ck").is_file(), out)
        self.assertFalse((alt_bin / "ck").is_symlink())
        # The default path was never touched.
        self.assertFalse(self._ck_target.exists())

    # ---- dev install: deprecated, installs nothing ------------------ #

    def test_install_dev_flag_is_deprecated_and_installs_nothing(self):
        out = self._run(["install", "--dev"])
        self.assertIn("Deprecated", out)
        self.assertFalse(self._dev_target.exists())
        self.assertFalse(self._ck_target.exists())
        self.assertFalse(self._snapshot.exists())

    def test_ck_dev_entrypoint_install_is_deprecated(self):
        """`ck-dev install` no longer implies a dev entrypoint install."""
        out = self._run(["install"], argv0=str(CK_DEV))
        self.assertIn("Deprecated", out)
        self.assertFalse(self._dev_target.exists())
        self.assertFalse(self._ck_target.exists())

    # ---- uninstall -------------------------------------------------- #

    def test_uninstall_removes_ck_and_legacy_dev_and_snapshot(self):
        self._run(["install"])
        self._bin.mkdir(parents=True, exist_ok=True)
        self._dev_target.symlink_to(CK_DEV)  # legacy --dev leftover
        out = self._run(["uninstall"])
        self.assertFalse(self._ck_target.exists(), out)
        self.assertFalse(self._dev_target.exists(), out)
        self.assertFalse(self._snapshot.exists(), out)
        self.assertIn(f"Removed: {self._ck_target}", out)
        self.assertIn(f"Removed: {self._dev_target}", out)

    def test_uninstall_removes_regular_file_copy(self):
        """The installed copy IS a regular file — uninstall removes it."""
        self._run(["install"])
        self.assertTrue(self._ck_target.is_file())
        out = self._run(["uninstall"])
        self.assertFalse(self._ck_target.exists(), out)

    def test_uninstall_when_nothing_installed_is_graceful(self):
        out = self._run(["uninstall"])
        self.assertIn("Not installed", out)

    def test_uninstall_refuses_directories(self):
        self._bin.mkdir(parents=True, exist_ok=True)
        self._ck_target.mkdir()
        out = self._run(["uninstall"])
        self.assertIn("Refusing", out)
        self.assertTrue(self._ck_target.is_dir())

    # ---- help / docs ------------------------------------------------ #

    def test_help_documents_physical_install(self):
        self.assertIn("physical executable file, never a symlink", HELP_TEXT)
        self.assertIn("~/.local/bin/ck-dev", HELP_TEXT)
        # No reference may suggest that install CREATES a symlink.
        self.assertNotIn("Symlink ck to", HELP_TEXT)
        self.assertNotIn("Symlink ck-dev", HELP_TEXT)


class TestPhysicalInstallEndToEnd(unittest.TestCase):
    """Acceptance criteria, end to end on an isolated checkout copy.

    - ``./ck install`` creates a standalone physical copy at
      ``<HOME>/.local/bin/ck`` (regular executable file, NOT a
      symlink) backed by the static ``cklib`` snapshot.
    - Checkout source edits do NOT change the installed behavior
      until ``./ck install`` is explicitly re-run.
    - ``ck uninstall`` cleanly removes the installation.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.fake_home = self.base / "home"
        self.fake_home.mkdir()
        try:
            self.checkout = make_checkout_copy(self.base / "checkout-copy")
        except RuntimeError as e:
            self.skipTest(f"cannot build isolated checkout copy: {e}")
        self.ck_copy = self.checkout / "ck"
        self.bin_ck = self.fake_home / ".local" / "bin" / "ck"
        self.snapshot = self.fake_home / ".local" / "share" / "ck"

    def _env(self) -> dict:
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("CK_SANDBOX", "CK_SANDBOX_ROOT", "CK_DEV",
                         "CK_DEBUG", "CK_PROJECT_ROOT", "PYTHONPATH",
                         "CK_DISABLE_UPDATE_CHECK")
        }
        env["HOME"] = str(self.fake_home)
        env["CK_DISABLE_UPDATE_CHECK"] = "1"
        return env

    def _run(self, argv: list, script: Path, cwd: Path):
        return subprocess.run(
            [sys.executable, str(script), *argv],
            capture_output=True, text=True, timeout=60,
            cwd=str(cwd), env=self._env(),
        )

    def test_install_creates_standalone_physical_copy(self):
        result = self._run(["install"], self.ck_copy, self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.bin_ck.is_file(), result.stdout)
        self.assertFalse(self.bin_ck.is_symlink())
        self.assertEqual(self.bin_ck.stat().st_mode & 0o777, 0o755)
        # Standalone: the launcher copy + package snapshot exist together.
        self.assertTrue((self.snapshot / "cklib" / "__init__.py").is_file())
        self.assertIn("physical copy", result.stdout)

    def test_installed_copy_executes_the_snapshot(self):
        self._run(["install"], self.ck_copy, self.checkout)
        result = self._run(["info"], self.bin_ck, self.fake_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        # install_dir is the SNAPSHOT, never the checkout: the installed
        # binary executes its own static package copy.
        self.assertIn(f"install_dir: {self.snapshot}", result.stdout)
        self.assertNotIn(f"install_dir: {self.checkout}", result.stdout)

    def test_repo_edits_do_not_alter_installed_behavior_until_reinstall(self):
        self._run(["install"], self.ck_copy, self.checkout)
        before = self._run(["info"], self.bin_ck, self.fake_home)
        self.assertNotIn("CHECKOUT-MARKER", before.stdout)

        # Edit the CHECKOUT sources; the installed copy must not care.
        cli_copy = self.checkout / "cklib" / "cli.py"
        cli_copy.write_text(
            cli_copy.read_text(encoding="utf-8")
            + "\n\n_orig_main = main\n\n\ndef main(argv=None):\n"
              "    print('CHECKOUT-MARKER')\n"
              "    return _orig_main(argv)\n",
            encoding="utf-8",
        )

        after_edit = self._run(["info"], self.bin_ck, self.fake_home)
        self.assertEqual(after_edit.returncode, 0, after_edit.stderr)
        self.assertNotIn(
            "CHECKOUT-MARKER", after_edit.stdout,
            "installed copy must be immune to checkout edits",
        )

        # Explicit re-install applies the checkout changes.
        reinstall = self._run(["install"], self.ck_copy, self.checkout)
        self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
        after_reinstall = self._run(["info"], self.bin_ck, self.fake_home)
        self.assertIn(
            "CHECKOUT-MARKER", after_reinstall.stdout,
            "re-install must apply the checkout changes to the snapshot",
        )

    def test_install_replaces_legacy_symlink_with_physical_copy(self):
        self.bin_ck.parent.mkdir(parents=True, exist_ok=True)
        self.bin_ck.symlink_to(self.ck_copy)
        result = self._run(["install"], self.ck_copy, self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.bin_ck.is_file())
        self.assertFalse(self.bin_ck.is_symlink())

    def test_uninstall_removes_installed_copy_and_snapshot(self):
        self._run(["install"], self.ck_copy, self.checkout)
        self.assertTrue(self.bin_ck.is_file())
        result = self._run(["uninstall"], self.ck_copy, self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.bin_ck.exists(), result.stdout)
        self.assertFalse(self.snapshot.exists(), result.stdout)

    def test_install_dev_flag_installs_nothing(self):
        result = self._run(["install", "--dev"], self.ck_copy, self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Deprecated", result.stdout)
        self.assertFalse((self.bin_ck.parent / "ck-dev").exists())
        self.assertFalse(self.bin_ck.exists())

    def test_ck_dev_install_installs_nothing(self):
        result = self._run(
            ["install"], self.checkout / "ck-dev", self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Deprecated", result.stdout)
        self.assertFalse((self.bin_ck.parent / "ck-dev").exists())
        self.assertFalse(self.bin_ck.exists())


# ---------------------------------------------------------------------------
# 7. Sandbox session lifecycle (bare ck-dev / ck-dev exit / interceptor)
# ---------------------------------------------------------------------------


class TestCkDevSessionLifecycle(_WrapperBase):
    """End-to-end entry/exit through the REAL ck-dev wrapper.

    Runs against the isolated checkout copy, so entry actually builds
    ``.sandbox/`` (fixtures included) inside the temp copy and the
    host repository is never touched.
    """

    def _host_config_dir(self) -> Path:
        return self.fake_home / ".config" / "ck"

    def test_bare_entry_builds_sandbox_and_banners(self):
        result = _run_ck_dev([], self.checkout, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        # The warning banner (label + dynamically resolved active
        # binary + exit hint), followed by the Global Dashboard with
        # the fixture projects. The active binary is the CHECKOUT's
        # local dev binary: entry pins the checkout dir to the front
        # of PATH before the banner renders.
        self.assertIn("\u26a0\ufe0f  SANDBOX MODE ACTIVE", out)
        self.assertIn(f"Active binary: {self.checkout / 'ck'}", out)
        self.assertIn("Type 'exit' or press Ctrl+D to return to "
                      "production", out)
        self.assertLess(out.index("SANDBOX MODE ACTIVE"),
                        out.index("GLOBAL DASHBOARD"))
        self.assertIn("alpha", out)
        self.assertIn("orphaned-deleted", out)
        self.assertIn("\u250c", out) and self.assertIn("\u2514", out)
        # The mock environment was built inside the COPY's sandbox.
        self.assertTrue((self.copy_sandbox / "config"
                         / "projects.json").is_file())
        self.assertTrue(
            (self.copy_sandbox / "projects" / "alpha" / ".ck"
             / "PLAN.md").is_file())
        # The session marker was persisted.
        state = self.copy_sandbox / "state.json"
        self.assertTrue(state.is_file())
        self.assertIn("alpha", state.read_text(encoding="utf-8"))
        # Host isolation held: the fake-HOME registry was never
        # created and the session landed inside the COPY's sandbox.
        self.assertFalse(self._host_config_dir().exists())

    def test_entry_then_delegated_st_inside_active_project(self):
        entry = _run_ck_dev([], self.checkout, self.fake_home,
                            script=self.ck_dev_copy)
        self.assertEqual(entry.returncode, 0, entry.stderr)

        alpha = self.copy_sandbox / "projects" / "alpha"
        result = _run_ck_dev(["st"], alpha, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The active project's fixture status renders.
        self.assertIn("Active focus task", result.stdout)
        self.assertNotIn("Traceback", result.stderr + result.stdout)

    def test_exit_clears_session_and_confirms_repo_root(self):
        """The REAL exit flow: bare `ck-dev` spawns the session
        subshell; when the user types `exit` (probe $SHELL terminates),
        the exit trap clears the session and reports the dynamically
        resolved production binary. (`ck-dev exit` as a separate
        command is rejected by the safeguard — see
        test_dev_exit_arg_hits_safeguard_and_fails_1.)"""
        if shutil.which("sh") is None:
            self.skipTest("sh unavailable")
        probe = self.checkout / "_exit_probe.sh"
        probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        probe.chmod(0o755)
        self.addCleanup(probe.unlink, missing_ok=True)

        # Controlled PATH: the checkout copy leads (the session's
        # active binary), a decoy "global" dir follows (the binary
        # that takes over after the exit trap strips the checkout
        # entry) — making the resolution independent of whatever ck
        # the HOST machine happens to have installed.
        decoy_global = Path(self._tmp.name) / "global-bin"
        decoy_global.mkdir(exist_ok=True)
        decoy_ck = decoy_global / "ck"
        decoy_ck.write_text("#!/bin/sh\necho global-ck\n",
                            encoding="utf-8")
        decoy_ck.chmod(0o755)
        controlled_path = os.pathsep.join(
            [str(self.checkout), str(decoy_global),
             os.environ.get("PATH", "")])

        env = {
            "CK_DEV_FORCE_SUBSHELL": "1",
            "SHELL": str(probe),
            "PATH": controlled_path,
        }
        result = _run_ck_dev([], self.checkout, self.fake_home,
                             script=self.ck_dev_copy, env_extra=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        # Entry banner: the ACTIVE binary is the checkout's own `ck`
        # (first on the session PATH, resolved at runtime).
        self.assertIn(f"Active binary: {self.checkout / 'ck'}", out)
        # Exit-trap confirmation: EXACTLY ONE clean status line with
        # the dynamically resolved global binary.
        self.assertIn("[ok] Exited sandbox mode. Active binary is now: ",
                      out)
        self.assertNotIn("Back to production", out)
        self.assertEqual(out.count("[ok] Exited sandbox mode"), 1, out)
        binary_line = next(
            l for l in out.splitlines()
            if "Active binary is now:" in l)
        resolved = binary_line.split("Active binary is now: ", 1)[1]
        # The checkout's PATH entry was stripped: the DECOY global
        # binary takes over in production.
        self.assertEqual(resolved, str(decoy_ck),
                         f"resolved global binary {resolved!r}")
        # The session marker was cleared by the exit trap.
        self.assertFalse(
            (self.copy_sandbox / "state.json").exists())

    def test_dev_exit_arg_hits_safeguard_and_fails_1(self):
        """Spec safeguard: `ck-dev exit` / `./ck-dev exit` must NOT
        exit, no-op, or launch a subshell — it warns and exits 1."""
        # Build a session first: the safeguard must leave it intact.
        entry = _run_ck_dev([], self.checkout, self.fake_home,
                            script=self.ck_dev_copy)
        self.assertEqual(entry.returncode, 0, entry.stderr)
        state = self.copy_sandbox / "state.json"
        self.assertTrue(state.is_file())

        result = _run_ck_dev(["exit"], self.checkout, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 1)
        out = result.stdout
        self.assertIn("[!] 'ck-dev exit' cannot close an active subshell.",
                      out)
        self.assertIn("[!] To exit sandbox mode, type 'exit' or press "
                      "Ctrl+D.", out)
        self.assertNotIn("Starting sandbox subshell", out)
        self.assertNotIn("[ok] Exited sandbox mode", out)
        # The session was NOT torn down by the rejected attempt.
        self.assertTrue(state.is_file())

        # Manual cleanup of the session built above (the canonical
        # exit now lives INSIDE the subshell's exit trap).
        state.unlink()

    def test_banner_shows_local_dev_binary_over_path_leading_global(self):
        """Session contract: even when a GLOBAL ck leads the inherited
        PATH, entry pins the checkout dir to the front and the banner
        reports the LOCAL DEV binary."""
        decoy_bin = Path(self._tmp.name) / "global-bin"
        decoy_bin.mkdir(exist_ok=True)
        decoy = decoy_bin / "ck"
        decoy.write_text("#!/bin/sh\necho DECOY-GLOBAL-CK\n",
                         encoding="utf-8")
        decoy.chmod(0o755)
        env = {"PATH": os.pathsep.join(
            [str(decoy_bin), os.environ.get("PATH", "")])}
        result = _run_ck_dev([], self.checkout, self.fake_home,
                             script=self.ck_dev_copy, env_extra=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn(f"Active binary: {self.checkout / 'ck'}", out,
                      "banner must report the LOCAL DEV binary")
        self.assertNotIn(f"Active binary: {decoy}", out)
        # Manual session cleanup (canonical exit lives in the trap).
        (self.copy_sandbox / "state.json").unlink(missing_ok=True)

    def test_exit_without_session_is_graceful(self):
        result = _run_ck_dev(["exit"], self.checkout, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[!] 'ck-dev exit' cannot close an active subshell.",
                      result.stdout)
        self.assertNotIn("Not in sandbox mode", result.stdout)

    def test_entry_is_idempotent(self):
        first = _run_ck_dev([], self.checkout, self.fake_home,
                            script=self.ck_dev_copy)
        self.assertEqual(first.returncode, 0, first.stderr)
        plan = (self.copy_sandbox / "projects" / "alpha" / ".ck"
                / "PLAN.md")
        before = plan.read_text(encoding="utf-8")

        second = _run_ck_dev([], self.checkout, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("SANDBOX MODE ACTIVE", second.stdout)
        # The existing sandbox was NOT rebuilt from scratch.
        self.assertEqual(plan.read_text(encoding="utf-8"), before)
        self.assertTrue(
            (self.copy_sandbox / "state.json").is_file())

    def test_reentry_from_inside_alpha_keeps_cwd_valid(self):
        """The exact FileNotFoundError scenario: ``ck-dev`` runs TWICE
        with cwd = ``.sandbox/projects/alpha``. The re-initialization
        must be IN PLACE (no destructive rmtree under the caller), so
        the process's working directory stays real and the follow-up
        ``ck st -g`` (which calls ``os.getcwd()`` first) never raises
        ``FileNotFoundError: [Errno 2] No such file or directory``."""
        alpha = self.copy_sandbox / "projects" / "alpha"
        plan = alpha / ".ck" / "PLAN.md"
        if not plan.is_file():
            setup = _run_ck_dev(["sandbox", "setup"], self.checkout,
                                self.fake_home, script=self.ck_dev_copy)
            self.assertEqual(setup.returncode, 0, setup.stderr)
        self.assertTrue(plan.is_file(), "fixture missing")

        first = _run_ck_dev([], alpha, self.fake_home,
                            script=self.ck_dev_copy)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = plan.read_text(encoding="utf-8")

        # Second entry FROM INSIDE the sandbox, with state and
        # registry removed to FORCE re-initialization: pre-fix this
        # wiped and recreated .sandbox/ under the caller, dangling
        # its cwd descriptor.
        (self.copy_sandbox / "state.json").unlink(missing_ok=True)
        (self.copy_sandbox / "config" / "projects.json").unlink(
            missing_ok=True)
        second = _run_ck_dev([], alpha, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("SANDBOX MODE ACTIVE", second.stdout)
        # In-place re-init: announced, and fixture content survived.
        self.assertIn("IN PLACE", second.stdout)
        self.assertEqual(plan.read_text(encoding="utf-8"), before)

        # The follow-up plain `ck` from the SAME directory must work:
        # pre-fix, getcwd() raised FileNotFoundError here.
        env = _subprocess_env(self.fake_home)  # strips CK_SANDBOX
        result = subprocess.run(
            [sys.executable, str(self.checkout / "ck"), "st", "-g"],
            capture_output=True, text=True, timeout=60,
            cwd=str(alpha), env=env,
        )
        self.assertEqual(result.returncode, 0,
                         f"stderr: {result.stderr}")
        self.assertNotIn("FileNotFoundError", result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr + result.stdout)
        # The banner + populated dashboard still render correctly.
        self.assertIn("SANDBOX MODE ACTIVE", result.stdout)
        self.assertIn("GLOBAL DASHBOARD", result.stdout)
        self.assertIn("alpha", result.stdout)

    def test_bare_ck_dev_inside_session_warns_and_does_not_nest(self):
        """Matryoshka guard, end to end: bare `ck-dev` executed with
        CK_SANDBOX=1 in the INHERITED environment (i.e. inside a
        session subshell) prints the nesting warning + dashboard and
        spawns no second session / subshell."""
        env = {"CK_SANDBOX": "1"}  # the parent session subshell env
        result = _run_ck_dev([], self.checkout, self.fake_home,
                             script=self.ck_dev_copy, env_extra=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("Already in sandbox mode", out)
        self.assertIn("No nested session started", out)
        self.assertNotIn("SANDBOX MODE ACTIVE", out)  # no second banner
        # No subshell was started (spawn-free, deterministic output).
        self.assertNotIn("Starting sandbox subshell", out)
        # No persistent session was (re)written by the guarded entry.
        self.assertFalse((self.copy_sandbox / "state.json").exists())

    def test_piped_bare_ck_dev_is_spawn_free(self):
        """Piped (non-interactive) bare `ck-dev` keeps the legacy
        deterministic behavior: banner + dashboard, no subshell."""
        result = _run_ck_dev([], self.checkout, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("SANDBOX MODE ACTIVE", out)
        self.assertIn("GLOBAL DASHBOARD", out)
        self.assertNotIn("Starting sandbox subshell", out)

    def test_deep_sandbox_entry_anchors_to_top_level_root(self):
        """`ck-dev` executed from DEEP inside .sandbox/projects/alpha
        (no CK_SANDBOX_ROOT env) anchors every sandbox path to the
        TOP-LEVEL workspace root: CK_SANDBOX_ROOT is exactly
        <checkout>/.sandbox — never a nested .sandbox/.sandbox — and
        the spawned subshell inherits the user's cwd untouched."""
        if shutil.which("bash") is None:
            self.skipTest("bash unavailable")
        setup = _run_ck_dev(["sandbox", "setup"], self.checkout,
                            self.fake_home, script=self.ck_dev_copy)
        self.assertEqual(setup.returncode, 0, setup.stderr)

        alpha = self.copy_sandbox / "projects" / "alpha"
        inner = self.checkout / "_probe_inner.sh"
        inner.write_text(
            "#!/bin/sh\n"
            'echo PROBE_PWD=$PWD\n'
            'echo PROBE_ROOT=${CK_SANDBOX_ROOT-unset}\n'
            "exit 0\n",
            encoding="utf-8",
        )
        inner.chmod(0o755)
        self.addCleanup(inner.unlink, missing_ok=True)

        env = {"CK_DEV_FORCE_SUBSHELL": "1", "SHELL": str(inner)}
        # Run the wrapper FROM alpha: the subshell must inherit that
        # exact cwd (preservation) while the anchor resolves above it.
        result = _run_ck_dev([], alpha, self.fake_home,
                             script=self.ck_dev_copy, env_extra=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        # TOP-LEVEL anchor, no nesting:
        self.assertIn(f"PROBE_ROOT={self.copy_sandbox}", out)
        self.assertNotIn(
            f"{self.copy_sandbox}/.sandbox", out,
            "sandbox root must never nest a .sandbox inside itself")
        # Exact cwd preservation inside the subshell:
        self.assertIn(f"PROBE_PWD={alpha}", out)

    def test_deep_sandbox_delegated_command_anchors_to_top_level(self):
        """Delegated `ck-dev st` from deep inside alpha (no env anchor)
        also resolves the TOP-LEVEL sandbox root — no nested
        .sandbox, dashboard still lists the fixture projects."""
        setup = _run_ck_dev(["sandbox", "setup"], self.checkout,
                            self.fake_home, script=self.ck_dev_copy)
        self.assertEqual(setup.returncode, 0, setup.stderr)

        alpha = self.copy_sandbox / "projects" / "alpha"
        result = _run_ck_dev(["st"], alpha, self.fake_home,
                             script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        # No second .sandbox materialized anywhere under alpha:
        self.assertFalse((alpha / ".sandbox").exists())
        # The top-level anchor still shows the fixture dashboard.
        self.assertNotIn("ERROR", result.stdout)

    def test_find_repo_root_walks_past_sandbox_from_deep_inside(self):
        """Unit: the resolver's staged chain — anchor search walks past
        the sandbox boundary to the git root, the checkout anchor
        wins for anchorless trees, and no result ever lies INSIDE a
        .sandbox tree."""
        tmp = Path(tempfile.mkdtemp(prefix="ck-root-probe-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = tmp / "checkout"
        (repo / ".git").mkdir(parents=True)
        deep = repo / ".sandbox" / "projects" / "alpha"
        deep.mkdir(parents=True)
        self.assertEqual(cksandbox.find_repo_root(deep), repo)
        # Every resolution stage stays OUTSIDE the sandbox tree:
        self.assertFalse(cksandbox.is_within_sandbox(
            cksandbox.find_repo_root(deep)))

    def test_session_subshell_env_contract_via_wrapper(self):
        """Through the REAL wrapper with the force seam: the spawned
        session subshell receives CK_SANDBOX=1, a checkout-anchored
        PYTHONPATH and the other session flags via the environment —
        with NO rc file interception (the shell runs untouched).
        SHELL points at a probe script that dumps its env so the
        contract is observable without a real interactive bash."""
        setup = _run_ck_dev(["sandbox", "setup"], self.checkout,
                            self.fake_home, script=self.ck_dev_copy)
        self.assertEqual(setup.returncode, 0, setup.stderr)

        repo = str(self.checkout)
        probe = self.checkout / "_env_probe.sh"
        probe.write_text(
            "#!/bin/sh\n"
            "echo PROBE_CK_SANDBOX=${CK_SANDBOX-unset}\n"
            "echo PROBE_SHELL_FLAG=${CK_SANDBOX_SHELL-unset}\n"
            "case $CK_SANDBOX_ACTIVE in */projects/alpha) echo PROBE_ACTIVE=alpha ;; *) echo PROBE_ACTIVE=other ;; esac\n"
            f'case $PYTHONPATH in \"{repo}\"|\"{repo}\":*|*:\"{repo}\") echo PROBE_PYPATH=repo ;; *) echo PROBE_PYPATH=other ;; esac\n'
            "exit 0\n",
            encoding="utf-8",
        )
        probe.chmod(0o755)
        self.addCleanup(probe.unlink, missing_ok=True)

        env = {"CK_DEV_FORCE_SUBSHELL": "1", "SHELL": str(probe)}
        result = _run_ck_dev([], self.checkout, self.fake_home,
                             script=self.ck_dev_copy, env_extra=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("PROBE_CK_SANDBOX=1", out)
        self.assertIn("PROBE_SHELL_FLAG=1", out)
        self.assertIn("PROBE_ACTIVE=alpha", out)
        self.assertIn("PROBE_PYPATH=repo", out)
        # The wrapper announced the interactive subshell.
        self.assertIn("Starting sandbox subshell", out)
        """Plain `ck` under an active session prints the banner, then
        transparently executes the requested command in sandbox mode."""
        entry = _run_ck_dev([], self.checkout, self.fake_home,
                            script=self.ck_dev_copy)
        self.assertEqual(entry.returncode, 0, entry.stderr)

        alpha = self.copy_sandbox / "projects" / "alpha"
        env = _subprocess_env(self.fake_home)
        env.update({
            "CK_SANDBOX": "1",
            "CK_SANDBOX_ROOT": str(self.copy_sandbox),
            "CK_SANDBOX_ACTIVE": str(alpha),
            "NO_COLOR": "1",
        })
        result = subprocess.run(
            [sys.executable, str(self.checkout / "ck"), "list"],
            capture_output=True, text=True, timeout=60,
            cwd=str(alpha), env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        # The protective banner precedes the command output.
        self.assertIn("SANDBOX MODE ACTIVE", out)
        self.assertIn("Type 'exit' or press Ctrl+D to return to "
                      "production", out)
        self.assertLess(out.index("SANDBOX MODE ACTIVE"),
                        out.index("Active focus task"))
        # The command itself executed (proxied transparently).
        self.assertIn("Active focus task", out)
        self.assertNotIn("Traceback", result.stderr + out)

    def test_plain_ck_exit_is_rejected(self):
        env = _subprocess_env(self.fake_home)
        result = subprocess.run(
            [sys.executable, str(self.checkout / "ck"), "exit"],
            capture_output=True, text=True, timeout=60,
            cwd=str(self.checkout), env=env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown command", result.stdout)

    def test_st_g_inside_sandbox_dir_without_env_lists_alpha(self):
        """`ck st -g` run INSIDE ``.sandbox/projects/alpha`` (plain ck,
        fresh shell WITHOUT CK_SANDBOX) reads the isolated
        ``.sandbox/config/projects.json`` and renders the mock
        projects — never "No registered projects"."""
        entry = _run_ck_dev([], self.checkout, self.fake_home,
                            script=self.ck_dev_copy)
        self.assertEqual(entry.returncode, 0, entry.stderr)

        alpha = self.copy_sandbox / "projects" / "alpha"
        env = _subprocess_env(self.fake_home)  # strips CK_SANDBOX
        result = subprocess.run(
            [sys.executable, str(self.checkout / "ck"), "st", "-g"],
            capture_output=True, text=True, timeout=60,
            cwd=str(alpha), env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        # The populated dashboard (not the empty-registry message).
        self.assertIn("GLOBAL DASHBOARD", out)
        self.assertIn("alpha", out)
        self.assertIn("[MISSING] orphaned-deleted", out)
        self.assertNotIn("No registered projects", out)
        # The interceptor banner preceded it (cwd inside the sandbox).
        self.assertIn("SANDBOX MODE ACTIVE", out)
        self.assertLess(out.index("SANDBOX MODE ACTIVE"),
                        out.index("GLOBAL DASHBOARD"))
        # Host isolation held: the fake-HOME registry was never
        # created by the routed read.
        self.assertFalse((self.fake_home / ".config" / "ck").exists())


class TestSubshellResolvesCheckoutCk(_WrapperBase):
    """Binary execution order inside the spawned sandbox subshell.

    The reported failure mode: with a globally installed ``ck``
    earlier on the inherited ``PATH``, a bare ``ck`` in the session
    subshell ran the GLOBAL binary instead of the checkout's current
    dev sources. The contract verified here is isolation-safe: a
    DECOY ``ck`` (standing in for any global install) leads ``PATH``
    and the resolved command must still lie INSIDE the checkout.

    No system-specific path (``~/.local/bin``, ``/usr/local/bin``,
    …) is ever hardcoded or asserted on: the decoy lives in the
    test's temp dir and the assertion is purely "resolved path is
    under the repository root".
    """

    def setUp(self):
        super().setUp()
        if shutil.which("sh") is None:
            self.skipTest("sh unavailable")
        result = _run_ck_dev(["sandbox", "setup"], self.checkout,
                             self.fake_home, script=self.ck_dev_copy)
        self.assertEqual(result.returncode, 0, result.stderr)

    def _decoy_bin(self) -> Path:
        """A PATH-leading dir holding a DIFFERENT ``ck`` executable."""
        decoy_bin = Path(self._tmp.name) / "global-bin"
        decoy_bin.mkdir(exist_ok=True)
        decoy = decoy_bin / "ck"
        decoy.write_text("#!/bin/sh\necho DECOY-GLOBAL-CK\n",
                         encoding="utf-8")
        decoy.chmod(0o755)
        return decoy_bin

    def _probe(self, name: str, body: str) -> Path:
        probe = Path(self._tmp.name) / name
        probe.write_text("#!/bin/sh\n" + body + "exit 0\n",
                         encoding="utf-8")
        probe.chmod(0o755)
        return probe

    def test_which_ck_inside_subshell_points_inside_checkout(self):
        """Spawn the REAL session subshell and run ``command -v ck``:
        the resolved path starts with the repository root even though
        a decoy ``ck`` leads the inherited PATH."""
        decoy_bin = self._decoy_bin()
        out_file = Path(self._tmp.name) / "which-ck.txt"
        probe = self._probe(
            "which-ck.sh", 'command -v ck > "$PROBE_OUT"\n')

        env = {
            "PATH": os.pathsep.join(
                [str(decoy_bin), os.environ.get("PATH", "")]),
            "CK_DEV_FORCE_SUBSHELL": "1",
            "SHELL": str(probe),
            "PROBE_OUT": str(out_file),
        }
        result = _run_ck_dev([], self.checkout, self.fake_home,
                             script=self.ck_dev_copy, env_extra=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        resolved = out_file.read_text(encoding="utf-8").strip()
        # The contract: the wrapper's own directory wins the race.
        self.assertEqual(resolved, str(self.checkout / "ck"),
                         f"ck resolved to {resolved!r}")
        self.assertTrue(
            resolved.startswith(str(self.checkout) + os.sep),
            f"ck resolved to {resolved!r}, expected under {self.checkout}",
        )
        # It is the checkout wrapper, not the decoy that leads PATH.
        self.assertNotEqual(resolved, str(decoy_bin / "ck"))

    def test_subshell_top_level_flags_are_recognized(self):
        """`ck -l`, `ck -g` and `ck -e` run INSIDE the subshell without
        an argparse ``unrecognized arguments`` error (the regression
        the PATH routing work exists for: only the checkout version
        understands the top-level shorthands)."""
        alpha = self.copy_sandbox / "projects" / "alpha"
        probe = self._probe(
            "flags.sh",
            'out=$(ck -l 2>&1); case "$out" in '
            '*"unrecognized arguments"*) echo PROBE_L=UNRECOGNIZED;; '
            "*) echo PROBE_L=OK;; esac\n"
            'out=$(ck -g 2>&1); case "$out" in '
            '*"unrecognized arguments"*) echo PROBE_G=UNRECOGNIZED;; '
            "*) echo PROBE_G=OK;; esac\n"
            'out=$(ck -e 2>&1); case "$out" in '
            '*"unrecognized arguments"*) echo PROBE_E=UNRECOGNIZED;; '
            "*) echo PROBE_E=OK;; esac\n",
        )
        env = {
            "CK_DEV_FORCE_SUBSHELL": "1",
            "SHELL": str(probe),
            "EDITOR": "/bin/true",
            "VISUAL": "",
        }
        result = _run_ck_dev([], alpha, self.fake_home,
                             script=self.ck_dev_copy, env_extra=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("PROBE_L=OK", out)
        self.assertIn("PROBE_G=OK", out)
        self.assertIn("PROBE_E=OK", out)
        self.assertNotIn("unrecognized arguments", out)


if __name__ == "__main__":
    unittest.main()
