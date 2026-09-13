"""Tests for PLAN.md auto-repair, clean insertion, and gap/ID logic.

Covers Step 2 of the refactor:

1. Auto-repair on parse: corrupted ANSI/artifact lines are cleaned
   out of existing PLAN.md files (pure function + persisted repair).
2. Clean insertion: ``ck add`` inserts under ``## Current Sprint``
   (or directly before ``## Completed``).
3. ID & gap logic: positional gap detection, no false gaps for
   newly added tasks, sequential IDs after repair drops noise.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.core import ContextKeeper
from cklib.models import TaskStatus
from cklib.parser import (
    _find_completed_line,
    _find_section_end,
    load_repaired_plan,
    parse_plan,
    parse_plan_file,
    repair_plan_text,
)


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


# ---------------------------------------------------------------------------
# 1. Auto-repair (pure function)
# ---------------------------------------------------------------------------


class TestRepairPlanText(unittest.TestCase):
    """``repair_plan_text`` filters corrupted lines, keeps clean ones."""

    def test_clean_document_unchanged(self):
        text = "# P\n\n## Current Sprint\n- [ ] a\n- [x] b\n\n## Completed\n"
        self.assertEqual(repair_plan_text(text), text)

    def test_drops_pasted_terminal_noise_lines(self):
        text = (
            "# P\n"
            "\x1b[32m✔ 42 passed\x1b[0m\n"
            "\x1b[2K\rBuilding... 87%\n"
            "## Current Sprint\n"
            "- [ ] real task\n"
            "\x1b[1;31mERROR\x1b[0m something broke\n"
        )
        out = repair_plan_text(text)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("passed", out)
        self.assertNotIn("ERROR", out)
        self.assertIn("# P", out)
        self.assertIn("## Current Sprint", out)
        self.assertIn("- [ ] real task", out)

    def test_recovers_task_wrapped_in_ansi(self):
        text = "## Current Sprint\n- [ ] \x1b[33mDeploy app\x1b[0m\n"
        out = repair_plan_text(text)
        self.assertIn("- [ ] Deploy app", out)
        self.assertNotIn("\x1b", out)

    def test_repairs_ansi_in_done_and_focused_tasks(self):
        text = (
            "- [x] \x1b[31mfix bug\x1b[0m\n"
            "- [>] \x1b[32mship it\x1b[0m\n"
        )
        out = repair_plan_text(text)
        self.assertIn("- [x] fix bug", out)
        self.assertIn("- [>] ship it", out)

    def test_recovers_legacy_open_task_with_noise(self):
        text = "- [] \x1b[1mlegacy noisy\x1b[0m\n"
        out = repair_plan_text(text)
        self.assertIn("- [] legacy noisy", out)

    def test_recovers_header_with_noise(self):
        text = "## \x1b[36mCurrent Sprint\x1b[0m\n- [ ] a\n"
        out = repair_plan_text(text)
        self.assertIn("## Current Sprint", out)
        self.assertIn("- [ ] a", out)

    def test_drops_task_that_is_pure_noise(self):
        text = "- [ ] \x1b[2K\x1b[0m\x07\n- [ ] keep me\n"
        out = repair_plan_text(text)
        self.assertIn("- [ ] keep me", out)
        # The pure-noise task disappears entirely
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("- [")]), 1)

    def test_drops_control_character_artifacts(self):
        text = "# P\n\x00\x01binary junk\x02\n- [ ] ok\n"
        out = repair_plan_text(text)
        self.assertIn("# P", out)
        self.assertIn("- [ ] ok", out)
        self.assertNotIn("binary junk", out)

    def test_drops_midline_cr_progress_artifact(self):
        text = "## Current Sprint\n- [ ] task\n50%\r100%\r done\n"
        out = repair_plan_text(text)
        self.assertNotIn("100%", out)
        self.assertIn("- [ ] task", out)

    def test_idempotent(self):
        dirty = "# P\n\x1b[31mnoise\x1b[0m\n- [ ] a\n"
        once = repair_plan_text(dirty)
        self.assertEqual(repair_plan_text(once), once)

    def test_preserves_crlf_line_endings(self):
        text = "# P\r\n- [ ] a\r\n\x1b[0m junk\r\n"
        out = repair_plan_text(text)
        self.assertIn("\r\n", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("# P", out)
        self.assertIn("- [ ] a", out)

    def test_empty_text_passthrough(self):
        self.assertEqual(repair_plan_text(""), "")


# ---------------------------------------------------------------------------
# 2. Auto-repair on parse (persisted self-healing)
# ---------------------------------------------------------------------------


class TestLoadRepairedPlan(_IsolatedHome, unittest.TestCase):
    """``load_repaired_plan`` heals the file on disk when corrupt."""

    def _write_plan(self, tmp: Path, text: str) -> ContextKeeper:
        root = tmp / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(text, encoding="utf-8")
        return ck

    def test_repair_persists_and_flags(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._write_plan(
                Path(td),
                "# P\n\x1b[32mnoise\x1b[0m\n## Current Sprint\n- [ ] a\n",
            )
            tl, repaired = load_repaired_plan(ck.plan_file)
            self.assertTrue(repaired)
            self.assertEqual(tl.total, 1)
            self.assertEqual(tl.tasks[0].title, "a")
            self.assertNotIn("\x1b", ck.plan_file.read_text(encoding="utf-8"))

    def test_clean_file_not_rewritten(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._write_plan(
                Path(td), "# P\n## Current Sprint\n- [ ] a\n"
            )
            before = ck.plan_file.read_text(encoding="utf-8")
            tl, repaired = load_repaired_plan(ck.plan_file)
            self.assertFalse(repaired)
            self.assertEqual(ck.plan_file.read_text(encoding="utf-8"), before)

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "empty"
            root.mkdir()
            ck = ContextKeeper(root=root)
            tl, repaired = load_repaired_plan(ck.plan_file)
            self.assertEqual(tl.total, 0)
            self.assertFalse(repaired)

    def test_add_task_heals_corrupted_plan(self):
        """A corrupted PLAN.md is auto-repaired by the next `ck add`."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._write_plan(
                Path(td),
                "# P\n"
                "## Current Sprint\n"
                "- [ ] real one\n"
                "\x1b[2K\x1b[31mFAIL\x1b[0m src/main.py:42\n"
                "## Completed\n",
            )
            new_id = ck.add_task("brand new")
            text = ck.plan_file.read_text(encoding="utf-8")

            self.assertEqual(new_id, 2)  # noise line was not a task
            self.assertNotIn("\x1b", text)
            self.assertNotIn("FAIL", text)
            self.assertIn("- [ ] real one", text)
            self.assertIn("- [ ] brand new", text)

            # Re-parse: IDs are sequential, no gaps from dropped noise
            tl = parse_plan_file(ck.plan_file)
            self.assertEqual([t.id for t in tl.tasks], [1, 2])

    def test_done_and_start_also_heal(self):
        """start/done run through the repairing load too."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._write_plan(
                Path(td),
                "# P\n## Current Sprint\n- [ ] a\n- [ ] b\n"
                "\x1b[33mgarbage\x1b[0m\n",
            )
            ck.start(2)
            ck.done("1")
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertNotIn("\x1b", text)
            self.assertIn("- [>] b", text)
            self.assertIn("- [x] a", text)


# ---------------------------------------------------------------------------
# 3. Clean insertion position
# ---------------------------------------------------------------------------


class TestInsertionPosition(_IsolatedHome, unittest.TestCase):
    """``ck add`` lands under ## Current Sprint, else before Completed."""

    def _ck_with_plan(self, tmp: Path, text: str) -> ContextKeeper:
        root = tmp / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(text, encoding="utf-8")
        return ck

    def test_add_inserts_under_current_sprint(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck_with_plan(Path(td), (
                "# P\n"
                "\n"
                "## Current Sprint\n"
                "- [ ] first\n"
                "- [ ] second\n"
                "\n"
                "## Completed\n"
                "- [x] old\n"
            ))
            ck.add_task("third")
            text = ck.plan_file.read_text(encoding="utf-8")
            lines = text.splitlines()

            self.assertIn("- [ ] third", lines)
            idx_third = lines.index("- [ ] third")
            idx_completed = lines.index("## Completed")
            # third sits between second and the Completed header
            self.assertLess(lines.index("- [ ] second"), idx_third)
            self.assertLess(idx_third, idx_completed)

    def test_add_inserts_into_empty_sprint_section(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck_with_plan(Path(td), (
                "# P\n## Current Sprint\n## Completed\n- [x] old\n"
            ))
            ck.add_task("fresh")
            lines = ck.plan_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines.index("- [ ] fresh"),
                             lines.index("## Current Sprint") + 1)

    def test_add_inserts_directly_before_completed_when_no_sprint(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck_with_plan(Path(td), (
                "# P\n## Backlog\n- [ ] queued\n\n## Completed\n- [x] old\n"
            ))
            ck.add_task("urgent")
            lines = ck.plan_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines.index("- [ ] urgent") + 1,
                             lines.index("## Completed"))

    def test_add_appends_at_eof_when_no_headers(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck_with_plan(Path(td), "- [ ] only task\n")
            ck.add_task("another")
            lines = ck.plan_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["- [ ] only task", "- [ ] another"])

    def test_add_creates_plan_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fresh"
            root.mkdir()
            ck = ContextKeeper(root=root)
            new_id = ck.add_task("first ever")
            self.assertEqual(new_id, 1)
            self.assertIn("- [ ] first ever",
                          ck.plan_file.read_text(encoding="utf-8"))

    def test_sprint_insert_survives_reparse_section(self):
        """The inserted task re-parses as a Current Sprint member."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck_with_plan(Path(td), (
                "# P\n## Current Sprint\n- [ ] a\n\n## Completed\n"
            ))
            ck.add_task("in sprint")
            tl = parse_plan_file(ck.plan_file)
            by_title = {t.title: t for t in tl.tasks}
            self.assertEqual(by_title["in sprint"].section, "Current Sprint")


# ---------------------------------------------------------------------------
# 4. Gap & ID logic
# ---------------------------------------------------------------------------


class TestGapAndIdLogic(unittest.TestCase):
    """Positional gap detection + sequential IDs."""

    def test_gap_detection_positional(self):
        tl = parse_plan("- [x] done\n- [ ] gap1\n- [ ] gap2\n")
        self.assertEqual(tl.gap_ids(), [2, 3])

    def test_no_gaps_when_open_before_done(self):
        tl = parse_plan("- [ ] a\n- [ ] b\n- [x] c\n")
        self.assertEqual(tl.gap_ids(), [])

    def test_no_false_gap_for_new_pasteof_task(self):
        """A task added after a done one must not be flagged a gap
        merely because its provisional line number points past EOF."""
        tl = parse_plan("- [ ] a\n- [x] b\n")
        tl.add("new after done")  # provisional line_number > source len
        # New open task after done → it IS a genuine gap by design
        self.assertEqual(tl.gap_ids(), [3])

    def test_gap_after_repair_renumbering(self):
        """Repair dropping lines must not distort gap detection."""
        dirty = "# P\n\x1b[0m noise\n- [ ] a\n- [x] b\n- [ ] c\n"
        cleaned = repair_plan_text(dirty)
        tl = parse_plan(cleaned)
        # Sequential IDs despite the dropped noise line
        self.assertEqual([t.id for t in tl.tasks], [1, 2, 3])
        self.assertEqual(tl.gap_ids(), [3])

    def test_add_after_repair_keeps_id_sequence(self):
        """IDs continue 1..N+1 after repair heals the file."""
        dirty = (
            "# P\n"
            "## Current Sprint\n"
            "- [ ] a\n"
            "\x1b[31mE\x1b[0m pasted output\n"
            "- [ ] b\n"
        )
        cleaned = repair_plan_text(dirty)
        tl = parse_plan(cleaned)
        new = tl.add("c")
        self.assertEqual(new.id, 3)
        self.assertEqual([t.id for t in tl.tasks], [1, 2, 3])

    def test_ids_sequential_when_tasks_between_done(self):
        tl = parse_plan(
            "- [ ] a\n- [x] b\n- [ ] c\n- [x] d\n- [ ] e\n"
        )
        self.assertEqual([t.id for t in tl.tasks], [1, 2, 3, 4, 5])
        # Only e trails the LAST done task (d); c sits between done
        # tasks and is not a gap under the positional rule.
        self.assertEqual(tl.gap_ids(), [5])


# ---------------------------------------------------------------------------
# Section-end helper
# ---------------------------------------------------------------------------


class TestFindSectionEnd(unittest.TestCase):
    def test_end_after_last_content_line(self):
        lines = ["# P", "", "## Current Sprint", "- [ ] a", "- [ ] b", "",
                 "## Completed"]
        self.assertEqual(_find_section_end(lines, "Current Sprint"), 5)

    def test_empty_section(self):
        lines = ["## Current Sprint", "## Completed"]
        self.assertEqual(_find_section_end(lines, "Current Sprint"), 1)

    def test_section_at_eof(self):
        lines = ["# P", "## Current Sprint", "- [ ] a"]
        self.assertEqual(_find_section_end(lines, "Current Sprint"), 3)

    def test_missing_section_returns_none(self):
        self.assertIsNone(_find_section_end(["# P", "- [ ] a"], "Current Sprint"))

    def test_completed_line_found(self):
        lines = ["# P", "## Current Sprint", "- [ ] a", "## Completed"]
        self.assertEqual(_find_completed_line(lines), 3)


if __name__ == "__main__":
    unittest.main()
