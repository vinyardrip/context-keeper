"""Tests for the multiline task description (v0.13.4).

Guards:

- parser/AST: indented ``> text`` lines beneath a task bullet attach
  as ``Task.description``; canonical plans round-trip byte-exactly
  (LF and CRLF), the render is idempotent, and mutations compose;
- ownership boundaries: column-0 blockquotes and ``>`` lines after
  unrelated prose stay unowned; descriptions coexist with inline
  notes without duplication (either order is lossless);
- CLI ``ck desc <ID>``: show / set / ``--stdin`` / ``--clear``, clean
  exit codes; ``ck list`` renders desc lines only when present
  (desc-less plans keep byte-identical output);
- ``ck export --task`` and the ``ck st`` CURRENT FOCUS block surface
  the description.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib.cli import main
from cklib.parser import parse_plan, render_plan


class TestDescriptionParser(unittest.TestCase):
    CANONICAL = (
        "# Demo\n"
        "\n"
        "## Current Sprint\n"
        "- [ ] First task <!-- id: aaaa1111 -->\n"
        "- [>] Second task <!-- id: bbbb2222 -->\n"
        "  > Desc line one\n"
        "  > Desc line two\n"
        "  >\n"
        "  > After blank\n"
        "  * Note (2026-11-15 10:00 UTC): waiting on CI\n"
        "- [ ] Third task <!-- id: cccc3333 -->\n"
        "\n"
        "## Completed\n"
    )

    def test_description_attaches_to_task(self):
        tl = parse_plan(self.CANONICAL)
        self.assertEqual(
            tl.by_id(2).description,
            "Desc line one\nDesc line two\n\nAfter blank")
        self.assertEqual(tl.by_id(1).description, "")
        self.assertEqual(tl.by_id(3).description, "")

    def test_canonical_round_trip_is_byte_exact(self):
        self.assertEqual(render_plan(parse_plan(self.CANONICAL)),
                         self.CANONICAL)

    def test_crlf_round_trip_is_byte_exact(self):
        crlf = self.CANONICAL.replace("\n", "\r\n")
        self.assertEqual(render_plan(parse_plan(crlf)), crlf)

    def test_render_is_idempotent(self):
        once = render_plan(parse_plan(self.CANONICAL))
        self.assertEqual(render_plan(parse_plan(once)), once)

    def test_notes_and_description_survive_either_order(self):
        # desc after note (non-canonical) round-trips LOSSLESSLY into
        # the canonical order (description before notes).
        reordered = self.CANONICAL.replace(
            "  > Desc line one\n"
            "  > Desc line two\n"
            "  >\n"
            "  > After blank\n"
            "  * Note (2026-11-15 10:00 UTC): waiting on CI\n",
            "  * Note (2026-11-15 10:00 UTC): waiting on CI\n"
            "  > Desc line one\n"
            "  > Desc line two\n"
            "  >\n"
            "  > After blank\n")
        out = render_plan(parse_plan(reordered))
        self.assertEqual(out, self.CANONICAL)

    def test_column_zero_blockquote_is_not_a_description(self):
        text = ("# P\n"
                "## Current Sprint\n"
                "- [ ] task one <!-- id: aaaa1111 -->\n"
                "\n"
                "> A top-level blockquote is prose\n"
                "- [ ] task two <!-- id: bbbb2222 -->\n")
        tl = parse_plan(text)
        self.assertEqual(tl.by_id(1).description, "")
        self.assertEqual(tl.by_id(2).description, "")
        self.assertEqual(render_plan(tl), text)

    def test_desc_after_prose_stays_unowned(self):
        text = ("# P\n"
                "## Current Sprint\n"
                "- [ ] task one <!-- id: aaaa1111 -->\n"
                "some prose line\n"
                "  > looks like a description but is not\n")
        tl = parse_plan(text)
        self.assertEqual(tl.by_id(1).description, "")

    def test_mutation_sets_and_renders_description(self):
        tl = parse_plan(self.CANONICAL)
        tl.by_id(3).description = "fresh\ntext\n"
        out = render_plan(tl)
        self.assertIn("  > fresh\n  > text\n", out)
        self.assertEqual(render_plan(parse_plan(out)), out)


class _Harness(unittest.TestCase):
    PLAN = ("# Demo\n\n## Current Sprint\n"
            "- [ ] Alpha task <!-- id: aaaa1111 -->\n"
            "- [>] Beta task <!-- id: bbbb2222 -->\n")

    def _project(self, plan: str = PLAN) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name) / "project"
        root.mkdir()
        (root / ".ck").mkdir()
        (root / ".ck" / "PLAN.md").write_text(plan, encoding="utf-8")
        (root / ".ck" / "HISTORY.md").write_text(
            "## Completed\n", encoding="utf-8")
        (root / ".ck" / "version").write_text("0.13.0\n", encoding="utf-8")
        return root

    def _run(self, argv, cwd: Path,
             stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = Path.cwd()
        os.chdir(cwd)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                if stdin:
                    with mock.patch("sys.stdin", io.StringIO(stdin)):
                        code = main(argv)
                else:
                    code = main(argv)
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def _plan(self, root: Path) -> str:
        return (root / ".ck" / "PLAN.md").read_text(encoding="utf-8")


class TestDescCommand(_Harness):
    def test_show_when_empty(self):
        root = self._project()
        code, out, _err = self._run(["desc", "1"], root)
        self.assertEqual(code, 0)
        self.assertIn("No description on task #1", out)

    def test_set_joins_words_and_stores_inline(self):
        root = self._project()
        code, out, _err = self._run(["desc", "1", "build", "the", "thing"],
                                    root)
        self.assertEqual(code, 0)
        self.assertIn("Description set", out)
        self.assertIn("build the thing", out)
        self.assertIn("  > build the thing\n", self._plan(root))

    def test_stdin_sets_multiline_and_preserves_blank_line(self):
        root = self._project()
        code, out, _err = self._run(
            ["desc", "2", "--stdin"], root,
            stdin="step one\nstep two\n\nstep three\n")
        self.assertEqual(code, 0)
        self.assertIn(
            "  > step one\n  > step two\n  >\n  > step three\n",
            self._plan(root))
        # Show prints the stored text verbatim.
        code, out, _err = self._run(["desc", "2"], root)
        self.assertEqual(code, 0)
        self.assertIn("step one\nstep two\n\nstep three", out)

    def test_clear_removes_block_from_plan(self):
        root = self._project()
        self._run(["desc", "1", "temporary"], root)
        code, out, _err = self._run(["desc", "1", "--clear"], root)
        self.assertEqual(code, 0)
        self.assertIn("Description cleared", out)
        self.assertNotIn("  > temporary", self._plan(root))
        # Round-trip after clear is byte-exact.
        self.assertEqual(
            render_plan(parse_plan(self._plan(root))), self._plan(root))

    def test_unknown_task_is_a_clean_error(self):
        root = self._project()
        code, out, _err = self._run(["desc", "99", "text"], root)
        self.assertEqual(code, 1)
        self.assertIn("No task with id 99", out)

    def test_clear_with_text_is_a_usage_error(self):
        root = self._project()
        code, out, _err = self._run(["desc", "1", "--clear", "text"], root)
        self.assertEqual(code, 2)
        self.assertIn("cannot be combined", out)


class TestDescriptionDisplay(_Harness):
    def test_list_renders_description_lines(self):
        root = self._project()
        self._run(["desc", "2", "--stdin"], root,
                  stdin="beta context\nline two\n")
        code, out, _err = self._run(["list"], root)
        self.assertEqual(code, 0)
        self.assertIn("[>] 2. Beta task", out)
        self.assertIn("    > beta context", out)
        self.assertIn("    > line two", out)

    def test_list_output_unchanged_without_descriptions(self):
        root = self._project()
        code, out, _err = self._run(["list"], root)
        self.assertEqual(code, 0)
        # No indented description lines anywhere.
        self.assertNotIn("\n    >", out)

    def test_status_focus_block_shows_description(self):
        root = self._project()
        self._run(["desc", "2", "focus context"], root)
        code, out, _err = self._run(["st"], root)
        self.assertEqual(code, 0)
        self.assertIn("CURRENT FOCUS:", out)
        self.assertIn("> focus context", out)

    def test_export_task_includes_description(self):
        root = self._project()
        self._run(["desc", "1", "--stdin"], root,
                  stdin="alpha details\nmore details\n")
        code, out, _err = self._run(["export", "--task", "1"], root)
        self.assertEqual(code, 0)
        self.assertIn("## Task #1 (open)", out)
        self.assertIn("alpha details", out)
        self.assertIn("more details", out)

    def test_space_plan_round_trips_with_description(self):
        # Space files share the same parser/renderer: a description
        # in a space file survives the SpaceManager load/commit cycle.
        from cklib import spaces
        root = self._project()
        old = Path.cwd()
        os.chdir(root)
        try:
            with redirect_stdout(io.StringIO()):
                code = main(["week", "init"])
            self.assertEqual(code, 0)
            mgr = spaces.SpaceManager("week")
            tl = mgr.load()
            mgr.set_note(1, "note text") if tl.tasks else None
        finally:
            os.chdir(old)
        path = spaces.space_path("week")
        path.write_text(
            "## Current Sprint\n"
            "- [ ] spaced task <!-- id: aaaa1111 -->\n"
            "  > spaced description\n",
            encoding="utf-8")
        mgr = spaces.SpaceManager("week")
        mgr._commit(mgr.load())
        self.assertIn(
            "  > spaced description\n",
            path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
