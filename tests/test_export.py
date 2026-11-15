"""Tests for ``ck export`` — the AI context assembly command.

Guards:

- the default report carries PLAN.md and HISTORY.md verbatim plus the
  ``## Recent Commits`` section (real ``git log -n 5 --oneline``);
- the report is copy-paste clean: no ANSI escapes, exit 0;
- graceful degradation: not-a-git-repo note, ``_(no plan yet)_`` /
  ``_(no history yet)_`` placeholders, uninitialized directory exits 1;
- ``--task <ID>`` exports ONE task (line + notes), PLAN.md first, then
  positionally into the HISTORY.md ledger; unknown IDs exit 1;
- ``--with-errors`` appends tails of the newest ``*.log`` / ``*.err``
  files from ``airlock/`` and ``tests/`` (newest first, tail only);
- both dispatch styles (legacy positional and the inline
  ``--task=<ID>`` form) agree.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib.cli import main


class _Harness(unittest.TestCase):
    PLAN = (
        "# Demo\n\n## Current Sprint\n"
        "- [ ] Alpha task <!-- id: aaaa1111 -->\n"
        "- [>] Beta task <!-- id: bbbb2222 -->\n"
        "  * Note (2026-11-15 10:00 UTC): waiting on CI\n"
    )
    HISTORY = (
        "## Completed\n\n"
        "- [x] 2026-11-14 09:00: Gamma done <!-- id: cccc3333 -->\n"
        "- [x] 2026-11-14 10:00: Delta done <!-- id: dddd4444 -->\n"
        "  * Note (2026-11-14 10:05 UTC): post-mortem note\n"
        "- [x] 2026-11-14 11:00: Epsilon done <!-- id: eeee5555 -->\n"
    )

    def _project(self, plan: str = PLAN, history: str = HISTORY) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name) / "project"
        root.mkdir()
        (root / ".ck").mkdir()
        (root / ".ck" / "PLAN.md").write_text(plan, encoding="utf-8")
        (root / ".ck" / "HISTORY.md").write_text(history, encoding="utf-8")
        (root / ".ck" / "version").write_text("0.13.0\n", encoding="utf-8")
        return root

    def _run(self, argv, cwd: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = Path.cwd()
        os.chdir(cwd)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(argv)
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def _git_commit(self, root: Path, message: str) -> None:
        env = dict(os.environ,
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q"], cwd=root, env=env,
                       capture_output=True, check=True)
        (root / "file.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=root, env=env,
                       capture_output=True, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=root,
                       env=env, capture_output=True, check=True)


class TestExportDefault(_Harness):
    def test_prints_plan_and_history(self):
        root = self._project()
        code, out, _err = self._run(["export"], root)
        self.assertEqual(code, 0)
        self.assertIn("# CK Context Export: project", out)
        self.assertIn("## Plan (PLAN.md)", out)
        self.assertIn("Alpha task", out)
        self.assertIn("Beta task", out)
        self.assertIn("waiting on CI", out)
        self.assertIn("## History (HISTORY.md)", out)
        self.assertIn("Gamma done", out)
        self.assertIn("Delta done", out)
        # Plan precedes history in the fixed section order.
        self.assertLess(out.index("## Plan (PLAN.md)"),
                        out.index("## History (HISTORY.md)"))

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_includes_recent_git_commits(self):
        root = self._project()
        self._git_commit(root, "feat: demo commit")
        code, out, _err = self._run(["export"], root)
        self.assertEqual(code, 0)
        self.assertIn("## Recent Commits", out)
        self.assertIn("feat: demo commit", out)

    def test_without_git_reports_placeholder(self):
        root = self._project()
        code, out, _err = self._run(["export"], root)
        self.assertEqual(code, 0)
        self.assertIn("## Recent Commits", out)
        self.assertIn("not a git repository", out)

    def test_output_has_no_ansi_escapes(self):
        root = self._project()
        code, out, _err = self._run(["export"], root)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b[", out)

    def test_missing_history_prints_placeholder(self):
        root = self._project(history="")
        (root / ".ck" / "HISTORY.md").unlink()
        code, out, _err = self._run(["export"], root)
        self.assertEqual(code, 0)
        self.assertIn("## History (HISTORY.md)", out)
        self.assertIn("no history yet", out)

    def test_uninitialized_directory_exits_one(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        bare = Path(td.name)
        code, out, _err = self._run(["export"], bare)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", out)
        self.assertIn("No active project found", out)


class TestExportTask(_Harness):
    def test_task_flag_exports_single_task_with_notes(self):
        root = self._project()
        code, out, _err = self._run(["export", "--task", "2"], root)
        self.assertEqual(code, 0)
        self.assertIn("## Task #2 (focused)", out)
        self.assertIn("Beta task", out)
        self.assertIn("* Note (2026-11-15 10:00 UTC): waiting on CI", out)
        # Single-task mode: no full plan/history dumps, no other tasks.
        self.assertNotIn("## Plan (PLAN.md)", out)
        self.assertNotIn("## History (HISTORY.md)", out)
        self.assertNotIn("Alpha task", out)
        self.assertNotIn("Gamma done", out)

    def test_task_inline_value_form(self):
        root = self._project()
        code, out, _err = self._run(["export", "--task=1"], root)
        self.assertEqual(code, 0)
        self.assertIn("## Task #1 (open)", out)
        self.assertIn("Alpha task", out)

    def test_task_flag_falls_back_to_history_positionally(self):
        # The plan holds IDs 1-2 only; ID 3 indexes the history ledger
        # positionally (1 = oldest entry).
        root = self._project()
        code, out, _err = self._run(["export", "--task", "3"], root)
        self.assertEqual(code, 0)
        self.assertIn("## Task #3 (done 2026-11-14 11:00)", out)
        self.assertIn("Epsilon done", out)
        # Plan IDs shadow the same numbers in the ledger: Gamma/Delta
        # (entries 1-2) stay unreachable and are not printed.
        self.assertNotIn("Gamma done", out)
        self.assertNotIn("Delta done", out)
        self.assertNotIn("Beta task", out)

    def test_unknown_task_id_is_a_clean_error(self):
        root = self._project()
        code, out, _err = self._run(["export", "--task", "99"], root)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", out)
        self.assertIn("Task #99 not found", out)

    def test_task_id_requires_a_value(self):
        root = self._project()
        code, out, _err = self._run(["export", "--task"], root)
        self.assertEqual(code, 2)
        self.assertIn("`--task` requires a task ID", out)


class TestExportWithErrors(_Harness):
    def test_appends_newest_error_logs(self):
        root = self._project()
        airlock = root / "airlock"
        tests_dir = root / "tests"
        airlock.mkdir()
        tests_dir.mkdir()
        (airlock / "run.log").write_text("ERROR 1: boom\n", encoding="utf-8")
        (tests_dir / "pytest.err").write_text("E   assert False\n",
                                              encoding="utf-8")
        # Deterministic recency: tests/pytest.err is the NEWEST log.
        os.utime(airlock / "run.log", (1_000, 1_000))
        os.utime(tests_dir / "pytest.err", (2_000, 2_000))
        code, out, _err = self._run(["export", "--with-errors"], root)
        self.assertEqual(code, 0)
        self.assertIn("## Recent Errors", out)
        self.assertIn("```text", out)
        self.assertIn("ERROR 1: boom", out)
        self.assertIn("E   assert False", out)
        # Newest first, both files present.
        self.assertLess(out.index("tests/pytest.err"),
                        out.index("airlock/run.log"))

    def test_tail_only_lines_are_captured(self):
        root = self._project()
        airlock = root / "airlock"
        airlock.mkdir()
        lines = [f"line {i}" for i in range(60)]
        (airlock / "big.log").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")
        code, out, _err = self._run(["export", "--with-errors"], root)
        self.assertEqual(code, 0)
        self.assertIn("line 59", out)   # last line present
        self.assertNotIn("line 0\n", out)  # the head is cut off

    def test_no_logs_prints_muted_note(self):
        root = self._project()
        code, out, _err = self._run(["export", "--with-errors"], root)
        self.assertEqual(code, 0)
        self.assertIn("## Recent Errors", out)
        self.assertIn("no error logs found", out)

    def test_flags_combine(self):
        root = self._project()
        airlock = root / "airlock"
        airlock.mkdir()
        (airlock / "run.log").write_text("ERROR: boom\n", encoding="utf-8")
        code, out, _err = self._run(
            ["export", "--task", "1", "--with-errors"], root)
        self.assertEqual(code, 0)
        self.assertIn("## Task #1 (open)", out)
        self.assertIn("## Recent Errors", out)
        self.assertNotIn("## Plan (PLAN.md)", out)


if __name__ == "__main__":
    unittest.main()
