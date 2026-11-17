"""``ck done`` mirrors completions into the project ROADMAP.md (v0.13.6).

A hand-maintained roadmap checklist had a desync trap: ``ck done``
updated PLAN.md/HISTORY.md while the roadmap checkbox stayed open.
Now every ``- [ ]`` / ``- [>]`` / ``- [~]`` line whose title EXACTLY
matches a completed task flips to ``- [x]`` — best-effort, silent on
a missing roadmap, never breaking the done result.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib.cli import main


def _run_main(argv, root=None):
    cwd = os.getcwd()
    if root is not None:
        os.chdir(root)
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = main(argv)
    finally:
        os.chdir(cwd)
    return code, buf_out.getvalue(), buf_err.getvalue()


class TestRoadmapMirror(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ck_roadmap_mirror_"))
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        code, out, err = _run_main(["init"])
        assert code == 0, out + err
        self.roadmap = self.root / "ROADMAP.md"

    def tearDown(self):
        os.chdir(self.old_cwd)

    def _plan_id(self, index: int) -> str:
        plan = self.root / ".ck" / "PLAN.md"
        line = plan.read_text(encoding="utf-8").splitlines()[index]
        return line.split("<!-- id: ", 1)[1].split(" -->", 1)[0]

    def test_exact_title_match_flips_to_x(self):
        self.roadmap.write_text(
            "## Backlog\n- [ ] Ship the release\n"
            "## Active\n- [>] Polish the UI\n",
            encoding="utf-8")
        _run_main(["add", "Ship the release"])

        # The focused task completes via its stable id.
        code, out, err = _run_main(["done", "1"])
        self.assertEqual(code, 0, out + err)

        text = self.roadmap.read_text(encoding="utf-8")
        self.assertIn("- [x] Ship the release", text)
        self.assertIn("- [>] Polish the UI", text)  # untouched

    def test_done_marker_and_paused_marker_flip_too(self):
        self.roadmap.write_text(
            "- [>] focused one\n- [~] paused one\n- [ ] open one\n",
            encoding="utf-8")
        _run_main(["add", "focused one"])
        _run_main(["add", "paused one"])
        _run_main(["add", "open one"])

        code, out, err = _run_main(["done", "1-3"])
        self.assertEqual(code, 0, out + err)

        text = self.roadmap.read_text(encoding="utf-8")
        self.assertIn("- [x] focused one", text)
        self.assertIn("- [x] paused one", text)
        self.assertIn("- [x] open one", text)

    def test_partial_title_match_is_never_flipped(self):
        self.roadmap.write_text(
            "- [ ] Ship the release candidate\n- [x] Ship the release\n",
            encoding="utf-8")
        _run_main(["add", "Ship the release"])

        code, out, err = _run_main(["done", "1"])
        self.assertEqual(code, 0, out + err)

        text = self.roadmap.read_text(encoding="utf-8")
        # The longer open line is a DIFFERENT task — stays open.
        self.assertIn("- [ ] Ship the release candidate", text)

    def test_version_annotated_done_rows_are_untouched(self):
        self.roadmap.write_text(
            "- [x] Ship the release (v0.13.5)\n- [ ] Ship the release\n",
            encoding="utf-8")
        _run_main(["add", "Ship the release"])

        code, out, err = _run_main(["done", "1"])
        self.assertEqual(code, 0, out + err)

        text = self.roadmap.read_text(encoding="utf-8")
        self.assertIn("- [x] Ship the release (v0.13.5)", text)
        self.assertIn("- [x] Ship the release\n", text)

    def test_missing_roadmap_is_a_silent_noop(self):
        _run_main(["add", "alpha"])
        code, out, err = _run_main(["done", "1"])
        self.assertEqual(code, 0, out + err)
        self.assertFalse(self.roadmap.exists())

    def test_corrupt_roadmap_never_breaks_done(self):
        self.roadmap.write_bytes(b"\xff\xfe broken \x00")
        _run_main(["add", "alpha"])
        code, out, err = _run_main(["done", "1"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Marked done: 1", out)

    def test_unrelated_roadmap_checklists_untouched(self):
        self.roadmap.write_text(
            "- [ ] Build the docker image\n- [ ] Buy milk\n",
            encoding="utf-8")
        _run_main(["add", "Ship the release"])

        code, out, err = _run_main(["done", "1"])
        self.assertEqual(code, 0, out + err)

        text = self.roadmap.read_text(encoding="utf-8")
        self.assertIn("- [ ] Build the docker image", text)
        self.assertIn("- [ ] Buy milk", text)


if __name__ == "__main__":
    unittest.main()
