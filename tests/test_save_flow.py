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

            def fake_local_commit(msg, path=None, stage=None):
                commit_calls.append((msg, stage))
                return True

            gith_mod.local_commit = fake_local_commit
            try:
                result = ck.save(input_fn=fake_input)
            finally:
                gith_mod.is_git_repo = orig_is_repo
                gith_mod.local_commit = orig_local

            self.assertIsNotNone(result)
            self.assertEqual(len(commit_calls), 1)
            # H-1: the commit must stage ONLY the explicit .ck
            # context files — never ``git add .`` and never the raw
            # .ck directory (which would include *.lock files).
            msg, stage = commit_calls[0]
            self.assertEqual(stage, [
                ".ck/PLAN.md",
                ".ck/HISTORY.md",
                ".ck/prompt.md",
                ".ck/README.md",
                ".ck/.gitignore",
            ])


class TestSaveUxWording(IsolatedHomeMixin, unittest.TestCase):
    """Step 4: explicit save confirmation + history-vs-Git clarity."""

    def _make_ck(self, tmp: Path) -> ContextKeeper:
        root = tmp / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()
        ck.add_task("alpha")
        return ck

    def _run_save(self, ck, prompts):
        iter_prompts = iter(prompts)

        def fake_input(prompt: str = "") -> str:
            return next(iter_prompts)

        captured = []
        printer = lambda s: captured.append(s)
        result = ck.save(input_fn=fake_input, printer=printer)
        return result, captured

    def test_note_append_prints_explicit_confirmation(self):
        """Whenever an entry lands in HISTORY.md, STDOUT says so with
        the relative path and the not-a-Git-commit qualifier."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._make_ck(Path(td))
            result, captured = self._run_save(ck, [
                "s",            # skip note body
                "did the thing",
                "n",            # decline commit
            ])
            self.assertIsNone(result)
            self.assertTrue(
                any(
                    "Saved entry to .ck/HISTORY.md" in line
                    and "not a Git commit" in line
                    for line in captured
                ),
                f"missing explicit confirmation, got: {captured!r}",
            )
            # The entry is actually on disk
            self.assertIn("did the thing",
                          ck.history_file.read_text(encoding="utf-8"))

    def test_confirmation_appears_in_commit_path_too(self):
        """The explicit HISTORY.md confirmation prints before the Git
        phase even when the user goes on to commit."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._make_ck(Path(td))

            from cklib import git as gith_mod
            orig_is_repo = gith_mod.is_git_repo
            orig_local = gith_mod.local_commit
            gith_mod.is_git_repo = lambda path=None: True
            gith_mod.local_commit = lambda msg, path=None, stage=None: True
            try:
                result, captured = self._run_save(ck, [
                    "s",            # skip note body
                    "summary",
                    "",             # default commit message
                    "y",            # confirm commit
                ])
            finally:
                gith_mod.is_git_repo = orig_is_repo
                gith_mod.local_commit = orig_local

            self.assertIsNotNone(result)
            self.assertTrue(any(
                "Saved entry to .ck/HISTORY.md" in line for line in captured
            ))
            # Git success message mentions local-only, no push
            self.assertTrue(any("no push" in line for line in captured))

    def test_step_headers_distinguish_history_and_git(self):
        """The flow announces Step 1 (local history) and Step 2 (Git
        commit) as clearly separate phases."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._make_ck(Path(td))
            result, captured = self._run_save(ck, [
                "s", "summary", "n",
            ])
            self.assertIsNone(result)
            self.assertTrue(any(
                "Step 1: local history" in line and ".ck/HISTORY.md" in line
                for line in captured
            ))
            self.assertTrue(any(
                "Step 2: Git commit" in line for line in captured
            ))

    def test_decline_commit_message_clarifies_history_is_saved(self):
        """Declining the Git commit must reassure the user the local
        history entry is already saved."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._make_ck(Path(td))

            from cklib import git as gith_mod
            orig_is_repo = gith_mod.is_git_repo
            gith_mod.is_git_repo = lambda path=None: True
            try:
                result, captured = self._run_save(ck, [
                    "s",            # skip note body
                    "summary",
                    "",             # default commit message
                    "n",            # decline the commit itself
                ])
            finally:
                gith_mod.is_git_repo = orig_is_repo

            self.assertIsNone(result)
            decline_msg = "\n".join(captured)
            self.assertIn("No commit created", decline_msg)
            self.assertIn(".ck/HISTORY.md", decline_msg)
            self.assertIn("local history only", decline_msg)

    def test_no_repo_decline_clarifies_history_is_saved(self):
        """Declining Git init must reassure the user the local history
        entry is already saved."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._make_ck(Path(td))

            from cklib import git as gith_mod
            orig_is_repo = gith_mod.is_git_repo
            gith_mod.is_git_repo = lambda path=None: False
            try:
                result, captured = self._run_save(ck, [
                    "s",            # skip note body
                    "summary",
                    "n",            # decline git init
                ])
            finally:
                gith_mod.is_git_repo = orig_is_repo

            self.assertIsNone(result)
            decline_msg = "\n".join(captured)
            self.assertIn("No commit created", decline_msg)
            self.assertIn(".ck/HISTORY.md", decline_msg)
            self.assertIn("local history only", decline_msg)

    def test_eof_after_note_reports_history_saved(self):
        """EOF mid-flow (after the entry is durable, before the Git
        phase completes) still reports the HISTORY.md entry."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._make_ck(Path(td))

            from cklib import git as gith_mod
            orig_is_repo = gith_mod.is_git_repo
            gith_mod.is_git_repo = lambda path=None: True

            def fake_input(prompt: str = "") -> str:
                if "note for this task" in prompt:
                    return "s"
                if "Short summary" in prompt:
                    return "summary"
                # Git phase: stdin closes mid-flow
                raise EOFError

            captured = []
            printer = lambda s: captured.append(s)
            try:
                result = ck.save(input_fn=fake_input, printer=printer)
            finally:
                gith_mod.is_git_repo = orig_is_repo

            self.assertIsNone(result)
            combined = "\n".join(captured)
            self.assertIn("Saved entry to .ck/HISTORY.md", combined)
            self.assertIn("entry saved to .ck/HISTORY.md", combined)
            self.assertIn("no Git commit created", combined)

    def test_prompt_wording_distinguishes_git_commit(self):
        """The commit prompts explicitly say 'Git'."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._make_ck(Path(td))

            from cklib import git as gith_mod
            orig_is_repo = gith_mod.is_git_repo
            gith_mod.is_git_repo = lambda path=None: True
            prompts_seen = []

            def fake_input(prompt: str = "") -> str:
                prompts_seen.append(prompt)
                if "note for this task" in prompt:
                    return "s"
                if "Short summary" in prompt:
                    return "summary"
                if "commit message" in prompt.lower():
                    return ""
                return "n"

            try:
                result = ck.save(input_fn=fake_input)
            finally:
                gith_mod.is_git_repo = orig_is_repo

            self.assertIsNone(result)
            commit_prompt = next(
                p for p in prompts_seen if "commit message" in p.lower()
            )
            self.assertIn("Git commit message", commit_prompt)
            confirm_prompt = next(
                p for p in prompts_seen if "Create" in p and "commit" in p
            )
            self.assertIn("LOCAL Git commit", confirm_prompt)


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