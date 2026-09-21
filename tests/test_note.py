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
        # The note sits inside the status block, above the closing bar.
        lines = out.splitlines()
        note_lines = [i for i, l in enumerate(lines) if "* Note:" in l]
        self.assertEqual(len(note_lines), 1)
        self.assertLess(note_lines[0], len(lines) - 1)

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
            f"{ckui.BOLD}{ckui.CYAN}    * Note: checking API mapping"
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
