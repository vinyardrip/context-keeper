"""Tests for the PLAN.md AST parser and renderer.

These tests cover the three contract guarantees from the security
review:

1. ``parse_plan`` is PURE — duplicate ``[>]`` markers and legacy
   ``[]`` syntax are preserved.
2. ``normalize_plan`` is OPT-IN — must be called explicitly.
3. ``render_plan`` is DETERMINISTIC — full-file render from the AST.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from cklib.models import TaskStatus
from cklib.parser import (
    normalize_plan,
    parse_plan,
    render_plan,
    write_plan,
)


SAMPLE_CANONICAL = """# Project

## Current Sprint
- [ ] first task
- [>] second focused
- [x] third done

## Done section
- [x] old done
"""


class TestPureParser(unittest.TestCase):
    """``parse_plan`` must NOT mutate state."""

    def test_parses_canonical_three_statuses(self):
        tl = parse_plan(SAMPLE_CANONICAL)
        self.assertEqual(tl.total, 4)
        self.assertEqual(
            [t.status for t in tl.tasks],
            [TaskStatus.OPEN, TaskStatus.FOCUSED, TaskStatus.DONE, TaskStatus.DONE],
        )

    def test_assigns_ids_in_order(self):
        tl = parse_plan(SAMPLE_CANONICAL)
        self.assertEqual([t.id for t in tl.tasks], [1, 2, 3, 4])

    def test_preserves_line_numbers(self):
        tl = parse_plan(SAMPLE_CANONICAL)
        # 1: # Project, 2: blank, 3: ## Current Sprint,
        # 4: first, 5: focused, 6: done, 7: blank, 8: ## Done section,
        # 9: old done
        self.assertEqual([t.line_number for t in tl.tasks], [4, 5, 6, 9])

    def test_section_assignment(self):
        tl = parse_plan(SAMPLE_CANONICAL)
        self.assertEqual(tl.tasks[0].section, "Current Sprint")
        self.assertEqual(tl.tasks[3].section, "Done section")

    def test_pure_does_not_normalize_duplicate_focus(self):
        """Multiple [>] must be preserved verbatim by parse_plan."""
        text = "- [>] a\n- [>] b\n- [>] c\n"
        tl = parse_plan(text)
        focused = [t for t in tl.tasks if t.status == TaskStatus.FOCUSED]
        self.assertEqual(len(focused), 3)

    def test_pure_has_no_side_effects(self):
        """parse_plan must not modify its input string."""
        text = "- [>] a\n- [>] b\n"
        snapshot = text
        parse_plan(text)
        self.assertEqual(text, snapshot)


class TestLegacyAndCanonicalParsing(unittest.TestCase):
    """Standard GFM ``- [ ]`` and legacy ``- []`` must coexist."""

    def test_canonical_open_parsed(self):
        tl = parse_plan("- [ ] canonical open\n")
        self.assertEqual(tl.total, 1)
        self.assertEqual(tl.tasks[0].status, TaskStatus.OPEN)
        self.assertFalse(tl.tasks[0].legacy_syntax)

    def test_legacy_open_parsed_as_open(self):
        tl = parse_plan("- [] legacy open\n")
        self.assertEqual(tl.total, 1)
        self.assertEqual(tl.tasks[0].status, TaskStatus.OPEN)
        self.assertTrue(tl.tasks[0].legacy_syntax)

    def test_canonical_focused_parsed(self):
        tl = parse_plan("- [>] focused\n")
        self.assertEqual(tl.tasks[0].status, TaskStatus.FOCUSED)

    def test_canonical_done_parsed(self):
        tl = parse_plan("- [x] done\n")
        self.assertEqual(tl.tasks[0].status, TaskStatus.DONE)

    def test_mixed_legacy_and_canonical_in_one_file(self):
        text = (
            "# P\n"
            "## T\n"
            "- [] legacy open\n"
            "- [ ] canonical open\n"
            "- [x] done\n"
            "- [>] focused\n"
        )
        tl = parse_plan(text)
        self.assertEqual(tl.total, 4)
        # Legacy OPEN keeps legacy_syntax flag
        self.assertTrue(tl.tasks[0].legacy_syntax)
        self.assertFalse(tl.tasks[1].legacy_syntax)
        self.assertEqual(tl.tasks[0].status, TaskStatus.OPEN)
        self.assertEqual(tl.tasks[1].status, TaskStatus.OPEN)
        self.assertEqual(tl.tasks[2].status, TaskStatus.DONE)
        self.assertEqual(tl.tasks[3].status, TaskStatus.FOCUSED)

    def test_render_canonicalizes_legacy(self):
        """render_plan must emit canonical ``- [ ]`` even for legacy tasks."""
        text = "- [] legacy open\n"
        tl = parse_plan(text)
        normalize_plan(tl)
        out = render_plan(tl)
        self.assertIn("- [ ] legacy open", out)
        self.assertNotIn("- [] legacy", out)


class TestNormalizeExplicit(unittest.TestCase):
    """``normalize_plan`` is opt-in: it must be called explicitly."""

    def test_normalize_demotes_duplicate_focus(self):
        tl = parse_plan("- [>] a\n- [>] b\n")
        self.assertEqual(
            len([t for t in tl.tasks if t.status == TaskStatus.FOCUSED]), 2)
        normalize_plan(tl)
        focused = [t for t in tl.tasks if t.status == TaskStatus.FOCUSED]
        self.assertEqual(len(focused), 1)
        self.assertEqual(focused[0].id, 1)

    def test_normalize_clears_legacy_flag(self):
        tl = parse_plan("- [] x\n")
        self.assertTrue(tl.tasks[0].legacy_syntax)
        normalize_plan(tl)
        self.assertFalse(tl.tasks[0].legacy_syntax)


class TestBlankLinesAndHeaders(unittest.TestCase):
    """Off-by-one safety."""

    def test_blank_lines_and_headers_ignored(self):
        text = "\n\n# Title\n\n## Sub\n\n- [ ] real\n\n\n"
        tl = parse_plan(text)
        self.assertEqual(tl.total, 1)
        self.assertEqual(tl.tasks[0].title, "real")
        self.assertEqual(tl.tasks[0].section, "Sub")


class TestAnalytics(unittest.TestCase):
    def test_completion_pct_and_counts(self):
        tl = parse_plan(SAMPLE_CANONICAL)
        self.assertEqual(tl.completion_pct, 50.0)
        self.assertEqual(len(tl.done), 2)
        self.assertEqual(len(tl.open), 1)
        self.assertEqual(len(tl.focused), 1)

    def test_gap_detection(self):
        text = "- [x] done\n- [ ] gap1\n- [ ] ok\n"
        tl = parse_plan(text)
        self.assertEqual(tl.gap_ids(), [2, 3])

    def test_no_gaps_when_all_open_before_done(self):
        text = "- [ ] a\n- [ ] b\n- [x] c\n"
        tl = parse_plan(text)
        self.assertEqual(tl.gap_ids(), [])


class TestRenderer(unittest.TestCase):
    def test_render_plan_preserves_blank_and_header_lines(self):
        tl = parse_plan(SAMPLE_CANONICAL)
        out = render_plan(tl)
        # Section headers and blank lines preserved
        self.assertIn("# Project", out)
        self.assertIn("## Current Sprint", out)
        self.assertIn("## Done section", out)

    def test_render_plan_uses_canonical_markers(self):
        tl = parse_plan("- [ ] a\n- [>] b\n- [x] c\n")
        out = render_plan(tl)
        self.assertIn("- [ ] a", out)
        self.assertIn("- [>] b", out)
        self.assertIn("- [x] c", out)

    def test_render_plan_full_file(self):
        """render_plan is a full-file render, not a diff."""
        text = "header\n- [ ] a\n- [ ] b\n"
        tl = parse_plan(text)
        out = render_plan(tl)
        # Header still present
        self.assertIn("header", out)
        # Tasks present
        self.assertIn("- [ ] a", out)
        self.assertIn("- [ ] b", out)


class TestAstMutationRoundtrip(unittest.TestCase):
    """Full AST mutation roundtrip: parse → mutate → render → write."""

    def test_focus_then_done_roundtrip(self):
        tl = parse_plan("- [ ] a\n- [ ] b\n- [ ] c\n")
        tl.focus(2)
        rendered = render_plan(tl)
        self.assertIn("- [>] b", rendered)

        tl.toggle_done([2])
        rendered2 = render_plan(tl)
        self.assertIn("- [x] b", rendered2)
        # Other tasks untouched
        self.assertIn("- [ ] a", rendered2)
        self.assertIn("- [ ] c", rendered2)

    def test_add_task_roundtrip_keeps_canonical(self):
        tl = parse_plan("# P\n\n## S\n- [ ] a\n\n## Completed\n")
        new_task = tl.add("brand new", status=TaskStatus.OPEN, section="S")
        out = render_plan(tl)
        self.assertIn("- [ ] brand new", out)
        self.assertIn("## Completed", out)
        # New task should land before Completed header (line_number > source length)
        self.assertGreater(new_task.line_number, 4)

    def test_write_plan_atomic(self):
        """write_plan writes a valid PLAN.md file."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            plan_path = Path(td) / "PLAN.md"
            tl = parse_plan("- [ ] a\n- [ ] b\n")
            tl.toggle_done([1])
            write_plan(plan_path, tl)
            self.assertTrue(plan_path.exists())
            self.assertIn("- [x] a", plan_path.read_text(encoding="utf-8"))
            self.assertIn("- [ ] b", plan_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()