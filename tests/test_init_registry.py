"""Tests for local-only project initialization and opt-in registration."""

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
from cklib.cli import main


class TestInitRegistryBoundary(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._orig_config = (
            ckconfig.GLOBAL_CONFIG_DIR,
            ckconfig.GLOBAL_REGISTRY_FILE,
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE,
        )
        self._orig_registry = (
            ckregistry.GLOBAL_CONFIG_DIR,
            ckregistry.GLOBAL_REGISTRY_FILE,
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE,
        )
        global_dir = home / ".config" / "ck"
        registry_file = global_dir / "projects.json"
        legacy_file = home / ".ckrc"
        ckconfig.GLOBAL_CONFIG_DIR = global_dir
        ckconfig.GLOBAL_REGISTRY_FILE = registry_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = legacy_file
        ckregistry.GLOBAL_CONFIG_DIR = global_dir
        ckregistry.GLOBAL_REGISTRY_FILE = registry_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = legacy_file

    def tearDown(self):
        (
            ckconfig.GLOBAL_CONFIG_DIR,
            ckconfig.GLOBAL_REGISTRY_FILE,
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE,
        ) = self._orig_config
        (
            ckregistry.GLOBAL_CONFIG_DIR,
            ckregistry.GLOBAL_REGISTRY_FILE,
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE,
        ) = self._orig_registry

    def _run_init(self, *args: str) -> tuple[int, str, Path, Path]:
        project = Path(self._tmp.name) / "project"
        project.mkdir()
        registry_file = ckconfig.GLOBAL_REGISTRY_FILE
        output = io.StringIO()
        old_cwd = Path.cwd()
        try:
            os.chdir(project)
            with redirect_stdout(output):
                code = main(["init", *args])
        finally:
            os.chdir(old_cwd)
        return code, output.getvalue(), project, registry_file

    def test_init_is_local_only_and_does_not_create_registry(self):
        code, output, project, registry_file = self._run_init()

        self.assertEqual(code, 0)
        self.assertTrue((project / ".ck" / "PLAN.md").exists())
        self.assertFalse(registry_file.exists())
        self.assertNotIn("Registered:", output)

    def test_init_does_not_modify_existing_registry(self):
        registry_file = ckconfig.GLOBAL_REGISTRY_FILE
        registry_file.parent.mkdir(parents=True)
        original = '{"projects": [{"name": "existing"}]}\n'
        registry_file.write_text(original, encoding="utf-8")

        code, _output, project, _registry_file = self._run_init()

        self.assertEqual(code, 0)
        self.assertTrue((project / ".ck").is_dir())
        self.assertEqual(registry_file.read_text(encoding="utf-8"), original)

    def test_init_register_creates_local_files_and_registry_entry(self):
        code, output, project, registry_file = self._run_init("--register")

        self.assertEqual(code, 0)
        self.assertTrue((project / ".ck" / "PLAN.md").exists())
        self.assertTrue(registry_file.exists())
        data = json.loads(registry_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data["projects"]), 1)
        self.assertEqual(data["projects"][0]["path"], str(project.resolve()))
        self.assertIn("Registered:", output)


if __name__ == "__main__":
    unittest.main()
