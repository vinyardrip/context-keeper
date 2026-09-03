"""Tests for the save flow (two-step Notes + local commit) and
graceful decline when Git is unavailable."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Callable

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.core import ContextKeeper
from cklib.models import TaskStatus
from cklib.parser import parse_plan_file


class IsolatedHomeMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._orig_dir = ckconfig.GLOBAL_CONFIG_DIR
        self._orig_file = ckconfig.GLOBAL_REGISTRY_FILE
        self._orig_legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE

        new_global_dir = fake_home / ".config" / "ck"
        new_registry_file = new_global_dir / "projects.json"
        new_legacy = fake_home / ".ckrc"

        ckconfig.GLOBAL_CONFIG_DIR = new_global_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_registry_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = new_legacy
        ckregistry.GLOBAL_CONFIG_DIR = new_global_dir
        ckregistry.GLOBAL_REGISTRY_FILE = new_registry_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = new_legacy

    def tearDown(self):
        ckconfig.GLOBAL_CONFIG_DIR = self._orig_dir
        ckconfig.GLOBAL_REGISTRY_FILE = self._orig_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
        ckregistry.GLOBAL_CONFIG_DIR = self._orig_dir
        ckregistry.GLOBAL_REGISTRY_FILE = self._orig_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy


def _make_ck(tmp: Path) -> ContextKeeper:
    root = tmp / "project"
    root.mkdir()
    ck = ContextKeeper(root=root)
    ck.init()
    ck.add_task("alpha")
    ck.add_task("beta")
    ck.add_task("gamma")
    return ck


class TestSaveGracefulDecline(IsolatedHomeMixin, unittest.TestCase):
    """Declining Git init must NOT raise or trigger further Git prompts."""

    def test_decline_git_init_exits_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = _make_ck(tmp)

            prompts = [
                "s",            # Step 1: skip note
                "some summary", # short summary
                "n",            # decline git init
                # After "n", no further Git prompts must be issued.
            ]
            iter_prompts = iter(prompts)

            def fake_input(prompt: str = "") -> str:
                return next(iter_prompts)

            captured = []
            printer = lambda s: captured.append(s)

            # Ensure no Git repo exists in the tmp project
            from cklib import git as gith_mod
            orig_is_repo = gith_mod.is_git_repo
            orig_init = gith_mod.init_repo
            gith_mod.is_git_repo = lambda path=None: False
            gith_mod.init_repo = lambda path=None: True
            try:
                result = ck.save(input_fn=fake_input, printer=printer)
            finally:
                gith_mod.is_git_repo = orig_is_repo
                gith_mod.init_repo = orig_init

            self.assertIsNone(result)

            # Verify that the commit-message prompt was never issued
            combined = "\n".join(captured) + "\n".join(prompts)
            # The "commit message" prompt must NOT appear because we
            # bypassed the entire commit step.
            self.assertNotIn("Create local commit", combined)
            self.assertNotIn("Commit message", combined)

            # The "no commit" message must appear
            self.assertTrue(
                any("No commit" in line for line in captured),
                f"Expected 'No commit' message, got: {captured!r}",
            )

    def test_commit_phase_with_git_repo(self):
        """When a Git repo exists, commit prompts are issued and
        ``local_commit`` is invoked."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = _make_ck(tmp)

            from cklib import git as gith_mod

            prompts = [
                "s",                  # skip note
                "implemented alpha",
                "",                   # accept default commit message
                "y",                  # confirm commit
            ]
            iter_prompts = iter(prompts)

            def fake_input(prompt: str = "") -> str:
                return next(iter_prompts)

            commit_calls = []
            orig_is_repo = gith_mod.is_git_repo
            orig_local = gith_mod.local_commit
            gith_mod.is_git_repo = lambda path=None: True
            gith_mod.local_commit = lambda msg, path=None: (
                commit_calls.append(msg) or True
            )
            try:
                result = ck.save(input_fn=fake_input)
            finally:
                gith_mod.is_git_repo = orig_is_repo
                gith_mod.local_commit = orig_local

            self.assertIsNotNone(result)
            self.assertEqual(len(commit_calls), 1)


class TestAddTaskIsAstBased(IsolatedHomeMixin, unittest.TestCase):
    def test_add_returns_id_and_writes_canonical(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = _make_ck(tmp)

            # init() creates a default task (id=1); add_task 3 times
            # gives ids 2, 3, 4. The new add returns 5.
            new_id = ck.add_task("delta")
            self.assertEqual(new_id, 5)
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [ ] delta", text)
            self.assertNotIn("- [] delta", text)

    def test_add_when_no_plan_creates_one(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            root = tmp / "fresh"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.add_task("first task")
            self.assertTrue(ck.plan_file.exists())
            self.assertIn("- [ ] first task",
                          ck.plan_file.read_text(encoding="utf-8"))


class TestStartAndDone(IsolatedHomeMixin, unittest.TestCase):
    def test_start_marks_focused(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = _make_ck(tmp)
            ck.start(2)
            tl = parse_plan_file(ck.plan_file)
            self.assertEqual(tl.focused[0].id, 2)
            self.assertEqual(tl.by_id(1).status, TaskStatus.OPEN)

    def test_done_marks_single_and_ranges(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = _make_ck(tmp)
            ck.done("1")
            ck.done("2-3")
            ck.done("4")
            tl = parse_plan_file(ck.plan_file)
            self.assertTrue(all(t.status == TaskStatus.DONE for t in tl.tasks))

    def test_done_with_unknown_id_raises(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = _make_ck(tmp)
            with self.assertRaises(KeyError):
                ck.done("999")


if __name__ == "__main__":
    unittest.main()