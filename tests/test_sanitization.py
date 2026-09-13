"""Tests for ``ck add`` input sanitization.

ANSI escape codes, control characters, and whitespace noise pasted
into the CLI (e.g. piped command output) must never reach PLAN.md.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.core import ContextKeeper
from cklib.parser import parse_plan_file, sanitize_task_text


class _IsolatedHome:
    """Pin the global registry to a per-test tmp HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._orig_dir = ckconfig.GLOBAL_CONFIG_DIR
        self._orig_file = ckconfig.GLOBAL_REGISTRY_FILE
        self._orig_legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE

        new_global_dir = fake_home / ".config" / "ck"
        ckconfig.GLOBAL_CONFIG_DIR = new_global_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_global_dir / "projects.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"
        ckregistry.GLOBAL_CONFIG_DIR = ckconfig.GLOBAL_CONFIG_DIR
        ckregistry.GLOBAL_REGISTRY_FILE = ckconfig.GLOBAL_REGISTRY_FILE
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = ckconfig.LEGACY_GLOBAL_CONFIG_FILE

    def tearDown(self):
        ckconfig.GLOBAL_CONFIG_DIR = self._orig_dir
        ckconfig.GLOBAL_REGISTRY_FILE = self._orig_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
        ckregistry.GLOBAL_CONFIG_DIR = self._orig_dir
        ckregistry.GLOBAL_REGISTRY_FILE = self._orig_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy


class TestSanitizeTaskText(unittest.TestCase):
    """Pure-function behaviour of ``sanitize_task_text``."""

    def test_strips_csi_color_codes(self):
        raw = "\x1b[32m\x1b[1mFix login bug\x1b[0m"
        self.assertEqual(sanitize_task_text(raw), "Fix login bug")

    def test_strips_cursor_and_erase_sequences(self):
        raw = "\x1b[2K\x1b[1G\x1b[?25lRewrite parser\x1b[?25h"
        self.assertEqual(sanitize_task_text(raw), "Rewrite parser")

    def test_strips_osc_sequences(self):
        raw = "\x1b]0;window title\x07Deploy service"
        self.assertEqual(sanitize_task_text(raw), "Deploy service")

    def test_strips_osc_with_st_terminator(self):
        raw = "\x1b]8;;https://example.com\x1b\\link text\x1b]8;;\x1b\\"
        self.assertEqual(sanitize_task_text(raw), "link text")

    def test_strips_dcs_sequences(self):
        raw = "\x1bP1;2q\x1b\\Query optimizer\x1bP\x1b\\"
        self.assertEqual(sanitize_task_text(raw), "Query optimizer")

    def test_strips_charset_and_short_escapes(self):
        raw = "\x1b(B\x1b)0\x1b7\x1b8Render chart\x1bM"
        self.assertEqual(sanitize_task_text(raw), "Render chart")

    def test_strips_control_characters(self):
        raw = "Re\x00move\x7f me\x01\x02 and\x0b this"
        self.assertEqual(sanitize_task_text(raw), "Remove me and this")

    def test_normalizes_whitespace(self):
        raw = "  Fix\t\tthe \n\n  parser   race  "
        self.assertEqual(sanitize_task_text(raw), "Fix the parser race")

    def test_clean_text_unchanged(self):
        self.assertEqual(sanitize_task_text("Refactor core module"),
                         "Refactor core module")

    def test_idempotent(self):
        once = sanitize_task_text("\x1b[31mBuild CI\x1b[0m pipeline")
        self.assertEqual(sanitize_task_text(once), once)

    def test_pure_noise_yields_empty(self):
        self.assertEqual(sanitize_task_text("\x1b[2K\x1b[0m\x07"), "")
        self.assertEqual(sanitize_task_text("   \t\n  "), "")

    def test_preserves_unicode_content(self):
        self.assertEqual(sanitize_task_text("Описать первую задачу"),
                         "Описать первую задачу")


class TestAddTaskSanitization(_IsolatedHome, unittest.TestCase):
    """``ck add`` must persist only sanitized titles into PLAN.md."""

    def _ck(self, tmp: Path) -> ContextKeeper:
        root = tmp / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()
        return ck

    def test_add_strips_ansi_and_writes_clean_plan(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td))
            new_id = ck.add_task("\x1b[33m\x1b[1mDeploy to prod\x1b[0m")

            self.assertEqual(new_id, 2)  # after the default init task
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [ ] Deploy to prod", text)
            self.assertNotIn("\x1b", text)

    def test_add_normalizes_whitespace_in_plan(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td))
            ck.add_task("Fix   the\t\tparser\n\n race")
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [ ] Fix the parser race", text)

    def test_add_rejects_pure_noise(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td))
            with self.assertRaises(ValueError):
                ck.add_task("\x1b[2K\x1b[0m\x07")

    def test_sanitized_title_survives_reparse(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck(Path(td))
            ck.add_task("\x1b[32mWrite unit tests\x1b[0m for parser")
            tl = parse_plan_file(ck.plan_file)
            titles = [t.title for t in tl.tasks]
            self.assertIn("Write unit tests for parser", titles)
            self.assertTrue(all("\x1b" not in t.title for t in tl.tasks))


if __name__ == "__main__":
    unittest.main()
