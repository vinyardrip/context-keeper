"""Tests for the active-task process note (`ck note`).

Covers:

- ``ck note <text>`` persists the note in ``.ck/state.json`` under
  ``active_task`` (id, title, note, updated_at) for the CURRENT
  active task (focused, else first open).
- ``ck st`` displays the note as a prominent ``* Note: <text>``
  line — in both plain and colored modes.
- ``ck done`` on the noted task purges the note completely; done on
  a DIFFERENT task keeps it.
- Error paths: empty note text, no active task.
- CLI dispatch through ``main()`` (note → st → done lifecycle) and
  the NO_COLOR plain-text guarantee.
- DEV MODE: the note write is redirected into the sandbox; the real
  ``.ck/state.json`` is never created.
"""

from __future__ import annotations

# Filesystem isolation safety net: importing the tests package
# pins CK_SANDBOX_ROOT to an OS-temp directory (see tests/__init__),
# so no test in this module can create or wipe the repository's own
# .sandbox/ — under ANY runner, including bare `unittest discover`.
import tests  # noqa: F401

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import ui as ckui
from cklib.cli import main
from cklib.core import ContextKeeper
from cklib.sandbox import resolve_write_path


class _IsolatedHome:
    """Pin the global registry to a per-test tmp HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
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

    def _cleanup(self):
        self._tmp.cleanup()
        ckconfig.GLOBAL_CONFIG_DIR = self._orig_dir
        ckconfig.GLOBAL_REGISTRY_FILE = self._orig_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
        ckregistry.GLOBAL_CONFIG_DIR = self._orig_dir
        ckregistry.GLOBAL_REGISTRY_FILE = self._orig_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy


def _clean_env(**overrides):
    """Env patcher: CK color/sandbox toggles removed by default."""
    base = {
        k: v for k, v in os.environ.items()
        if k not in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE",
                     "CK_SANDBOX", "CK_DEV")
    }
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=True)


class _NoteHarness(_IsolatedHome, unittest.TestCase):
    """Shared project scaffold: a plan with a focused task."""

    PLAN = (
        "# P\n"
        "## Current Sprint\n"
        "- [x] setup repo\n"
        "- [>] implement API mapping\n"
        "- [ ] write docs\n"
    )

    def _ck(self, plan: str = PLAN) -> ContextKeeper:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name) / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(plan, encoding="utf-8")
        return ck

    def _read_state_file(self, ck: ContextKeeper) -> dict:
        self.assertTrue(ck.state_file.exists(), "state.json must exist")
        return json.loads(ck.state_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Persistence: ck note -> state.json
# ---------------------------------------------------------------------------


class TestNotePersistence(_NoteHarness):
    def test_note_stored_under_active_task(self):
        ck = self._ck()
        info = ck.set_note("checking API mapping")

        state = self._read_state_file(ck)
        active = state["active_task"]
        # The note attaches to the FOCUSED task (active task).
        self.assertEqual(active["id"], 2)
        self.assertEqual(active["title"], "implement API mapping")
        self.assertEqual(active["note"], "checking API mapping")
        self.assertTrue(active["updated_at"])
        self.assertEqual(info, active)

    def test_note_attaches_to_first_open_when_no_focus(self):
        ck = self._ck("# P\n- [ ] alpha\n- [ ] beta\n")
        ck.set_note("scratch")
        active = self._read_state_file(ck)["active_task"]
        self.assertEqual(active["id"], 1)
        self.assertEqual(active["title"], "alpha")

    def test_note_update_overwrites(self):
        ck = self._ck()
        ck.set_note("first draft")
        ck.set_note("stopped at line 42 before lunch")
        active = self._read_state_file(ck)["active_task"]
        self.assertEqual(active["note"], "stopped at line 42 before lunch")

    def test_note_text_is_sanitized(self):
        """ANSI noise pasted into ck note can never corrupt state."""
        ck = self._ck()
        ck.set_note("\x1b[31mred\x1b[0m   spaced\t\tout\nnewline")
        active = self._read_state_file(ck)["active_task"]
        self.assertEqual(active["note"], "red spaced out newline")

    def test_empty_note_rejected(self):
        ck = self._ck()
        with self.assertRaises(ValueError):
            ck.set_note("   \x1b[31m\x1b[0m  ")

    def test_note_without_active_task_rejected(self):
        ck = self._ck("# P\n")  # no tasks at all
        with self.assertRaises(ValueError):
            ck.set_note("orphan note")
        # And nothing was written.
        self.assertFalse(ck.state_file.exists())

    def test_get_note_roundtrip(self):
        ck = self._ck()
        self.assertIsNone(ck.get_note())
        ck.set_note("hello")
        self.assertEqual(ck.get_note()["note"], "hello")


# ---------------------------------------------------------------------------
# Display: ck st renders the note
# ---------------------------------------------------------------------------


class TestNoteDisplay(_NoteHarness):
    def test_status_shows_note_line(self):
        ck = self._ck()
        ck.set_note("checking API mapping")
        out = ck.status()
        self.assertIn("* Note: checking API mapping", out)
        # The note appears twice by design: once in the top-level
        # CURRENT FOCUS block and once under the task in WORK CONTEXT.
        lines = out.splitlines()
        note_lines = [i for i, l in enumerate(lines) if "* Note:" in l]
        self.assertEqual(len(note_lines), 2)
        self.assertLess(note_lines[0], note_lines[1])
        self.assertLess(note_lines[1], len(lines) - 1)

    def test_status_without_note_has_no_note_line(self):
        ck = self._ck()
        self.assertNotIn("* Note:", ck.status())

    def test_note_survives_focus_change_display(self):
        """The note stays visible while its task still exists, even
        if focus moved elsewhere (it is a task scratchpad)."""
        ck = self._ck()
        ck.set_note("paused mid-refactor")
        ck.start(3)  # focus a different task
        self.assertIn("* Note: paused mid-refactor", ck.status())

    def test_stale_note_for_removed_task_not_shown(self):
        ck = self._ck()
        ck.set_note("note on task 2")
        # Externally rewrite the plan so task 2 no longer exists.
        ck.plan_file.write_text("# P\n- [ ] only task\n", encoding="utf-8")
        self.assertNotIn("* Note:", ck.status())

    def test_colored_status_shows_note_in_bold_cyan(self):
        ck = self._ck()
        ck.set_note("checking API mapping")
        with mock.patch.object(
                ckui, "get_palette", return_value=ckui.Palette(True)):
            out = ck.status()
        self.assertIn(
            f"{ckui.BOLD}{ckui.CYAN}       * Note: checking API mapping"
            f"{ckui.RESET}",
            out,
        )
        # Plain equivalence after stripping escapes.
        with mock.patch.object(
                ckui, "get_palette", return_value=ckui.Palette(False)):
            plain = ck.status()
        self.assertEqual(ckui.strip_ansi(out), plain)


# ---------------------------------------------------------------------------
# Lifecycle: ck done purges the note
# ---------------------------------------------------------------------------


class TestNotePurgeOnDone(_NoteHarness):
    def test_done_of_noted_task_purges_note(self):
        ck = self._ck()
        ck.set_note("checking API mapping")
        ck.done("2")  # complete the noted (focused) task

        state = self._read_state_file(ck)
        self.assertNotIn("active_task", state)
        self.assertNotIn("* Note:", ck.status())

    def test_done_of_different_task_keeps_note(self):
        ck = self._ck()
        ck.set_note("note on focused task")
        ck.done("3")  # complete the NEXT task, not the noted one

        state = self._read_state_file(ck)
        self.assertEqual(state["active_task"]["note"], "note on focused task")
        self.assertIn("* Note: note on focused task", ck.status())

    def test_done_range_including_noted_task_purges(self):
        ck = self._ck()
        ck.set_note("bulk cleanup note")
        ck.done("2-3")
        state = self._read_state_file(ck)
        self.assertNotIn("active_task", state)

    def test_done_without_note_writes_no_state(self):
        """A project that never had a note must not gain a state
        file from ck done."""
        ck = self._ck()
        ck.done("3")
        self.assertFalse(ck.state_file.exists())


# ---------------------------------------------------------------------------
# Unfocused / Paused Context: focus-loss handling with/without a note
# ---------------------------------------------------------------------------


class TestUnfocusedPausedContext(_NoteHarness):
    """Focus-loss handling per spec:

    - A noted open task that loses focus (``ck start <NEW_ID>`` or a
      reset) renders in the dedicated "Unfocused / Paused Context"
      block of ``ck st`` / ``ck dashboard -v`` with its note intact.
    - A noteless demotion lets the CLI emit the soft attach-a-note
      hint.
    - ``ck done`` archives the note into HISTORY.md and clears it.
    """

    def _run_cli(self, argv, cwd: Path) -> str:
        buf = io.StringIO()
        orig_cwd = Path.cwd()
        os.chdir(cwd)
        try:
            with _clean_env(), redirect_stdout(buf):
                code = main(argv)
            self.assertEqual(code, 0, f"exit {code} for {argv!r}")
        finally:
            os.chdir(orig_cwd)
        return buf.getvalue()

    def test_switch_moves_noted_task_to_paused_block(self):
        ck = self._ck()
        ck.set_note("paused mid-refactor")
        ck.start(3)
        out = ck.status()
        self.assertIn("Unfocused / Paused Context:", out)
        self.assertIn("- [2] implement API mapping", out)
        # The note travels with the paused task and appears exactly
        # once — NOT duplicated under the new Focus.
        self.assertEqual(out.count("* Note: paused mid-refactor"), 1)
        lines = out.splitlines()
        paused_idx = next(
            i for i, l in enumerate(lines)
            if "Unfocused / Paused Context:" in l)
        note_idx = next(
            i for i, l in enumerate(lines)
            if "* Note: paused mid-refactor" in l)
        self.assertGreater(note_idx, paused_idx)
        self.assertIn("- [2] implement API mapping", lines[paused_idx + 1])

    def test_switch_without_note_prints_soft_hint(self):
        ck = self._ck()
        out = self._run_cli(["start", "3"], ck.root)
        self.assertIn("-> Focused [3]: write docs", out)
        self.assertIn(
            "[!] Task #2 lost focus without a note. "
            "Attach one via `ck note <text>`.", out)
        # A noteless demotion does NOT claim a paused block.
        self.assertNotIn("Unfocused / Paused Context:", out)

    def test_switch_with_note_reports_paused_block_via_cli(self):
        ck = self._ck()
        ck.set_note("half done")
        out = self._run_cli(["start", "3"], ck.root)
        self.assertIn(
            "[i] Task [2] implement API mapping lost focus — moved to "
            "Unfocused / Paused Context (note preserved; archived by "
            "`ck done`).", out)
        self.assertNotIn("lost focus without a note", out)

    def test_done_archives_note_into_history_and_clears_state(self):
        ck = self._ck()
        ck.set_note("finish the mapping")
        ck.done("2")
        self.assertIsNone(ck.get_note())
        self.assertNotIn("active_task", self._read_state_file(ck))
        history = ck.history_file.read_text(encoding="utf-8")
        self.assertIn("[2] implement API mapping", history)
        self.assertIn("- finish the mapping", history)

    def test_done_of_other_task_keeps_note_and_history_untouched(self):
        ck = self._ck()
        ck.set_note("still working")
        before = (
            ck.history_file.read_text(encoding="utf-8")
            if ck.history_file.exists() else ""
        )
        ck.done("3")
        self.assertEqual(ck.get_note()["note"], "still working")
        after = (
            ck.history_file.read_text(encoding="utf-8")
            if ck.history_file.exists() else ""
        )
        self.assertEqual(before, after)

    def test_noteless_done_writes_no_history(self):
        ck = self._ck()
        ck.done("2")
        self.assertFalse(ck.state_file.exists())
        self.assertFalse(ck.history_file.exists())

    def test_focus_reset_keeps_note_visible(self):
        """After ``ck start 0`` the demoted task is the auto-resolved
        Next candidate, so its note renders inline under Next — the
        dedicated paused block is reserved for tasks superseded by
        another explicit focus (nothing is buried either way)."""
        ck = self._ck()
        ck.set_note("wip")
        result = ck.start(0)
        self.assertEqual(result.task_id, 0)
        self.assertEqual(result.demoted_id, 2)
        self.assertTrue(result.had_note)
        self.assertEqual(ck._load_plan().focused, [])
        out = ck.status()
        self.assertIn("[>] Next:", out)
        self.assertIn("* Note: wip", out)
        self.assertNotIn("Unfocused / Paused Context:", out)

    def test_reset_without_focus_is_noop(self):
        # An UNFOCUSED plan: nothing to demote, nothing reported.
        ck = self._ck("# P\n- [ ] alpha\n- [ ] beta\n")
        result = ck.start(0)
        self.assertIsNone(result.demoted_id)
        self.assertFalse(result.had_note)

    def test_reset_of_focused_task_reports_demotion(self):
        ck = self._ck()
        result = ck.start(0)
        self.assertEqual(result.demoted_id, 2)
        self.assertFalse(result.had_note)

    def test_start_of_focused_task_does_not_demote(self):
        ck = self._ck()
        ck.set_note("wip")
        result = ck.start(2)  # re-focus the already-focused task
        self.assertIsNone(result.demoted_id)
        self.assertEqual(ck.get_note()["note"], "wip")

    def test_dashboard_verbose_shows_paused_block_for_registered_project(
            self):
        from cklib import registry as ckregistry
        from cklib.core import _render_dashboard
        from cklib.parser import parse_plan_file

        ck = self._ck()
        ck.set_note("paused mid-refactor")
        ck.start(3)
        ckregistry.register_project(ck.root, name="project")
        out = _render_dashboard(
            ck,
            list_projects=ckregistry.list_projects,
            parse_plan_file=parse_plan_file,
            verbose=True,
        )
        self.assertIn("Unfocused / Paused Context:", out)
        self.assertIn("- [2] implement API mapping", out)
        self.assertIn("* Note: paused mid-refactor", out)

    def test_dashboard_verbose_shows_inline_note_under_focus(self):
        from cklib import registry as ckregistry
        from cklib.core import _render_dashboard
        from cklib.parser import parse_plan_file

        ck = self._ck()
        ck.set_note("checking API mapping")  # note stays on the focus
        ckregistry.register_project(ck.root, name="project")
        out = _render_dashboard(
            ck,
            list_projects=ckregistry.list_projects,
            parse_plan_file=parse_plan_file,
            verbose=True,
        )
        self.assertIn("* Note: checking API mapping", out)
        self.assertNotIn("Unfocused / Paused Context:", out)


# ---------------------------------------------------------------------------
# Interactive note prompt on focus switch (TTY / non-TTY / flags)
# ---------------------------------------------------------------------------


class TestInteractiveNotePrompt(_NoteHarness):
    """``ck start <NEW_ID>`` offers to capture context before demoting
    a noteless focused task — but only on an interactive TTY."""

    def _run(self, argv, cwd: Path, *, stdin_tty=True, stdout_tty=True,
             inputs=None):
        buf = io.StringIO()
        prompts: list[str] = []
        scripted = list(inputs or [])

        def fake_input(prompt=""):
            prompts.append(prompt)
            if scripted:
                item = scripted.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            raise EOFError("stdin closed")

        orig_cwd = Path.cwd()
        os.chdir(cwd)
        try:
            with _clean_env(), redirect_stdout(buf):
                with mock.patch("sys.stdin.isatty", return_value=stdin_tty), \
                        mock.patch("sys.stdout.isatty",
                                   return_value=stdout_tty), \
                        mock.patch("builtins.input", side_effect=fake_input):
                    code = main(argv)
            self.assertEqual(code, 0, f"exit {code} for {argv!r}")
        finally:
            os.chdir(orig_cwd)
        return buf.getvalue(), prompts

    def test_tty_yes_saves_note_then_switches(self):
        ck = self._ck()
        out, prompts = self._run(
            ["start", "3"], ck.root,
            inputs=["y", "paused mid-refactor"])
        self.assertIn(
            "Task #2 lost focus. Add a process note? [y/N]: ", prompts)
        self.assertIn("Note text: ", prompts)
        self.assertIn("* Note saved for [2]: paused mid-refactor", out)
        self.assertIn("-> Focused [3]: write docs", out)
        self.assertNotIn("lost focus without a note", out)
        # The note moved into the paused registry with the task.
        paused = ck._paused_tasks()
        self.assertEqual(paused[0]["id"], 2)
        self.assertEqual(paused[0]["note"], "paused mid-refactor")

    def test_tty_no_falls_back_to_soft_hint(self):
        ck = self._ck()
        out, prompts = self._run(["start", "3"], ck.root, inputs=["n"])
        self.assertIn(
            "Task #2 lost focus. Add a process note? [y/N]: ", prompts)
        self.assertIn("-> Focused [3]: write docs", out)
        self.assertIn(
            "[!] Task #2 lost focus without a note. "
            "Attach one via `ck note <text>`.", out)
        self.assertEqual(prompts, [prompts[0]])  # no Note text prompt
        # Still recorded in the ledger (noteless pause).
        self.assertEqual(ck._paused_tasks()[0]["id"], 2)

    def test_bare_enter_declines(self):
        ck = self._ck()
        out, _ = self._run(["start", "3"], ck.root, inputs=[""])
        self.assertIn("-> Focused [3]: write docs", out)
        self.assertIn("lost focus without a note", out)

    def test_eof_declines_without_crash(self):
        ck = self._ck()
        out, _ = self._run(["start", "3"], ck.root)
        self.assertIn("-> Focused [3]: write docs", out)
        self.assertIn("lost focus without a note", out)

    def test_non_tty_never_prompts(self):
        ck = self._ck()
        for stdin_tty, stdout_tty in ((False, True), (True, False),
                                      (False, False)):
            # Reset focus so each round starts from the same state.
            self._run(["start", "2"], ck.root,
                      stdin_tty=stdin_tty, stdout_tty=stdout_tty)
            out, prompts = self._run(["start", "3"], ck.root,
                                     stdin_tty=stdin_tty,
                                     stdout_tty=stdout_tty)
            self.assertEqual(prompts, [])
            self.assertIn("lost focus without a note", out)

    def test_no_input_flag_skips_prompt_even_on_tty(self):
        ck = self._ck()
        out, prompts = self._run(["start", "--no-input", "3"], ck.root)
        self.assertEqual(prompts, [])
        self.assertIn("-> Focused [3]: write docs", out)

    def test_y_flag_asks_text_only(self):
        ck = self._ck()
        out, prompts = self._run(["start", "-y", "3"], ck.root,
                                 inputs=["from -y flag"])
        self.assertNotIn("Add a process note?", "\n".join(prompts))
        self.assertIn("Note text: ", prompts)
        self.assertIn("* Note saved for [2]: from -y flag", out)

    def test_prompt_not_shown_when_task_has_note(self):
        ck = self._ck()
        ck.set_note("already noted")
        out, prompts = self._run(["start", "3"], ck.root, inputs=["y", "x"])
        self.assertEqual(prompts, [])
        self.assertIn("-> Focused [3]: write docs", out)

    def test_prompt_not_shown_on_reset_without_prior_focus(self):
        ck = self._ck("# P\n- [ ] alpha\n- [ ] beta\n")
        out, prompts = self._run(["start", "0"], ck.root)
        self.assertEqual(prompts, [])

    def test_blank_note_text_aborts_capture(self):
        ck = self._ck()
        out, prompts = self._run(["start", "3"], ck.root,
                                 inputs=["y", "  "])
        self.assertIn("-> Focused [3]: write docs", out)
        self.assertNotIn("* Note saved", out)


# ---------------------------------------------------------------------------
# Paused ledger: registry semantics and multi-task rendering
# ---------------------------------------------------------------------------


class TestPausedLedger(_NoteHarness):

    def test_multiple_pauses_all_render_in_paused_block(self):
        ck = self._ck("# P\n- [>] a\n- [ ] b\n- [ ] c\n- [ ] d\n")
        ck.set_note("a note")
        ck.start(2)  # a paused w/ note
        ck.start(3)  # b paused noteless
        ck.start(4)  # c paused noteless
        out = ck.status()
        self.assertEqual(out.count("Unfocused / Paused Context:"), 1)
        self.assertIn("- [1] a", out)
        self.assertIn("* Note: a note", out)
        self.assertIn("- [2] b", out)
        self.assertIn("- [3] c", out)
        # Paused tasks excluded from the generic skipped list.
        self.assertNotIn("[!] Skipped (2 tasks):", out)
        for tid in (1, 2, 3):
            self.assertNotIn(f"       - [{tid}] ",
                             out.split("[!] Skipped")[1].split(
                                 "Unfocused")[0])

    def test_paused_note_survives_intermediate_switches(self):
        ck = self._ck("# P\n- [>] a\n- [ ] b\n- [ ] c\n- [ ] d\n")
        ck.set_note("a note")
        ck.start(2)
        ck.start(3)
        ck.start(4)
        ck.start(5 if False else 4)  # switch among non-noted tasks
        st = ck.status()
        self.assertIn("* Note: a note", st)
        self.assertIn("- [1] a", st)

    def test_refocus_restores_note_and_removes_ledger_entry(self):
        ck = self._ck("# P\n- [>] a\n- [ ] b\n- [ ] c\n")
        ck.set_note("a note")
        ck.start(2)
        ck.start(3)
        ck.start(1)
        self.assertEqual(ck.get_note()["note"], "a note")
        # Tasks 2 and 3 were both paused along the way; only task 1's
        # entry was consumed by the restore.
        self.assertEqual(
            [e["id"] for e in ck._paused_tasks()], [3, 2])
        st = ck.status()
        # The restored note renders inline under Focus.
        self.assertIn("* Note: a note", st)

    def test_done_purges_completed_tasks_from_ledger(self):
        ck = self._ck("# P\n- [>] a\n- [ ] b\n- [ ] c\n")
        ck.set_note("a note")
        ck.start(2)
        ck.start(3)
        ck.done("1")  # complete the paused, noted task
        self.assertEqual(
            [e["id"] for e in ck._paused_tasks()], [2])
        self.assertIn("a note", ck.history_file.read_text())

    def test_peek_reports_focus_loss(self):
        # No focus set -> nothing would be demoted.
        ck = self._ck("# P\n- [ ] alpha\n- [ ] beta\n")
        self.assertIsNone(ck.pending_focus_loss())
        ck.start(1)
        ck.set_note("wip")
        info = ck.pending_focus_loss()
        self.assertEqual(info["id"], 1)
        self.assertTrue(info["has_note"])
        ck.start(2)
        self.assertFalse(ck.pending_focus_loss()["has_note"])

    def test_corrupt_state_degrades_to_empty_ledger(self):
        ck = self._ck()
        ck.state_file.write_text("{not json", encoding="utf-8")
        self.assertEqual(ck._paused_tasks(), [])


# ---------------------------------------------------------------------------
# ck start idempotency, CURRENT FOCUS line, and the ck notes command
# ---------------------------------------------------------------------------


class TestStartIdempotency(_NoteHarness):
    """Re-focusing the already-focused task is a clean no-op."""

    def _run(self, argv, cwd: Path, *, stdin_tty=True, stdout_tty=True,
             inputs=None):
        buf = io.StringIO()
        prompts: list[str] = []
        scripted = list(inputs or [])

        def fake_input(prompt=""):
            prompts.append(prompt)
            if scripted:
                item = scripted.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            raise EOFError("stdin closed")

        orig_cwd = Path.cwd()
        os.chdir(cwd)
        try:
            with _clean_env(), redirect_stdout(buf):
                with mock.patch("sys.stdin.isatty", return_value=stdin_tty), \
                        mock.patch("sys.stdout.isatty",
                                   return_value=stdout_tty), \
                        mock.patch("builtins.input", side_effect=fake_input):
                    code = main(argv)
            self.assertEqual(code, 0, f"exit {code} for {argv!r}")
        finally:
            os.chdir(orig_cwd)
        return buf.getvalue(), prompts

    def test_already_focused_prints_notice_and_skips_prompt(self):
        ck = self._ck()  # plan has task 2 focused
        out, prompts = self._run(["start", "2"], ck.root)
        self.assertEqual(prompts, [])
        self.assertIn("Task #2 is already focused.", out)
        self.assertNotIn("-> Focused", out)
        self.assertNotIn("lost focus", out)
        self.assertEqual(ck.get_note(), None)

    def test_already_focused_even_when_task_carries_note(self):
        ck = self._ck()
        ck.set_note("wip")
        out, prompts = self._run(["start", "2"], ck.root)
        self.assertEqual(prompts, [])
        self.assertIn("Task #2 is already focused.", out)
        self.assertEqual(ck.get_note()["note"], "wip")

    def test_already_focused_is_a_plan_noop(self):
        ck = self._ck()
        before = ck.plan_file.read_text(encoding="utf-8")
        self._run(["start", "2"], ck.root)
        self.assertEqual(
            ck.plan_file.read_text(encoding="utf-8"), before)

    def test_switching_to_a_different_task_still_works(self):
        ck = self._ck()
        out, _ = self._run(["start", "3"], ck.root)
        self.assertIn("-> Focused [3]: write docs", out)

    def test_idempotent_reset_zero_still_demotes(self):
        """The no-op guard covers only a positive already-focused ID;
        `ck start 0` keeps its reset semantics."""
        ck = self._ck()
        out, _ = self._run(["start", "0"], ck.root)
        self.assertIn("[ok] Focus reset.", out)


class TestCurrentFocusLine(_NoteHarness):
    """Top-level focus visibility in `ck st`."""

    def test_current_focus_line_under_progress(self):
        ck = self._ck()
        out = ck.status()
        lines = out.splitlines()
        focus_idx = next(
            i for i, l in enumerate(lines)
            if "-> CURRENT FOCUS:" in l)
        progress_idx = next(
            i for i, l in enumerate(lines) if "[%] Progress:" in l)
        work_idx = next(
            i for i, l in enumerate(lines) if "-> WORK CONTEXT:" in l)
        self.assertGreater(focus_idx, progress_idx)
        self.assertLess(focus_idx, work_idx)
        self.assertIn("[#2] implement API mapping", lines[focus_idx])

    def test_current_focus_line_includes_note(self):
        ck = self._ck()
        ck.set_note("mid-refactor")
        lines = ck.status().splitlines()
        focus_idx = next(
            i for i, l in enumerate(lines)
            if "-> CURRENT FOCUS:" in l)
        self.assertIn("* Note: mid-refactor", lines[focus_idx + 1])

    def test_no_current_focus_line_without_explicit_focus(self):
        ck = self._ck("# P\n- [ ] alpha\n- [ ] beta\n")
        self.assertNotIn("-> CURRENT FOCUS:", ck.status())

    def test_focus_restored_from_ledger_shows_in_current_focus(self):
        ck = self._ck()
        ck.set_note("wip")
        ck.start(3)
        ck.start(2)  # re-focus: note restored from the paused ledger
        lines = ck.status().splitlines()
        focus_idx = next(
            i for i, l in enumerate(lines)
            if "-> CURRENT FOCUS:" in l)
        self.assertIn("* Note: wip", lines[focus_idx + 1])


class TestNotesCommand(_NoteHarness):
    """`ck notes`: structured listing of all active process notes."""

    def _run(self, argv, cwd: Path) -> str:
        buf = io.StringIO()
        orig_cwd = Path.cwd()
        os.chdir(cwd)
        try:
            with _clean_env(), redirect_stdout(buf):
                code = main(argv)
            self.assertEqual(code, 0, f"exit {code} for {argv!r}")
        finally:
            os.chdir(orig_cwd)
        return buf.getvalue()

    def test_empty_state_message(self):
        ck = self._ck("# P\n- [ ] alpha\n- [ ] beta\n")
        out = self._run(["notes"], ck.root)
        self.assertEqual(out.strip(), "[i] No active process notes found.")

    def test_focused_but_noteless_also_reports_empty(self):
        ck = self._ck()
        out = self._run(["notes"], ck.root)
        self.assertEqual(out.strip(), "[i] No active process notes found.")

    def test_active_focus_section_with_note(self):
        ck = self._ck()
        ck.set_note("checking API mapping")
        out = self._run(["notes"], ck.root)
        self.assertIn("[>] Active Focus:", out)
        self.assertIn("- [2] implement API mapping", out)
        self.assertIn("* Note: checking API mapping", out)
        self.assertNotIn("Unfocused / Paused Context:", out)

    def test_paused_section_lists_all_paused_tasks(self):
        ck = self._ck("# P\n- [>] a\n- [ ] b\n- [ ] c\n- [ ] d\n")
        ck.set_note("a note")
        ck.start(2)  # a paused w/ note
        ck.start(3)  # b paused noteless
        out = self._run(["notes"], ck.root)
        self.assertIn("[>] Active Focus:", out)
        self.assertIn("- [3] c", out)
        self.assertIn("[!] Unfocused / Paused Context:", out)
        self.assertIn("- [1] a", out)
        self.assertIn("* Note: a note", out)
        self.assertIn("- [2] b", out)
        self.assertIn("(no note)", out)

    def test_note_survives_intermediate_switches_in_notes_view(self):
        ck = self._ck("# P\n- [>] a\n- [ ] b\n- [ ] c\n- [ ] d\n")
        ck.set_note("a note")
        ck.start(2)
        ck.start(3)
        ck.start(4)
        out = self._run(["notes"], ck.root)
        self.assertIn("* Note: a note", out)
        self.assertIn("- [1] a", out)

    def test_help_documents_notes(self):
        from cklib.cli import HELP_TEXT
        self.assertIn("notes", HELP_TEXT)
        self.assertIn("No active process notes found", HELP_TEXT)

    def test_notes_via_argparse_path(self):
        ck = self._ck()
        ck.set_note("via argparse")
        out = self._run(["notes"], ck.root)
        self.assertIn("[>] Active Focus:", out)
        self.assertIn("* Note: via argparse", out)


# ---------------------------------------------------------------------------
# CLI dispatch: ck note / ck st / ck done lifecycle
# ---------------------------------------------------------------------------


class TestNoteCli(_NoteHarness):
    def _run(self, argv, cwd: Path) -> str:
        buf = io.StringIO()
        orig_cwd = Path.cwd()
        os.chdir(cwd)
        try:
            with redirect_stdout(buf):
                code = main(argv)
            self.assertEqual(code, 0, f"exit {code} for {argv!r}")
        finally:
            os.chdir(orig_cwd)
        return buf.getvalue()

    def test_cli_note_then_st_then_done(self):
        ck = self._ck()
        with _clean_env():
            # 1. Attach the note.
            out = self._run(["note", "checking", "API", "mapping"], ck.root)
            self.assertIn("* Note saved for [2]", out)
            self.assertIn("checking API mapping", out)

            # 2. ck st displays it.
            out = self._run(["st"], ck.root)
            self.assertIn("* Note: checking API mapping", out)

            # 3. ck done closes the task and clears the note.
            self._run(["done", "2"], ck.root)
            out = self._run(["st"], ck.root)
            self.assertNotIn("* Note:", out)

        state = self._read_state_file(ck)
        self.assertNotIn("active_task", state)

    def test_cli_note_no_args_is_usage_error(self):
        ck = self._ck()
        with _clean_env():
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["note"])
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck note", buf.getvalue())

    def test_cli_note_on_empty_plan_errors_cleanly(self):
        ck = self._ck("# P\n")
        with _clean_env():
            buf = io.StringIO()
            orig_cwd = Path.cwd()
            # Bind the CLI to the tmp project (plan exists but has no
            # tasks) so the error is the no-active-task one.
            os.chdir(ck.root)
            try:
                with redirect_stdout(buf):
                    code = main(["note", "orphan"])
            finally:
                os.chdir(orig_cwd)
        self.assertEqual(code, 2)
        self.assertIn("No active task", buf.getvalue())

    def test_cli_st_respects_no_color(self):
        """NO_COLOR=1 ck st outputs clean, unformatted plain text."""
        ck = self._ck()
        ck.set_note("checking API mapping")
        with _clean_env(NO_COLOR="1"):
            out = self._run(["st"], ck.root)
        self.assertNotIn("\033[", out)
        self.assertIn("* Note: checking API mapping", out)
        self.assertIn("[>] Focus:", out)
        self.assertIn("- [2] implement API mapping", out)

    def test_cli_st_forces_color_when_requested(self):
        """FORCE_COLOR=1 keeps colors even though stdout is a pipe."""
        ck = self._ck()
        with _clean_env(FORCE_COLOR="1"):
            out = self._run(["st"], ck.root)
        self.assertIn("\033[", out)
        # Content survives stripping.
        self.assertIn("WORK CONTEXT", ckui.strip_ansi(out))

    def test_cli_note_rejects_unknown_flag(self):
        ck = self._ck()
        with _clean_env():
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["note", "--evil"])
        self.assertEqual(code, 2)
        self.assertIn("Unknown flag", buf.getvalue())

    def test_help_documents_note(self):
        from cklib.cli import HELP_TEXT
        self.assertIn("note <text>", HELP_TEXT)


# ---------------------------------------------------------------------------
# DEV MODE: note writes never touch production state
# ---------------------------------------------------------------------------


class TestNoteDevMode(_NoteHarness):
    def test_dev_mode_note_lands_in_sandbox(self):
        ck = self._ck()
        with mock.patch.dict(os.environ, {"CK_SANDBOX": "1"}):
            ck.set_note("sandboxed note")
            # Read-your-writes inside the dev session.
            self.assertEqual(ck.get_note()["note"], "sandboxed note")
            sandbox_state = resolve_write_path(ck.state_file)
        self.assertTrue(sandbox_state.exists())
        data = json.loads(sandbox_state.read_text(encoding="utf-8"))
        self.assertEqual(data["active_task"]["note"], "sandboxed note")
        # Production state.json was never created.
        self.assertFalse(ck.state_file.exists())

    def test_dev_mode_done_purges_sandbox_note(self):
        ck = self._ck()
        with mock.patch.dict(os.environ, {"CK_SANDBOX": "1"}):
            # Resolve the sandbox target while dev mode is active —
            # outside the patch the mapping falls back to the real path.
            sandbox_state = resolve_write_path(ck.state_file)
            ck.set_note("temp note")
            ck.done("2")
            self.assertIsNone(ck.get_note())
        data = json.loads(sandbox_state.read_text(encoding="utf-8"))
        self.assertNotIn("active_task", data)


if __name__ == "__main__":
    unittest.main()
