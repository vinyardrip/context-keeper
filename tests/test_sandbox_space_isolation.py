"""Strict isolation of global spaces during dev/sandbox sessions.

A sandbox run must never read, discover, list or mutate the host's
``~/.config/ck/spaces/`` tree. Every space path, note sidecar, history
file and the persisted space order resolve to
``.sandbox/config/spaces/`` and ``.sandbox/config/state.json`` instead.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import spaces
from cklib.cli import main


class TestSandboxSpaceIsolation(unittest.TestCase):
    _ENV_KEYS = ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ROOT",
                 "CK_SANDBOX_ACTIVE", "CK_SANDBOX_SHELL")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)

        self.prod_config = root / "home" / ".config" / "ck"
        self.prod_spaces = self.prod_config / "spaces"
        self.sandbox_root = root / ".sandbox"
        self.sandbox_spaces = self.sandbox_root / "config" / "spaces"

        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["CK_SANDBOX_ROOT"] = str(self.sandbox_root)
        os.environ["CK_SANDBOX"] = "1"

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

        ckconfig.GLOBAL_CONFIG_DIR = self.prod_config
        ckconfig.GLOBAL_REGISTRY_FILE = self.prod_config / "projects.json"
        ckconfig.GLOBAL_STATE_FILE = self.prod_config / "state.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = root / "home" / ".ckrc"
        for name in self._saved_registry:
            setattr(ckregistry, name, getattr(ckconfig, name))

        self.prod_spaces.mkdir(parents=True)

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

    def _write_host_space(self, name: str, task: str) -> Path:
        path = self.prod_spaces / f"{name}.md"
        path.write_text(
            f"# {name}\n\n## Current Sprint\n- [ ] {task}\n",
            encoding="utf-8",
        )
        return path

    def test_paths_are_strictly_redirected_without_creating_sandbox(self):
        self.assertEqual(spaces.spaces_dir(), self.sandbox_spaces)
        self.assertEqual(spaces.space_path("local"),
                         self.sandbox_spaces / "local.md")
        self.assertEqual(spaces.state_path("local"),
                         self.sandbox_spaces / "local.json")
        self.assertEqual(spaces.history_path("local"),
                         self.sandbox_spaces / "local.HISTORY.md")
        # Path resolution is pure: merely asking never materializes it.
        self.assertFalse(self.sandbox_root.exists())

    def test_host_spaces_are_invisible_and_host_order_is_not_read(self):
        self._write_host_space("secret", "HOST-ONLY TASK")
        (self.prod_config / "state.json").write_text(
            json.dumps({"spaces_order": ["secret", "local"]}),
            encoding="utf-8",
        )

        self.assertNotIn("secret", spaces.existing_space_names())
        self.assertFalse(spaces.is_valid_space("secret"))
        self.assertNotIn("HOST-ONLY TASK", spaces.list_space("local"))
        self.assertEqual(spaces.read_space_order(), [])
        self.assertNotIn("secret", spaces.space_order())

        with self.assertRaises(ValueError):
            spaces.SpaceManager("secret")

    def test_sandbox_writes_never_touch_host_space_files(self):
        host_local = self._write_host_space("local", "HOST LOCAL CONTENT")
        before = host_local.read_text(encoding="utf-8")

        spaces.SpaceManager("local").add_task("SANDBOX ONLY TASK")

        self.assertEqual(host_local.read_text(encoding="utf-8"), before)
        sandbox_local = self.sandbox_spaces / "local.md"
        self.assertTrue(sandbox_local.is_file())
        sandbox_text = sandbox_local.read_text(encoding="utf-8")
        self.assertIn("SANDBOX ONLY TASK", sandbox_text)
        self.assertNotIn("HOST LOCAL CONTENT", sandbox_text)

    def test_default_spaces_are_seeded_only_in_the_sandbox(self):
        self._write_host_space("secret", "HOST-ONLY TASK")

        created = spaces.ensure_default_spaces()

        self.assertEqual(sorted(created), ["local", "remote"])
        for name in ("local", "remote"):
            self.assertTrue((self.sandbox_spaces / f"{name}.md").is_file())
            self.assertFalse((self.prod_spaces / f"{name}.md").exists())
        self.assertTrue((self.prod_spaces / "secret.md").is_file())

    def test_cli_startup_lists_only_sandbox_spaces(self):
        self._write_host_space("secret", "HOST-SECRET-TASK")

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["space", "list"])

        self.assertEqual(code, 0, buf.getvalue())
        out = buf.getvalue()
        self.assertNotIn("SECRET", out)
        self.assertIn("LOCAL", out)
        self.assertIn("REMOTE", out)
        self.assertTrue((self.sandbox_spaces / "local.md").is_file())
        self.assertTrue((self.sandbox_spaces / "remote.md").is_file())
        self.assertFalse((self.prod_spaces / "local.md").exists())
        self.assertFalse((self.prod_spaces / "remote.md").exists())

    def test_production_mode_still_reads_host_spaces(self):
        os.environ.pop("CK_SANDBOX", None)
        self._write_host_space("secret", "HOST-ONLY TASK")

        self.assertEqual(spaces.spaces_dir(), self.prod_spaces)
        self.assertIn("secret", spaces.existing_space_names())
        self.assertTrue(spaces.is_valid_space("secret"))
        self.assertIn("HOST-ONLY TASK", spaces.list_space("secret"))


if __name__ == "__main__":
    unittest.main()
