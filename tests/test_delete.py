"""Tests for task deletion (``ck delete <ID>`` / ``ck rm <ID>``).

Coverage:

- AST deletion drops the task's source bullet and renumbers the
  remaining IDs 1..N in document order (no gaps, no duplicates).
- ``ContextKeeper.delete_task`` preserves headers/prose, refocuses the
  next task when the focused one is removed, and reconciles
  ``.ck/state.json`` (active + paused note IDs shift, notes bound to
  the deleted task are archived into HISTORY.md, never silently lost).
- CLI behaviour: the ID/title and target space are shown, a ``[y/N]``
  answer confirms/aborts, a non-interactive run refuses without
  ``-f``, ``-f`` / ``--force`` bypasses the prompt, and the ``rm``
  alias is identical.
- Cross-space deletion (``-s`` / ``--space``): the target space's
  file is mutated/re-indexed, its notes are archived into
  ``<space>.HISTORY.md``, the sidecar IDs shift, and the active
  project's ``PLAN.md`` / ``.ck/state.json`` stay byte-identical.
- Error paths: unknown ID, unknown/unsafe space, non-numeric ID (with
  the explicit ``ck space delete`` pointer), missing/extra arguments,
  unknown flags, and no bound project — all clean, no traceback.
- Architectural distinction: task deletion never removes a global
  space; whole-space removal stays ``ck space delete``.
"""

from __future__ import annotations

# Filesystem isolation safety net (see tests/__init__).
import tests  # noqa: F401

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import spaces
from cklib.cli import main
from cklib.core import ContextKeeper
from cklib.parser import parse_plan, render_plan


PLAN = (
    "# P\n"
    "\n"
    "## Current Sprint\n"
    "- [ ] alpha\n"
    "- [ ] beta\n"
    "- [ ] gamma\n"
    "\n"
    "## Completed\n"
)


def _run_main(argv, *, tty=False, answer=None, prompt_log=None):
    """Run the CLI in-process; return (rc, stdout, stderr).

    ``prompt_log`` (a list) captures the exact prompt passed to
    ``input()`` so tests can assert the target space/count wording.
    """
    patches = [
        mock.patch("sys.stdin.isatty", return_value=tty),
        mock.patch("sys.stdout.isatty", return_value=tty),
    ]
    if answer is not None:
        def _answer(prompt=""):
            if prompt_log is not None:
                prompt_log.append(prompt)
            return answer

        patches.append(mock.patch("builtins.input", side_effect=_answer))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        for patcher in patches:
            patcher.start()
        try:
            rc = main(argv)
        finally:
            for patcher in reversed(patches):
                patcher.stop()
    return rc, out.getvalue(), err.getvalue()


class _Harness(unittest.TestCase):
    """Isolated registry + a real project directory bound in cwd."""

    PLAN = PLAN

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)

        # Global config + registry isolation: `_sync_active_task`
        # touches projects.json, and space deletion/history uses the
        # spaces tree under the same isolated config root.
        self._orig = (
            ckconfig.GLOBAL_CONFIG_DIR,
            ckconfig.GLOBAL_REGISTRY_FILE,
            ckconfig.GLOBAL_STATE_FILE,
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE,
            ckregistry.GLOBAL_CONFIG_DIR,
            ckregistry.GLOBAL_REGISTRY_FILE,
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE,
        )
        new_dir = home / ".config" / "ck"
        new_file = new_dir / "projects.json"
        new_legacy = home / ".ckrc"
        ckconfig.GLOBAL_CONFIG_DIR = new_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_file
        ckconfig.GLOBAL_STATE_FILE = new_dir / "state.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = new_legacy
        ckregistry.GLOBAL_CONFIG_DIR = new_dir
        ckregistry.GLOBAL_REGISTRY_FILE = new_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = new_legacy

        def _restore() -> None:
            (ckconfig.GLOBAL_CONFIG_DIR,
             ckconfig.GLOBAL_REGISTRY_FILE,
             ckconfig.GLOBAL_STATE_FILE,
             ckconfig.LEGACY_GLOBAL_CONFIG_FILE,
             ckregistry.GLOBAL_CONFIG_DIR,
             ckregistry.GLOBAL_REGISTRY_FILE,
             ckregistry.LEGACY_GLOBAL_CONFIG_FILE) = self._orig
        self.addCleanup(_restore)

        self.root = home / "project"
        self.root.mkdir()
        self.ck = ContextKeeper(root=self.root)
        self.ck.ck_path.mkdir(parents=True, exist_ok=True)
        self.ck.plan_file.write_text(self.PLAN, encoding="utf-8")

        self._old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._old_cwd)

    # ---- helpers ----

    @property
    def plan_path(self) -> Path:
        return self.ck.plan_file

    def plan_text(self) -> str:
        return self.plan_path.read_text(encoding="utf-8")

    def reloaded(self):
        return parse_plan(self.plan_text())

    def ids(self) -> list:
        return [t.id for t in self.reloaded().tasks]

    def titles(self) -> list:
        return [t.title for t in self.reloaded().tasks]

    def state(self) -> dict:
        self.assertTrue(self.ck.state_file.exists())
        return json.loads(self.ck.state_file.read_text(encoding="utf-8"))

    def make_space(self, name: str, tasks: tuple = (
            "alpha", "beta", "gamma")) -> spaces.SpaceManager:
        """Create a real space file + tasks in the isolated config."""
        spaces.create_space(name)
        manager = spaces.SpaceManager(name)
        for title in tasks:
            manager.add_task(title)
        return manager


# ---------------------------------------------------------------------------
# AST-level deletion
# ---------------------------------------------------------------------------


class TestAstDelete(unittest.TestCase):

    def test_delete_drops_source_line_and_renumbers(self):
        tl = parse_plan("- [ ] a\n- [ ] b\n- [ ] c\n")
        removed = tl.delete(2)
        self.assertEqual(removed.title, "b")
        self.assertEqual([t.id for t in tl.tasks], [1, 2])
        self.assertEqual([t.title for t in tl.tasks], ["a", "c"])
        self.assertEqual(render_plan(tl), "- [ ] a\n- [ ] c\n")

    def test_delete_keeps_headers_and_prose(self):
        tl = parse_plan(
            "# P\n\n## Current Sprint\nintro prose\n- [ ] a\n- [ ] b\n")
        tl.delete(1)
        out = render_plan(tl)
        self.assertIn("intro prose", out)
        self.assertNotIn("- [ ] a", out)
        self.assertIn("- [ ] b", out)

    def test_delete_unknown_id_raises(self):
        tl = parse_plan("- [ ] a\n")
        with self.assertRaises(KeyError):
            tl.delete(9)

    def test_render_without_deletions_is_unchanged(self):
        src = "- [ ] a\n- [ ] b\n"
        self.assertEqual(render_plan(parse_plan(src)), src)


# ---------------------------------------------------------------------------
# Domain deletion
# ---------------------------------------------------------------------------


class TestDeleteTaskCore(_Harness):

    def test_delete_removes_task_and_reports_remaining(self):
        result = self.ck.delete_task(2)
        self.assertEqual(result["id"], 2)
        self.assertEqual(result["title"], "beta")
        self.assertEqual(result["remaining"], 2)
        self.assertEqual(result["notes_archived"], 0)
        self.assertEqual(self.titles(), ["alpha", "gamma"])
        self.assertEqual(self.ids(), [1, 2])
        # No broken sequence / duplicate IDs remain.
        self.assertEqual(self.reloaded().gap_ids(), [])
        self.assertNotIn("beta", self.plan_text())
        # Headers preserved.
        self.assertIn("## Current Sprint", self.plan_text())
        self.assertIn("## Completed", self.plan_text())

    def test_delete_first_task_reindexes_contiguously(self):
        self.ck.delete_task(1)
        self.assertEqual(self.titles(), ["beta", "gamma"])
        self.assertEqual(self.ids(), [1, 2])

    def test_delete_completed_task_is_allowed(self):
        self.ck.done("2")
        self.ck.delete_task(2)
        self.assertEqual(self.titles(), ["alpha", "gamma"])
        self.assertEqual(self.ids(), [1, 2])

    def test_delete_unknown_id_is_keyerror_and_plan_untouched(self):
        before = self.plan_text()
        with self.assertRaises(KeyError):
            self.ck.delete_task(99)
        self.assertEqual(self.plan_text(), before)

    def test_focus_survives_deletion_of_earlier_task(self):
        self.ck.start(2)  # beta focused
        self.ck.delete_task(1)
        tl = self.reloaded()
        self.assertEqual([t.title for t in tl.tasks], ["beta", "gamma"])
        self.assertEqual([t.title for t in tl.focused], ["beta"])
        self.assertEqual(tl.focused[0].id, 1)

    def test_deleting_focused_task_clears_focus(self):
        self.ck.start(2)
        self.ck.delete_task(2)
        self.assertEqual(self.reloaded().focused, [])

    def test_delete_reindexes_so_later_done_hits_right_task(self):
        self.ck.delete_task(2)
        self.ck.done("1")
        tl = self.reloaded()
        self.assertEqual([t.title for t in tl.done], ["alpha"])
        self.assertEqual([t.title for t in tl.open], ["gamma"])


class TestDeleteStateReconciliation(_Harness):

    def test_active_note_id_shifts_after_earlier_deletion(self):
        self.ck.start(3)
        self.ck.set_note("gamma scratchpad")
        self.ck.delete_task(1)

        active = self.state()["active_task"]
        self.assertEqual(active["id"], 2)
        self.assertEqual(active["title"], "gamma")
        self.assertEqual(active["note"], "gamma scratchpad")
        # And the note still renders on the right (renumbered) task.
        self.assertIn("* Note: gamma scratchpad", self.ck.status())

    def test_note_of_deleted_task_is_archived_not_dropped(self):
        self.ck.start(2)
        self.ck.set_note("half-done mapping")
        result = self.ck.delete_task(2)

        self.assertEqual(result["notes_archived"], 1)
        self.assertIn("half-done mapping",
                      self.ck.history_file.read_text(encoding="utf-8"))
        self.assertNotIn("active_task", self.state())

    def test_paused_note_id_shifts_after_deletion(self):
        self.ck.start(2)
        self.ck.set_note("paused beta note")
        self.ck.start(3)  # beta loses focus -> paused registry entry
        self.ck.delete_task(1)

        paused = self.state()["paused_tasks"]
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0]["id"], 1)
        self.assertEqual(paused[0]["title"], "beta")
        self.assertEqual(paused[0]["note"], "paused beta note")
        self.assertIn("paused beta note", self.ck.notes())

    def test_deleting_paused_task_archives_its_note(self):
        self.ck.start(2)
        self.ck.set_note("paused beta note")
        self.ck.start(3)
        result = self.ck.delete_task(2)

        self.assertEqual(result["notes_archived"], 1)
        self.assertIn("paused beta note",
                      self.ck.history_file.read_text(encoding="utf-8"))
        state = self.state()
        self.assertEqual(state.get("paused_tasks"), [])

    def test_no_state_file_is_created_by_a_bare_deletion(self):
        self.ck.delete_task(1)
        self.assertFalse(self.ck.state_file.exists())


# ---------------------------------------------------------------------------
# CLI: confirmation, force and output
# ---------------------------------------------------------------------------


class TestSpaceTaskDeleteDomain(_Harness):
    """``SpaceManager.delete_task`` — AST, sidecar and history isolation."""

    def test_delete_reindexes_and_archives_bound_note(self):
        manager = self.make_space("work")
        manager.focus(2)
        manager.set_note(2, "half-done beta")
        manager.set_note(3, "gamma scratchpad")

        result = manager.delete_task(2)

        self.assertEqual(result["id"], 2)
        self.assertEqual(result["title"], "beta")
        self.assertEqual(result["space"], "work")
        self.assertEqual(result["remaining"], 2)
        self.assertEqual(result["notes_archived"], 1)
        self.assertEqual([t.title for t in manager.load().tasks],
                         ["alpha", "gamma"])
        self.assertEqual([t.id for t in manager.load().tasks], [1, 2])

        history = spaces.history_path("work")
        self.assertTrue(history.is_file())
        entry = history.read_text(encoding="utf-8")
        self.assertIn("half-done beta", entry)
        self.assertIn("[2] beta", entry)
        self.assertNotIn("gamma scratchpad", entry)

        # The deleted note is gone; the later task's note followed the
        # positional re-index from ID 3 to ID 2.
        self.assertEqual(manager.get_note(2), "gamma scratchpad")
        self.assertEqual(manager.get_note(3), "")

    def test_delete_unknown_id_does_not_write_history(self):
        manager = self.make_space("work")
        with self.assertRaises(KeyError):
            manager.delete_task(99)
        self.assertFalse(spaces.history_path("work").exists())

    def test_history_name_is_not_a_routable_discovered_space(self):
        self.make_space("work")
        history = spaces.history_path("work")
        history.write_text("# audit\n", encoding="utf-8")

        self.assertNotIn("work.HISTORY", spaces.existing_space_names())
        self.assertFalse(spaces.is_valid_space("work.HISTORY"))
        self.assertIsNone(spaces.routable_space_name("work.HISTORY"))
        with self.assertRaises(ValueError):
            spaces.validate_new_space_name("work.HISTORY")


class TestCrossSpaceDeleteCli(_Harness):
    """CLI ``ck delete <ID> -s <space>`` / ``--space`` behaviour."""

    def _space_titles(self, name: str = "work") -> list:
        return [t.title for t in spaces.SpaceManager(name).load().tasks]

    def _space_text(self, name: str = "work") -> str:
        return spaces.space_path(name).read_text(encoding="utf-8")

    def test_short_space_flag_with_force(self):
        self.make_space("work")
        before_project = self.plan_text()
        rc, out, err = _run_main(
            ["delete", "2", "-s", "work", "-f"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Task #2: beta", out)
        self.assertIn("space 'work'", out)
        self.assertEqual(self._space_titles(), ["alpha", "gamma"])
        self.assertEqual(self.plan_text(), before_project)

    def test_long_space_flag(self):
        self.make_space("work")
        rc, out, err = _run_main(
            ["delete", "1", "--space", "work", "--force"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._space_titles(), ["beta", "gamma"])

    def test_space_equals_form(self):
        self.make_space("work")
        rc, _, err = _run_main(
            ["delete", "1", "--space=work", "-f"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._space_titles(), ["beta", "gamma"])

    def test_rm_alias_with_space(self):
        self.make_space("work")
        rc, out, err = _run_main(["rm", "3", "-s", "work", "-f"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Task #3: gamma", out)
        self.assertEqual(self._space_titles(), ["alpha", "beta"])

    def test_interactive_prompt_names_space_and_task(self):
        self.make_space("work")
        prompts: list = []
        rc, out, err = _run_main(
            ["delete", "2", "--space", "work"],
            tty=True, answer="y", prompt_log=prompts)
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            prompts,
            ["Permanently delete task #2 from space 'work'? [y/N]: "])
        self.assertEqual(self._space_titles(), ["alpha", "gamma"])

    def test_interactive_no_aborts(self):
        self.make_space("work")
        before = self._space_text()
        rc, out, _ = _run_main(
            ["delete", "2", "-s", "work"], tty=True, answer="n")
        self.assertEqual(rc, 2)
        self.assertIn("Aborted.", out)
        self.assertEqual(self._space_text(), before)

    def test_non_interactive_without_force_refuses(self):
        self.make_space("work")
        before = self._space_text()
        rc, out, err = _run_main(["delete", "2", "-s", "work"])
        self.assertEqual(rc, 2)
        self.assertIn("Refusing to delete task #2 from space 'work'", out)
        self.assertIn("Pass -f to confirm", out)
        self.assertEqual(self._space_text(), before)
        self.assertNotIn("Traceback", err)

    def test_force_bypasses_prompt_and_archives_note(self):
        manager = self.make_space("work")
        manager.focus(2)
        manager.set_note(2, "cross-space note")
        rc, out, err = _run_main(
            ["delete", "2", "-s", "work", "-f"])
        self.assertEqual(rc, 0, err)
        history = spaces.history_path("work").read_text(encoding="utf-8")
        self.assertIn("cross-space note", history)
        self.assertNotIn("cross-space note",
                         spaces.state_path("work").read_text(encoding="utf-8"))
        self.assertIn("Archived 1 process note(s)", out)

    def test_explicit_project_space_targets_the_project(self):
        self.make_space("work")
        before_space = self._space_text()
        rc, out, err = _run_main(
            ["delete", "2", "-s", "project", "-f"])
        self.assertEqual(rc, 0, err)
        self.assertIn("from space 'project'", out)
        self.assertEqual(self._space_text(), before_space)
        self.assertEqual(self.titles(), ["alpha", "gamma"])

    def test_cross_space_delete_works_outside_a_project(self):
        self.make_space("work")
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        os.chdir(outside)
        rc, _, err = _run_main(["delete", "2", "-s", "work", "-f"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._space_titles(), ["alpha", "gamma"])

    def test_unknown_space_is_a_clean_not_found(self):
        before = self.plan_text()
        rc, out, err = _run_main(["delete", "1", "-s", "ghost", "-f"])
        self.assertEqual(rc, 1)
        self.assertIn("Space 'ghost' not found", out)
        self.assertEqual(self.plan_text(), before)
        self.assertNotIn("Traceback", err)

    def test_unsafe_space_name_is_a_clean_usage_error(self):
        rc, out, err = _run_main(
            ["delete", "1", "-s", "../escape", "-f"])
        self.assertEqual(rc, 2)
        self.assertIn("Invalid space name", out)
        self.assertNotIn("Traceback", err)

    def test_unknown_task_in_space_is_clean(self):
        self.make_space("work")
        before = self._space_text()
        rc, out, err = _run_main(["delete", "99", "-s", "work", "-f"])
        self.assertEqual(rc, 2)
        self.assertIn("No task with id 99", out)
        self.assertEqual(self._space_text(), before)
        self.assertNotIn("Traceback", err)

    def test_missing_space_value_is_a_usage_error(self):
        rc, out, _ = _run_main(["delete", "1", "-s"])
        self.assertEqual(rc, 2)
        self.assertIn("requires a space name", out)

    def test_cross_space_delete_leaves_project_state_untouched(self):
        manager = self.make_space("work")
        manager.focus(2)
        manager.set_note(2, "space note")
        self.ck.start(2)
        self.ck.set_note("project note")
        before_state = self.ck.state_file.read_text(encoding="utf-8")
        before_plan = self.plan_text()

        rc, _, err = _run_main(
            ["delete", "2", "-s", "work", "-f"])

        self.assertEqual(rc, 0, err)
        self.assertEqual(
            self.ck.state_file.read_text(encoding="utf-8"), before_state)
        self.assertEqual(self.plan_text(), before_plan)


class TestDeleteCli(_Harness):

    def test_force_deletes_without_prompt(self):
        rc, out, err = _run_main(["delete", "2", "--force"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Task #2: beta", out)   # target shown
        self.assertIn("IDs re-indexed", out)
        self.assertEqual(self.titles(), ["alpha", "gamma"])
        self.assertEqual(self.ids(), [1, 2])

    def test_short_force_flag_before_id(self):
        rc, out, err = _run_main(["delete", "-f", "1"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.titles(), ["beta", "gamma"])

    def test_rm_alias_deletes_like_delete(self):
        rc, out, err = _run_main(["rm", "3", "-f"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Task #3: gamma", out)
        self.assertEqual(self.titles(), ["alpha", "beta"])

    def test_interactive_confirmation_yes_deletes(self):
        rc, out, err = _run_main(["delete", "2"], tty=True, answer="y")
        self.assertEqual(rc, 0, err)
        self.assertIn("Task #2: beta", out)
        self.assertEqual(self.titles(), ["alpha", "gamma"])

    def test_interactive_prompt_names_the_default_project_space(self):
        prompts: list = []
        rc, _, err = _run_main(
            ["delete", "2"], tty=True, answer="y", prompt_log=prompts)
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            prompts,
            ["Permanently delete task #2 from space 'project'? [y/N]: "])

    def test_interactive_confirmation_no_aborts(self):
        before = self.plan_text()
        rc, out, err = _run_main(["delete", "2"], tty=True, answer="n")
        self.assertEqual(rc, 2)
        self.assertIn("Aborted.", out)
        self.assertIn("Task #2: beta", out)
        self.assertEqual(self.plan_text(), before)

    def test_interactive_bare_enter_aborts(self):
        before = self.plan_text()
        rc, out, _ = _run_main(["delete", "2"], tty=True, answer="")
        self.assertEqual(rc, 2)
        self.assertIn("Aborted.", out)
        self.assertEqual(self.plan_text(), before)

    def test_non_interactive_without_force_refuses(self):
        before = self.plan_text()
        rc, out, err = _run_main(["delete", "2"])
        self.assertEqual(rc, 2)
        self.assertIn("Refusing to delete task #2", out)
        self.assertIn("Pass -f to confirm", out)
        self.assertEqual(self.plan_text(), before)
        self.assertNotIn("Traceback", err)

    def test_unknown_id_is_clean_error(self):
        before = self.plan_text()
        rc, out, err = _run_main(["delete", "9", "-f"])
        self.assertEqual(rc, 2)
        self.assertIn("No task with id 9", out)
        self.assertEqual(self.plan_text(), before)
        self.assertNotIn("Traceback", err)

    def test_non_numeric_id_points_at_space_deletion(self):
        before = self.plan_text()
        rc, out, err = _run_main(["delete", "work", "-f"])
        self.assertEqual(rc, 2)
        self.assertIn("Invalid task ID", out)
        self.assertIn("ck space delete work", out)
        self.assertEqual(self.plan_text(), before)

    def test_missing_id_prints_usage(self):
        rc, out, _ = _run_main(["rm"])
        self.assertEqual(rc, 2)
        self.assertIn("Usage: ck rm <ID> [-f|--force]", out)

    def test_extra_ids_are_rejected(self):
        rc, out, _ = _run_main(["delete", "1", "2", "-f"])
        self.assertEqual(rc, 2)
        self.assertIn("takes exactly one task ID", out)

    def test_unknown_flag_is_rejected(self):
        rc, out, _ = _run_main(["delete", "1", "--yes"])
        self.assertEqual(rc, 2)
        self.assertIn("Unknown flag", out)

    def test_delete_does_not_touch_global_spaces(self):
        """Architectural distinction: task deletion is PLAN-only."""
        spaces_dir = ckconfig.GLOBAL_CONFIG_DIR / "spaces"
        spaces_dir.mkdir(parents=True, exist_ok=True)
        space = spaces_dir / "work.md"
        space.write_text("## Current Sprint\n- [ ] keep me\n", encoding="utf-8")
        rc, _, err = _run_main(["delete", "1", "-f"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(space.exists())
        self.assertIn("keep me", space.read_text(encoding="utf-8"))

    def test_no_project_context_is_a_clean_error(self):
        outside = Path(self._tmp.name) / "empty"
        outside.mkdir()
        os.chdir(outside)
        rc, out, err = _run_main(["delete", "1", "-f"])
        self.assertEqual(rc, 2)
        self.assertIn("No active project found", out)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
