"""TTY-only context banner for ``ck list`` (v0.13.6).

Piped output must stay byte-identical (the documented grep/cut
contract); on a terminal the listing gains one muted context line:
``[i] project: <name> — <path> (<N> open)`` — so the reader always
knows WHICH plan the rows belong to.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

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


class TestListContextBanner(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ck_list_banner_")
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        code, out, err = _run_main(["init"])
        assert code == 0, out + err
        code, out, err = _run_main(["add", "alpha"])
        assert code == 0, out + err

    def tearDown(self):
        os.chdir(self.old_cwd)

    def test_piped_output_has_no_banner(self):
        """Redirected STDOUT keeps the listing clean (grep contract)."""
        code, out, _err = _run_main(["list"], self.root)
        self.assertEqual(code, 0)
        self.assertFalse(out.startswith("[i] project:"))
        self.assertIn("## Current Sprint", out)

    def test_tty_output_carries_banner_above_listing(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        buf = FakeTTY()
        with redirect_stdout(buf):
            code = main(["list"])
        out_tty = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertTrue(out_tty.startswith("[i] project: "))
        self.assertIn(f"— {os.path.realpath(self.root)}", out_tty)
        self.assertIn("(1 open)", out_tty)
        self.assertIn("## Current Sprint", out_tty)

    def test_unknown_project_name_has_no_banner(self):
        code, out, _err = _run_main(["list", "no-such-project"], self.root)
        self.assertEqual(code, 1)
        self.assertNotIn("[i] project:", out)


if __name__ == "__main__":
    unittest.main()
