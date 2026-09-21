"""Tests for cklib.config path resolution (dangling-cwd degradation).

Regression coverage for the ``FileNotFoundError: [Errno 2] No such
file or directory`` crash class: when the process working directory
is deleted (sandbox rebuild, external ``rm -rf``), ``Path.cwd()``
raises — :func:`cklib.config.find_project_root` must degrade to
``None`` instead of crashing every ``ck`` invocation.
"""

from __future__ import annotations

# Filesystem isolation safety net (see tests/__init__.py).
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
from unittest import mock

from cklib.config import find_project_root


class TestFindProjectRootNormal(unittest.TestCase):
    def test_explicit_start_walks_up_to_ck(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            (root / ".ck").mkdir(parents=True)
            deep = root / "a" / "b"
            deep.mkdir(parents=True)
            self.assertEqual(find_project_root(deep), root)

    def test_explicit_start_without_ck_returns_start(self):
        with tempfile.TemporaryDirectory() as td:
            plain = Path(td) / "plain"
            plain.mkdir()
            self.assertEqual(find_project_root(plain), plain)


class TestFindProjectRootDanglingCwd(unittest.TestCase):
    """The cwd descriptor is gone: return None, never raise."""

    def test_returns_none_when_cwd_raises_filenotfound(self):
        err = FileNotFoundError(2, "No such file or directory")
        with mock.patch("cklib.config.Path.cwd", side_effect=err):
            self.assertIsNone(find_project_root())

    def test_returns_none_for_os_getcwd_failure(self):
        # The real-world trigger: os.getcwd() itself fails because
        # the directory descriptor was invalidated by a wipe.
        with mock.patch("os.getcwd", side_effect=FileNotFoundError(2, "gone")):
            self.assertIsNone(find_project_root())

    def test_returns_none_on_other_oserror(self):
        with mock.patch("cklib.config.Path.cwd", side_effect=OSError(13, "denied")):
            self.assertIsNone(find_project_root())

    def test_explicit_start_ignores_broken_cwd(self):
        # With an explicit start, the broken cwd is never consulted.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            (root / ".ck").mkdir(parents=True)
            err = FileNotFoundError(2, "No such file or directory")
            with mock.patch("cklib.config.Path.cwd", side_effect=err):
                self.assertEqual(find_project_root(root), root)


class _IsolatedGlobalConfig:
    """Mixin: pin the global registry/config to a per-test tmp HOME."""

    def _isolate(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp_name = tmp.name
        fake_home = Path(tmp.name) / ".config" / "ck"

        from cklib import config as ckconfig
        from cklib import registry as ckregistry

        self._ckconfig = ckconfig
        self._ckregistry = ckregistry
        self._orig = {}
        for mod, names in (
            (ckconfig, ("GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                        "LEGACY_GLOBAL_CONFIG_FILE")),
            (ckregistry, ("GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                          "LEGACY_GLOBAL_CONFIG_FILE")),
        ):
            for n in names:
                self._orig[(mod, n)] = getattr(mod, n)
        ckconfig.GLOBAL_CONFIG_DIR = fake_home
        ckconfig.GLOBAL_REGISTRY_FILE = fake_home / "projects.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = Path(tmp.name) / ".ckrc"
        ckregistry.GLOBAL_CONFIG_DIR = fake_home
        ckregistry.GLOBAL_REGISTRY_FILE = fake_home / "projects.json"
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = Path(tmp.name) / ".ckrc"
        return Path(tmp.name)

    def _restore(self) -> None:
        for (mod, n), v in self._orig.items():
            setattr(mod, n, v)


class TestContextKeeperRootless(_IsolatedGlobalConfig, unittest.TestCase):
    """A keeper built from a dangling cwd runs ROOTLESS (root=None)."""

    def setUp(self):
        self._isolate()
        self.addCleanup(self._restore)

    def _register_project(self) -> Path:
        from cklib.core import ContextKeeper

        proj = Path(self._tmp_name) / "proj"
        (proj / ".ck").mkdir(parents=True)
        (proj / ".ck" / "PLAN.md").write_text(
            "# p\n## Current Sprint\n- [ ] t1\n", encoding="utf-8")
        ContextKeeper(root=proj).register(path=proj)
        return proj

    def test_keeper_root_is_none_on_dangling_cwd(self):
        from cklib.core import ContextKeeper

        self._register_project()
        err = FileNotFoundError(2, "No such file or directory")
        with mock.patch("cklib.config.Path.cwd", side_effect=err):
            ck = ContextKeeper()
        self.assertIsNone(ck.root)
        self.assertIsNone(ck.ck_path)
        self.assertIsNone(ck.plan_file)

    def test_rootless_keeper_global_commands_work(self):
        from cklib.core import ContextKeeper

        proj = self._register_project()
        err = FileNotFoundError(2, "No such file or directory")
        with mock.patch("cklib.config.Path.cwd", side_effect=err):
            ck = ContextKeeper()
            out = ck.dashboard()
            self.assertIn("proj", out)
            self.assertIn("GLOBAL DASHBOARD", out)
            # Registration with an explicit path still works.
            entry = ck.register(path=proj)
            self.assertEqual(entry.name, "proj")
            # Pure-registry prune works.
            self.assertEqual(ck.prune(), [])
            # Context resolution falls through to the GLOBAL tier:
            # the registered project serves reads from anywhere.
            self.assertIn("t1", ck.tasks())

    def test_rootless_keeper_local_commands_raise_clean_valueerror(self):
        from cklib.core import ContextKeeper

        self._register_project()
        err = FileNotFoundError(2, "No such file or directory")
        with mock.patch("cklib.config.Path.cwd", side_effect=err):
            ck = ContextKeeper()
            with self.assertRaises(ValueError) as cm:
                ck.add_task("x")
            self.assertIn("Not in a valid project", str(cm.exception))
            with self.assertRaises(ValueError):
                ck.init()
            with self.assertRaises(ValueError):
                ck.unregister()
            with self.assertRaises(ValueError):
                ck.edit_plan()


def _rmtree_quiet(p: Path) -> None:
    import shutil

    shutil.rmtree(p, ignore_errors=True)


_DANGLING_ERR = FileNotFoundError(2, "No such file or directory")
_NO_ROOT_SNIPPET = "Not in a valid project directory"


class TestCliWithRootlessKeeper(_IsolatedGlobalConfig, unittest.TestCase):
    """CLI integration: global commands survive a dangling cwd; local
    commands print the clean actionable message (exit 1, no traceback).
    """

    def setUp(self):
        self._isolate()
        self.addCleanup(self._restore)
        from cklib.core import ContextKeeper

        self.proj = Path(self._tmp_name) / "proj"
        (self.proj / ".ck").mkdir(parents=True)
        (self.proj / ".ck" / "PLAN.md").write_text(
            "# p\n## Current Sprint\n- [ ] t1\n", encoding="utf-8")
        ContextKeeper(root=self.proj).register(path=self.proj)

    def _run_dangling(self, argv: list) -> tuple[int, str]:
        from cklib.cli import main

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("cklib.config.Path.cwd", side_effect=_DANGLING_ERR):
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                code = main(argv)
        return code, buf_out.getvalue() + buf_err.getvalue()

    def test_dashboard_executes_with_exit_0(self):
        code, out = self._run_dangling(["dashboard"])
        self.assertEqual(code, 0)
        self.assertIn("GLOBAL DASHBOARD", out)
        self.assertIn("proj", out)
        self.assertNotIn("Traceback", out)

    def test_st_dash_g_executes_with_exit_0(self):
        code, out = self._run_dangling(["st", "-g"])
        self.assertEqual(code, 0)
        self.assertIn("GLOBAL DASHBOARD", out)
        self.assertNotIn("Traceback", out)

    def test_register_prune_unregister_work(self):
        code, out = self._run_dangling(["prune"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing to prune", out)

        code, out = self._run_dangling(
            ["register", "--path", str(self.proj), "--name", "again"])
        self.assertEqual(code, 0)
        self.assertIn("[ok] Registered: again", out)

        code, out = self._run_dangling(["unregister", "again"])
        self.assertEqual(code, 0)
        self.assertIn("[ok] Unregistered", out)

    def test_local_commands_show_clean_error_exit_1(self):
        for argv in (["add", "x"], ["start", "1"], ["done", "1"],
                     ["init"], ["note", "n"], ["edit"], ["log"],
                     ["save"]):
            code, out = self._run_dangling(argv)
            self.assertEqual(code, 1, f"{argv}: exit {code}: {out}")
            self.assertIn(_NO_ROOT_SNIPPET, out, f"{argv}: {out}")
            self.assertNotIn("Traceback", out)

    def test_read_commands_fall_back_to_global_registry(self):
        """Rootless reads resolve through the GLOBAL tier: st/list
        render the most-recently-registered project instead of
        crashing on the dead cwd (matches the documented hierarchy).
        """
        for argv in (["st"], ["list"]):
            code, out = self._run_dangling(argv)
            self.assertEqual(code, 0, f"{argv}: exit {code}: {out}")
            self.assertIn("t1", out)
            self.assertNotIn("Traceback", out)


class TestDanglingCwdEndToEnd(unittest.TestCase):
    """Subprocess e2e: run plain `ck` from a REALLY deleted cwd.

    Mocked-Path.cwd tests prove the fallback logic; this one proves
    the full CLI against a genuinely invalidated directory descriptor
    (created, chdir'd into, then rmtree'd from outside — exactly the
    post-sandbox-rebuild state).
    """

    def test_st_g_survives_deleted_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            home.mkdir()
            doomed = base / "doomed"
            doomed.mkdir()
            proj = base / "proj"
            (proj / ".ck").mkdir(parents=True)
            (proj / ".ck" / "PLAN.md").write_text(
                "# p\n## Current Sprint\n- [ ] t1\n", encoding="utf-8")

            env = {
                k: v for k, v in os.environ.items()
                if k not in ("CK_SANDBOX", "CK_SANDBOX_ROOT", "CK_DEV",
                             "CK_DEBUG", "CK_SANDBOX_ACTIVE")
            }
            env["HOME"] = str(home)
            env["CK_DISABLE_UPDATE_CHECK"] = "1"
            repo_root = Path(__file__).resolve().parent.parent

            # Register the project FIRST (cwd still valid).
            reg = subprocess.run(
                [sys.executable, str(repo_root / "ck"), "register",
                 "--path", str(proj)],
                capture_output=True, text=True, timeout=60,
                cwd=str(proj), env=env,
            )
            self.assertEqual(reg.returncode, 0, reg.stderr)

            # Orchestration: the child chdirs into `doomed` and signals
            # readiness; THIS process then deletes the directory so the
            # child's cwd descriptor dangles, and only then launches
            # `ck st -g` — cwd inherited as the dead path, exactly the
            # post-sandbox-rebuild state.
            ready = base / "ready.flag"
            go = base / "go.flag"
            inner = (
                "import os, subprocess, sys, time\n"
                f"os.chdir({str(doomed)!r})\n"
                f"open({str(ready)!r}, 'w').write('1')\n"
                f"while not os.path.exists({str(go)!r}):\n"
                "    time.sleep(0.02)\n"
                "r = subprocess.run("
                f"[sys.executable, {str(repo_root / 'ck')!r}, 'st', '-g'],"
                "capture_output=True, text=True)\n"
                "sys.stdout.write(r.stdout)\n"
                "sys.stderr.write(r.stderr)\n"
                "sys.exit(r.returncode)\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", inner],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=str(home), env=env,
            )
            try:
                for _ in range(250):
                    if ready.exists():
                        break
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "child never became ready")
                doomed.rmdir()
                go.write_text("1", encoding="utf-8")
                out, err = child.communicate(timeout=60)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
            combined = out + err
            self.assertNotIn("FileNotFoundError", combined)
            self.assertNotIn("Traceback", combined)
            self.assertEqual(child.returncode, 0,
                             f"stderr: {err}")
            self.assertIn("GLOBAL DASHBOARD", out)
            self.assertIn("proj", out)


if __name__ == "__main__":
    unittest.main()
