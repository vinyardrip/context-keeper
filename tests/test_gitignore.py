"""Tests for the root ``.gitignore`` modifier."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cklib.core import ContextKeeper


class TestEnsureRootGitignore(unittest.TestCase):
    def test_creates_gitignore_if_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gi = root / ".gitignore"
            self.assertFalse(gi.exists())

            added = ContextKeeper._ensure_root_gitignore(root)
            self.assertTrue(gi.exists())
            self.assertIn(".ck/*.bak", added)
            self.assertIn(".ck/state.json", added)
            text = gi.read_text(encoding="utf-8")
            self.assertIn(".ck/*.bak", text)
            self.assertIn(".ck/state.json", text)

    def test_appends_only_missing_entries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gi = root / ".gitignore"
            gi.write_text("# Pre-existing\nnode_modules\n", encoding="utf-8")

            added = ContextKeeper._ensure_root_gitignore(root)
            self.assertIn(".ck/*.bak", added)
            self.assertIn(".ck/state.json", added)

            added_again = ContextKeeper._ensure_root_gitignore(root)
            self.assertEqual(added_again, [])

            text = gi.read_text(encoding="utf-8")
            self.assertIn("node_modules", text)
            self.assertIn("Context Keeper", text)

    def test_does_not_remove_tracked_entries_when_no_blanket(self):
        """Without a blanket ignore rule, we do not append negations."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gi = root / ".gitignore"
            gi.write_text(".ck/*.bak\n", encoding="utf-8")
            added = ContextKeeper._ensure_root_gitignore(root)
            self.assertNotIn("!PLAN.md", " ".join(added))


class TestBlanketIgnoreNegation(unittest.TestCase):
    """When a blanket ignore rule exists, ``!.ck/PLAN.md`` etc. are appended."""

    def test_blanket_star_triggers_negations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gi = root / ".gitignore"
            gi.write_text("*\n", encoding="utf-8")

            added = ContextKeeper._ensure_root_gitignore(root)
            text = gi.read_text(encoding="utf-8")
            self.assertIn("!.ck/PLAN.md", text)
            self.assertIn("!.ck/prompt.md", text)
            self.assertIn("!.ck/README.md", text)
            self.assertIn("!.ck/PLAN.md", added)
            self.assertIn("!.ck/prompt.md", added)
            self.assertIn("!.ck/README.md", added)

    def test_blanket_ck_dir_triggers_negations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gi = root / ".gitignore"
            gi.write_text(".ck/\n", encoding="utf-8")

            added = ContextKeeper._ensure_root_gitignore(root)
            text = gi.read_text(encoding="utf-8")
            self.assertIn("!.ck/PLAN.md", text)
            self.assertIn("!.ck/prompt.md", text)
            self.assertIn("!.ck/README.md", text)

    def test_no_negations_when_no_blanket(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gi = root / ".gitignore"
            gi.write_text("node_modules\n__pycache__\n", encoding="utf-8")

            added = ContextKeeper._ensure_root_gitignore(root)
            text = gi.read_text(encoding="utf-8")
            self.assertNotIn("!.ck/", text)

    def test_negations_are_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gi = root / ".gitignore"
            gi.write_text("*\n", encoding="utf-8")

            ContextKeeper._ensure_root_gitignore(root)
            first = gi.read_text(encoding="utf-8")
            ContextKeeper._ensure_root_gitignore(root)
            second = gi.read_text(encoding="utf-8")
            self.assertEqual(first, second)

    def test_blanket_detect_helper(self):
        self.assertTrue(ContextKeeper._detect_blanket_ignore("*\n"))
        self.assertTrue(ContextKeeper._detect_blanket_ignore(".\n"))
        self.assertTrue(ContextKeeper._detect_blanket_ignore("?*\n"))
        self.assertFalse(ContextKeeper._detect_blanket_ignore("node_modules\n"))
        self.assertFalse(ContextKeeper._detect_blanket_ignore("# nothing\n"))


class TestEnsureCkGitignore(unittest.TestCase):
    def test_ck_gitignore_created(self):
        with tempfile.TemporaryDirectory() as td:
            ck = Path(td) / ".ck"
            ck.mkdir()
            ContextKeeper._ensure_ck_gitignore(ck)
            gi = ck / ".gitignore"
            self.assertTrue(gi.exists())
            text = gi.read_text(encoding="utf-8")
            self.assertIn("*.bak", text)
            self.assertIn("state.json", text)

    def test_ck_gitignore_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            ck = Path(td) / ".ck"
            ck.mkdir()
            ContextKeeper._ensure_ck_gitignore(ck)
            first = (ck / ".gitignore").read_text(encoding="utf-8")
            ContextKeeper._ensure_ck_gitignore(ck)
            second = (ck / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()