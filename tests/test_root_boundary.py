"""Boundary tests for project-root resolution and strict-local init.

Covers the v0.6.4 architecture:

- ``ck init`` operates strictly on ``$PWD`` and never inherits an
  ancestor ``.ck/`` / ``PLAN.md`` — it creates a fresh ``.ck/`` right
  where it is run.
- ``find_project_root`` stops upward traversal at a ``.git`` boundary,
  so a nested directory never attaches to a parent repository's project.
- ``find_project_root`` and the ``ck st`` / ``ck list`` upward context
  walk stop at the ``.sandbox/`` edge while a sandbox session is active,
  so in-sandbox directories never leak into the host checkout's project.
- Global spaces (``ck local`` / ``ck remote``) and global views
  (``ck dashboard``) stay independent of ``$PWD``.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.cli import main
from cklib.config import find_project_root
from cklib.core import ContextKeeper, _upward_context_candidates


class _Isolated(unittest.TestCase):
    """Pin global config to a per-test tmp HOME; reset CK toggles."""

    _ENV_KEYS = ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                 "CK_SANDBOX_SHELL", "CK_SANDBOX_ROOT")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["CK_SANDBOX_ROOT"] = str(fake_home / ".sandbox")

        self._orig = {
            (mod, n): getattr(mod, n)
            for mod in (ckconfig, ckregistry)
            for n in ("GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                      "LEGACY_GLOBAL_CONFIG_FILE")
        }
        new_dir = fake_home / ".config" / "ck"
        for mod in (ckconfig, ckregistry):
            mod.GLOBAL_CONFIG_DIR = new_dir
            mod.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
            mod.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"

        self._orig_cwd = Path.cwd()
        self.addCleanup(os.chdir, self._orig_cwd)

    def tearDown(self):
        for (mod, name), value in self._orig.items():
            setattr(mod, name, value)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _run(self, cwd: Path, argv) -> str:
        """Run ``ck <argv>`` with ``cwd`` as PWD; return stdout."""
        os.chdir(cwd)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(argv))
        self.assertEqual(code, 0, buf.getvalue())
        return buf.getvalue()


class TestStrictLocalInit(_Isolated):
    def test_init_in_nested_subdir_does_not_bind_to_parent_plan(self):
        parent = Path(self._tmp.name) / "parent"
        (parent / ".ck").mkdir(parents=True)
        (parent / ".ck" / "PLAN.md").write_text(
            "# p\n## Current Sprint\n- [ ] parent task\n", encoding="utf-8")
        sub = parent / "nested" / "deep"
        sub.mkdir(parents=True)

        self._run(sub, ["init"])

        # A fresh .ck/ was created right in $PWD ...
        self.assertTrue((sub / ".ck" / "PLAN.md").is_file())
        # ... and the ancestor project's PLAN.md was NOT rebound/edited.
        self.assertIn("parent task",
                      (parent / ".ck" / "PLAN.md").read_text(encoding="utf-8"))
        # The ancestor did not receive a plan update either (init is local).
        self.assertNotIn("parent task",
                         (sub / ".ck" / "PLAN.md").read_text(encoding="utf-8"))

    def test_init_with_explicit_root_still_uses_that_root(self):
        target = Path(self._tmp.name) / "explicit"
        target.mkdir()
        ck = ContextKeeper(root=target)
        ck.init()
        self.assertTrue((target / ".ck" / "PLAN.md").is_file())


class TestGitBoundary(_Isolated):
    def test_upward_walk_never_crosses_git_root(self):
        outer = Path(self._tmp.name) / "outer"
        (outer / ".ck").mkdir(parents=True)
        (outer / ".ck" / "PLAN.md").write_text(
            "# o\n- [ ] above git\n", encoding="utf-8")
        repo = outer / "repo"
        (repo / ".git").mkdir(parents=True)
        deep = repo / "src" / "deep"
        deep.mkdir(parents=True)

        # Must NOT leak to the parent project above the git root.
        self.assertEqual(find_project_root(deep), deep)

    def test_git_root_with_its_own_ck_is_still_the_project(self):
        repo = Path(self._tmp.name) / "repo"
        (repo / ".ck").mkdir(parents=True)
        (repo / ".git").mkdir()
        sub = repo / "src"
        sub.mkdir()
        self.assertEqual(find_project_root(sub), repo)


class TestSandboxBoundary(_Isolated):
    def test_upward_walk_stops_at_sandbox_edge(self):
        host = Path(self._tmp.name) / "host"
        (host / ".ck").mkdir(parents=True)
        (host / ".ck" / "PLAN.md").write_text(
            "# h\n- [ ] host task\n", encoding="utf-8")
        sandbox = host / ".sandbox"
        trouble = sandbox / "projects" / "temp" / "trouble_path"
        trouble.mkdir(parents=True)

        os.environ["CK_SANDBOX"] = "1"
        os.environ["CK_SANDBOX_ROOT"] = str(sandbox)
        os.chdir(trouble)

        # find_project_root never climbs above .sandbox/ to the host.
        self.assertEqual(find_project_root(trouble), trouble)

        # The ck st / ck list context candidates stop at .sandbox/ too.
        cands = _upward_context_candidates(trouble)
        self.assertEqual(cands[-1], sandbox)
        self.assertNotIn(host, cands)

        # End-to-end: `ck st` from inside the sandbox never shows the
        # host project's task.
        out = self._run(trouble, ["st"])
        self.assertNotIn("host task", out)


class TestGlobalCommandsIgnorePwd(_Isolated):
    def test_dashboard_and_spaces_read_global_config_from_any_cwd(self):
        # Register a real project, then run global commands from an
        # unrelated nested directory with no project of its own.
        proj = Path(self._tmp.name) / "proj"
        (proj / ".ck").mkdir(parents=True)
        (proj / ".ck" / "PLAN.md").write_text(
            "# proj\n## Current Sprint\n- [ ] registered task\n",
            encoding="utf-8")
        ContextKeeper(root=proj).register(path=proj)

        nested = Path(self._tmp.name) / "scratch" / "nowhere"
        nested.mkdir(parents=True)

        dash = self._run(nested, ["dashboard"])
        self.assertIn("proj", dash)

        # Spaces are global (independent of $PWD) and resolve the same
        # from any working directory.
        add = self._run(nested, ["local", "add", "from nowhere"])
        self.assertIn("from nowhere", add)
        listed = self._run(nested, ["local", "list"])
        self.assertIn("from nowhere", listed)
        self.assertTrue(ckconfig.GLOBAL_CONFIG_DIR.exists())
