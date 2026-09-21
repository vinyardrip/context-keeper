"""Tests for dev-mode detection & write-path interception (Step 2).

Covers:

- ``is_dev_mode``: CK_SANDBOX / CK_DEV env toggles, ``--sandbox``
  flag, and the ``ck-dev`` entrypoint name.
- ``resolve_write_path``: production writes (global config, real
  project ``.ck/`` trees, ``.ck.json``) map strictly into
  ``.sandbox/``; read-mode / dev-off paths pass through unchanged;
  sandbox paths are idempotent; non-ck targets pass through.
- Guardrail: ``assert_no_real_write`` raises
  ``SandboxViolationError`` for production paths in dev mode and
  never mutates production files during simulated writes.
- ``log_sandbox_debug``: writes only to stderr / ``.sandbox/dev.log``
  when CK_DEBUG=1 / -v; stdout stays clean; silent when disabled.
"""

from __future__ import annotations

# Filesystem isolation safety net: importing the tests package
# pins CK_SANDBOX_ROOT to an OS-temp directory (see tests/__init__),
# so no test in this module can create or wipe the repository's own
# .sandbox/ — under ANY runner, including bare `unittest discover`.
import tests  # noqa: F401

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cklib import config as ckconfig
from cklib import sandbox
from cklib.sandbox import (
    SandboxViolationError,
    assert_no_real_write,
    debug_enabled,
    is_dev_mode,
    is_within_sandbox,
    log_sandbox_debug,
    resolve_write_path,
    sandbox_root,
)


class _SandboxCleanup(unittest.TestCase):
    """Deterministic environment for sandbox tests.

    - Removes ``.sandbox/`` before AND after each test.
    - Pops ``CK_SANDBOX`` / ``CK_DEV`` / ``CK_DEBUG`` from the real
      environment (restored on teardown) so each test starts OFF.
    - Pins ``sys.argv`` to ``["ck"]`` so pytest's own flags (e.g.
      ``-v``) can never leak into argv-based detection/logging.
    """

    def setUp(self):
        sandbox.clean_sandbox(quiet=True)
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in ("CK_SANDBOX", "CK_DEV", "CK_DEBUG")
        }
        self._saved_argv = sandbox.sys.argv
        sandbox.sys.argv = ["ck"]

    def tearDown(self):
        sandbox.clean_sandbox(quiet=True)
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        sandbox.sys.argv = self._saved_argv


# ---------------------------------------------------------------------------
# 1. Dev-mode detection
# ---------------------------------------------------------------------------


class TestIsDevMode(_SandboxCleanup, unittest.TestCase):
    ENV_OFF = {"HOME": "/tmp"}
    ENV_SANDBOX = {"HOME": "/tmp", "CK_SANDBOX": "1"}
    ENV_DEV = {"HOME": "/tmp", "CK_DEV": "1"}

    # ---- environment variables -------------------------------------- #

    def test_off_by_default(self):
        self.assertFalse(is_dev_mode(argv=["ck"], env=self.ENV_OFF))

    def test_ck_sandbox_env(self):
        for val in ("1", "true", "YES", "On", " 1 "):
            self.assertTrue(
                is_dev_mode(argv=["ck"], env={**self.ENV_OFF,
                                             "CK_SANDBOX": val}),
                msg=f"CK_SANDBOX={val!r} should activate dev mode",
            )
        for val in ("", "0", "no", "false", "off", None):
            env = dict(self.ENV_OFF)
            if val is not None:
                env["CK_SANDBOX"] = val
            self.assertFalse(
                is_dev_mode(argv=["ck"], env=env),
                msg=f"CK_SANDBOX={val!r} must NOT activate dev mode",
            )

    def test_ck_dev_env(self):
        self.assertTrue(is_dev_mode(argv=["ck"], env=self.ENV_DEV))
        self.assertFalse(
            is_dev_mode(argv=["ck"], env={**self.ENV_DEV, "CK_DEV": "0"})
        )

    def test_env_consulted_from_os_environ_when_not_injected(self):
        os.environ["CK_SANDBOX"] = "1"
        try:
            self.assertTrue(is_dev_mode(argv=["ck"]))
        finally:
            del os.environ["CK_SANDBOX"]

    # ---- --sandbox flag ---------------------------------------------- #

    def test_sandbox_flag_activates(self):
        self.assertTrue(
            is_dev_mode(argv=["ck", "add", "text", "--sandbox"],
                        env=self.ENV_OFF)
        )
        # Flag anywhere except argv[0] counts.
        self.assertTrue(is_dev_mode(argv=["ck", "--sandbox", "st"],
                                    env=self.ENV_OFF))

    def test_sandbox_flag_must_not_be_the_entrypoint_name(self):
        # argv[0] = "./ck" (not "ck-dev"); no flag: OFF.
        self.assertFalse(
            is_dev_mode(argv=["./ck", "st"], env=self.ENV_OFF)
        )

    # ---- ck-dev entrypoint ------------------------------------------ #

    def test_dev_entrypoint_name(self):
        for argv0 in ("ck-dev", "./ck-dev", "/usr/local/bin/ck-dev",
                      "CK-DEV", "ck-dev.exe"):
            self.assertTrue(
                is_dev_mode(argv=[argv0, "st"], env=self.ENV_OFF),
                msg=f"entrypoint {argv0!r} should activate dev mode",
            )

    def test_normal_entrypoint_is_off(self):
        for argv0 in ("ck", "./ck", "/usr/local/bin/ck", "ck-clean",
                      "ckdev", "ck-devx"):
            self.assertFalse(
                is_dev_mode(argv=[argv0, "st"], env=self.ENV_OFF),
                msg=f"entrypoint {argv0!r} must NOT activate dev mode",
            )

    def test_pure_function_no_side_effects(self):
        env = {"CK_SANDBOX": "1"}
        argv = ["ck", "--sandbox"]
        is_dev_mode(argv=argv, env=env)
        self.assertEqual(env, {"CK_SANDBOX": "1"})
        self.assertEqual(argv, ["ck", "--sandbox"])


# ---------------------------------------------------------------------------
# 2. resolve_write_path
# ---------------------------------------------------------------------------


class TestResolveWritePath(_SandboxCleanup, unittest.TestCase):
    """Production paths map into .sandbox/; everything else passes."""

    def setUp(self):
        super().setUp()
        # Redirect the global config root into a temp dir so tests
        # never touch the real ~/.config/ck.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._fake_global = Path(self._tmp.name) / "config-root"
        self._orig_global = ckconfig.GLOBAL_CONFIG_DIR
        ckconfig.GLOBAL_CONFIG_DIR = self._fake_global
        # Activate dev mode via the REAL environment (resolve_write_path
        # reads process state).
        os.environ["CK_SANDBOX"] = "1"

    def tearDown(self):
        ckconfig.GLOBAL_CONFIG_DIR = self._orig_global
        os.environ.pop("CK_SANDBOX", None)
        super().tearDown()

    # ---- dev OFF: pass-through -------------------------------------- #

    def test_dev_off_returns_original(self):
        os.environ.pop("CK_SANDBOX", None)  # dev OFF for this test
        p = Path("/tmp/proj/.ck/PLAN.md")
        self.assertEqual(resolve_write_path(p), p)
        reg = self._fake_global / "projects.json"
        self.assertEqual(resolve_write_path(reg), reg)

    # ---- global config interception --------------------------------- #

    def test_registry_write_redirected(self):
        reg = self._fake_global / "projects.json"
        out = resolve_write_path(reg)
        self.assertTrue(is_within_sandbox(out))
        self.assertEqual(out, sandbox_root() / "config" / "projects.json")
        self.assertTrue(out.parent.is_dir())  # auto-created

    def test_global_state_write_redirected(self):
        st = self._fake_global / "state.json"
        out = resolve_write_path(st)
        self.assertEqual(out, sandbox_root() / "config" / "state.json")

    def test_nested_global_config_path_preserves_layout(self):
        deep = self._fake_global / "sub" / "deep" / "file.json"
        out = resolve_write_path(deep)
        self.assertEqual(
            out, sandbox_root() / "config" / "sub" / "deep" / "file.json"
        )
        self.assertTrue(out.parent.is_dir())

    def test_global_config_dir_itself_redirects_to_config_root(self):
        out = resolve_write_path(self._fake_global)
        self.assertEqual(out, sandbox_root() / "config")

    # ---- project write interception --------------------------------- #

    def test_plan_md_redirected(self):
        plan = Path("/tmp/someproj/.ck/PLAN.md")
        out = resolve_write_path(plan)
        self.assertTrue(is_within_sandbox(out))
        # .ck/ subpath preserved under the project's sandbox dir
        self.assertEqual(out.name, "PLAN.md")
        self.assertEqual(out.parent.name, ".ck")
        self.assertIn("someproj", out.parent.parent.name)
        self.assertTrue(out.parent.is_dir())

    def test_ck_json_redirected(self):
        cfg = Path("/tmp/someproj/.ck.json")
        out = resolve_write_path(cfg)
        self.assertTrue(is_within_sandbox(out))
        self.assertEqual(out.name, ".ck.json")
        self.assertIn("someproj", out.parent.name)

    def test_lock_and_backup_files_redirected(self):
        for rel in ("HISTORY.md.lock", "state.json", "HISTORY_20260101.md.bak"):
            p = Path("/tmp/someproj/.ck") / rel
            out = resolve_write_path(p)
            self.assertTrue(is_within_sandbox(out), rel)
            self.assertEqual(out.name, rel)

    def test_same_project_maps_to_one_sandbox_dir(self):
        a = resolve_write_path(Path("/tmp/someproj/.ck/PLAN.md"))
        b = resolve_write_path(Path("/tmp/someproj/.ck/HISTORY.md"))
        self.assertEqual(a.parent, b.parent)

    def test_distinct_projects_get_distinct_sandbox_dirs(self):
        a = resolve_write_path(Path("/tmp/alpha/.ck/PLAN.md"))
        b = resolve_write_path(Path("/tmp/beta/.ck/PLAN.md"))
        proj_a, proj_b = a.parent.parent, b.parent.parent
        self.assertNotEqual(proj_a, proj_b)
        self.assertTrue(proj_a.name.startswith("alpha-"))
        self.assertTrue(proj_b.name.startswith("beta-"))

    def test_same_named_projects_in_different_paths_dont_collide(self):
        a = resolve_write_path(Path("/loc/x/proj/.ck/PLAN.md"))
        b = resolve_write_path(Path("/loc/y/proj/.ck/PLAN.md"))
        self.assertNotEqual(a.parent, b.parent)

    # ---- idempotence & pass-through --------------------------------- #

    def test_already_sandboxed_path_is_idempotent(self):
        first = resolve_write_path(Path("/tmp/someproj/.ck/PLAN.md"))
        second = resolve_write_path(first)
        self.assertEqual(first, second)

    def test_non_ck_target_passes_through(self):
        scratch = Path("/tmp/scratch/notes.txt")
        out = resolve_write_path(scratch)
        self.assertEqual(out, scratch)
        self.assertFalse(is_within_sandbox(out))

    def test_relative_non_ck_path_passes_through(self):
        out = resolve_write_path(Path("relative/scratch.txt"))
        self.assertEqual(out, Path("relative/scratch.txt"))


# ---------------------------------------------------------------------------
# 3. Guardrail
# ---------------------------------------------------------------------------


class TestGuardrail(_SandboxCleanup, unittest.TestCase):
    """assert_no_real_write + production immutability."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._fake_global = Path(self._tmp.name) / "config-root"
        self._orig_global = ckconfig.GLOBAL_CONFIG_DIR
        ckconfig.GLOBAL_CONFIG_DIR = self._fake_global
        # Dev mode ON via the real environment for these tests.
        os.environ["CK_SANDBOX"] = "1"

    def tearDown(self):
        ckconfig.GLOBAL_CONFIG_DIR = self._orig_global
        os.environ.pop("CK_SANDBOX", None)
        super().tearDown()

    def test_real_project_write_raises(self):
        plan = Path("/tmp/someproj/.ck/PLAN.md")
        with self.assertRaises(SandboxViolationError):
            assert_no_real_write(plan)

    def test_real_config_write_raises(self):
        reg = self._fake_global / "projects.json"
        with self.assertRaises(SandboxViolationError):
            assert_no_real_write(reg)

    def test_sandboxed_path_passes(self):
        sandboxed = sandbox.sandbox_project_dir("/tmp/someproj") / "PLAN.md"
        assert_no_real_write(sandboxed)  # must not raise

    def test_non_ck_path_passes(self):
        assert_no_real_write(Path("/tmp/scratch/notes.txt"))  # no raise

    def test_dev_off_never_raises(self):
        os.environ.pop("CK_SANDBOX", None)  # dev OFF for this test
        plan = Path("/tmp/someproj/.ck/PLAN.md")
        assert_no_real_write(plan)  # must not raise

    def test_simulated_write_leaves_production_untouched(self):
        """Full simulated write flow: resolve → write → verify the
        real files were never created; the sandbox copy exists."""
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "realproj"
            (proj / ".ck").mkdir(parents=True)
            plan = proj / ".ck" / "PLAN.md"
            plan.write_text("# real\n", encoding="utf-8")
            before = plan.read_text(encoding="utf-8")
            before_mtime_ns = plan.stat().st_mtime_ns

            redirected = sandbox.resolve_write_path(plan)
            redirected.write_text("# sandboxed\n", encoding="utf-8")

            # Production file: unchanged content AND unchanged mtime.
            self.assertEqual(plan.read_text(encoding="utf-8"), before)
            self.assertEqual(plan.stat().st_mtime_ns, before_mtime_ns)
            # The write landed inside the sandbox.
            self.assertTrue(is_within_sandbox(redirected))
            self.assertIn("# sandboxed",
                          redirected.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 4. Debug logging (stdout hygiene)
# ---------------------------------------------------------------------------


class TestLogging(_SandboxCleanup, unittest.TestCase):
    """log_sandbox_debug: stderr/dev.log only; stdout stays clean."""

    def test_no_output_when_debug_disabled(self):
        buf_err = io.StringIO()
        buf_out = io.StringIO()
        with redirect_stderr(buf_err), redirect_stdout(buf_out):
            log_sandbox_debug("hidden", env={"CK_DEBUG": "0"})
        self.assertEqual(buf_err.getvalue(), "")
        self.assertEqual(buf_out.getvalue(), "")
        self.assertFalse((sandbox_root() / "dev.log").exists())

    def test_ck_debug_writes_to_stderr_and_dev_log(self):
        buf_err = io.StringIO()
        buf_out = io.StringIO()
        with redirect_stderr(buf_err), redirect_stdout(buf_out):
            log_sandbox_debug("Intercepted write -> X", env={"CK_DEBUG": "1"})
        self.assertIn("[DEBUG] Intercepted write -> X", buf_err.getvalue())
        # stdout must remain COMPLETELY clean.
        self.assertEqual(buf_out.getvalue(), "")
        log_file = sandbox_root() / "dev.log"
        self.assertTrue(log_file.exists())
        self.assertIn("[DEBUG] Intercepted write -> X",
                      log_file.read_text(encoding="utf-8"))

    def test_verbose_flag_enables_logging(self):
        saved_argv = sandbox.sys.argv
        sandbox.sys.argv = ["ck", "st", "-v"]
        try:
            self.assertTrue(debug_enabled(env={}))
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                log_sandbox_debug("via -v", env={})
            self.assertIn("[DEBUG] via -v", buf_err.getvalue())
        finally:
            sandbox.sys.argv = saved_argv

    def test_long_verbose_flag_enables(self):
        saved_argv = sandbox.sys.argv
        sandbox.sys.argv = ["ck", "add", "text", "--verbose"]
        try:
            self.assertTrue(debug_enabled(env={}))
        finally:
            sandbox.sys.argv = saved_argv

    def test_debug_env_overrides_flag_absence(self):
        saved_argv = sandbox.sys.argv
        sandbox.sys.argv = ["ck"]
        try:
            self.assertTrue(debug_enabled(env={"CK_DEBUG": "1"}))
            self.assertFalse(debug_enabled(env={}))
        finally:
            sandbox.sys.argv = saved_argv

    def test_log_never_raises_on_unwritable_sandbox(self):
        # dev.log unwritable (sandbox root is a file): swallow.
        sandbox.clean_sandbox(quiet=True)
        root = sandbox_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        root.write_text("blocker", encoding="utf-8")
        buf_err = io.StringIO()
        try:
            with redirect_stderr(buf_err):
                log_sandbox_debug("still ok", env={"CK_DEBUG": "1"})
        finally:
            root.unlink(missing_ok=True)
        # stderr line still emitted; file write silently dropped.
        self.assertIn("[DEBUG] still ok", buf_err.getvalue())

    def test_interception_emits_debug_log(self):
        """resolve_write_path traces interceptions when CK_DEBUG=1."""
        os.environ["CK_SANDBOX"] = "1"
        os.environ["CK_DEBUG"] = "1"
        try:
            buf_err = io.StringIO()
            buf_out = io.StringIO()
            with redirect_stderr(buf_err), redirect_stdout(buf_out):
                redirected = resolve_write_path(Path("/tmp/anyproj/.ck/PLAN.md"))
            self.assertIn("Intercepted write ->", buf_err.getvalue())
            self.assertIn(str(redirected), buf_err.getvalue())
            self.assertEqual(buf_out.getvalue(), "")
        finally:
            os.environ.pop("CK_SANDBOX", None)
            os.environ.pop("CK_DEBUG", None)


if __name__ == "__main__":
    unittest.main()
