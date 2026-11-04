"""Tests for the grouped dpx-style top-level help (v0.11.0, v0.11.1 color).

Guards:

- every group header of the target layout is present;
- the banner line opens the output;
- ONE global description column across all groups;
- inline aliases (``delete (alias: rm)``, the -l/-e/-g shorthand lines);
- ``ck -h`` / ``ck --help`` / ``ck help`` are byte-identical;
- no argparse garbage (``(choose from``, ``positional arguments``,
  stock ``usage:`` line);
- every command documented in the README Commands table appears in the
  help (a small whitelist for meta/external entrypoints);
- per-command ``--help`` still works (exit 0, argparse format);
- the 0.9.2 unknown-command one-liner is unchanged;
- v0.11.1: TTY / FORCE_COLOR color, NO_COLOR and pipes stay plain,
  and ANSI never changes alignment.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (filesystem isolation safety net)

import cklib.ui as ckui
from cklib.cli import HELP_TEXT, main, render_help

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CK_LAUNCHER = REPO_ROOT / "ck"

# Meta names documented in the README table that are intentionally NOT
# command entries of the grouped help:
# - `rm` is shown inline on the delete line as "(alias: rm)";
# - `ck-dev` / `ck-clean` are external entrypoints (sandbox shell and
#   cleanup script), not `ck` subcommands.
HELP_OMITTED = frozenset({"rm", "ck-dev", "ck-clean"})

GROUP_HEADERS = (
    "USAGE:",
    "TASKS:",
    "NOTES:",
    "VIEW:",
    "SAVE:",
    "SPACES (GLOBAL CONTEXTS):",
    "PROJECTS:",
    "SHELL:",
    "SELF:",
    "DEV:",
    "SHORTHANDS:",
    "OPTIONS:",
)


def _run(argv) -> tuple[int, str, str]:
    """Run main(argv) capturing stdout/stderr; returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as e:
                code = int(e.code) if e.code is not None else 2
    finally:
        pass
    return code, ckui.strip_ansi(out.getvalue()), ckui.strip_ansi(
        err.getvalue())


def _run_binary(argv) -> subprocess.CompletedProcess:
    """Run the real launcher in a scratch cwd via subprocess."""
    with tempfile.TemporaryDirectory() as td:
        return subprocess.run(
            [sys.executable, str(CK_LAUNCHER), *argv],
            capture_output=True, text=True, cwd=td, timeout=60,
        )


def _clean_color_env(**overrides) -> dict:
    """Environment without inherited color toggles, plus overrides."""
    env = {
        key: value for key, value in os.environ.items()
        if key not in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE")
    }
    env.update(overrides)
    return env


def _run_binary_env(argv, env) -> subprocess.CompletedProcess:
    """Run the real launcher with an explicit color environment."""
    with tempfile.TemporaryDirectory() as td:
        return subprocess.run(
            [sys.executable, str(CK_LAUNCHER), *argv],
            capture_output=True, text=True, cwd=td, timeout=60, env=env,
        )


class TestGroupedHelpLayout(unittest.TestCase):
    def test_group_headers_present(self):
        for header in GROUP_HEADERS:
            self.assertIn(header, HELP_TEXT)

    def test_banner_opens_output(self):
        self.assertTrue(HELP_TEXT.startswith("--- CK - Context Keeper ---"))

    def test_alignment_is_one_global_column(self):
        """Every entry line shares the same description column offset."""
        offsets = set()
        for line in HELP_TEXT.splitlines():
            match = re.match(r"^    (\S.*?)  +(\S.*)$", line)
            if match is None:
                continue
            offsets.add(match.start(2))
        self.assertTrue(offsets, "no entry lines matched")
        self.assertEqual(len(offsets), 1,
                         f"ragged description columns: {sorted(offsets)}")

    def test_aliases_and_shorthands_shown(self):
        self.assertRegex(HELP_TEXT, r"(?m)^    delete <ID> .*alias: rm")
        for line in ("-l, --list", "-e, --edit", "-g, --global"):
            self.assertRegex(
                HELP_TEXT,
                rf"(?m)^    {re.escape(line)}  +\S",
            )

    def test_no_argparse_garbage(self):
        self.assertNotIn("(choose from", HELP_TEXT)
        self.assertNotIn("positional arguments", HELP_TEXT)
        self.assertNotIn("usage: ck", HELP_TEXT)

    def test_every_readme_documented_command_is_present(self):
        """The README Commands table is the canonical command spec."""
        text = HELP_TEXT
        names = self._readme_command_names()
        self.assertTrue(names, "README command table was not parsed")
        for name in sorted(names):
            with self.subTest(command=name):
                self.assertIn(name, text)

    @staticmethod
    def _readme_command_names() -> set:
        """First token after ``ck `` in every EN `| ck …` table row."""
        names: set = set()
        for line in README.read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("|"):
                continue
            m = re.match(r"^\|\s*(ck[ \-][^|]*)", line)
            if m is None:
                continue
            cell = m.group(1).strip()
            tokens = cell.split()
            if len(tokens) < 2:
                continue  # `| ck |` meta row (e.g. -h/-v summary rows)
            name = tokens[0]
            if "<" in name or "`" in name:
                continue  # placeholder rows: `ck <space> <command>`
            if "-v" in name and "--version" in cell:
                names.add("-v")
                continue
            if "-h" in name and "--help" in cell:
                names.add("-h")
                continue
            if name in HELP_OMITTED:
                continue
            names.add(name)
        return names

    def test_three_entry_points_byte_identical(self):
        first = None
        for argv in (["-h"], ["--help"], ["help"]):
            done = _run_binary(argv)
            self.assertEqual(done.returncode, 0, argv)
            if first is None:
                first = done.stdout
            else:
                self.assertEqual(done.stdout, first)
        self.assertTrue(first.startswith("--- CK - Context Keeper ---"))


class TestHelpColors(unittest.TestCase):
    def test_no_color_matches_plain_golden(self):
        env = _clean_color_env(NO_COLOR="1")
        done = _run_binary_env(["-h"], env)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stderr, "")
        self.assertEqual(done.stdout, HELP_TEXT + "\n")
        self.assertNotIn("\x1b[", done.stdout)

    def test_redirected_output_is_plain_without_color_env(self):
        """A pipe is not a TTY, so help stays escape-free."""
        done = _run_binary_env(["-h"], _clean_color_env())
        self.assertEqual(done.returncode, 0)
        self.assertNotIn("\x1b[", done.stdout)
        self.assertEqual(done.stdout, HELP_TEXT + "\n")

    def test_force_color_styles_headers_and_commands(self):
        done = _run_binary_env(["-h"], _clean_color_env(FORCE_COLOR="1"))
        self.assertEqual(done.returncode, 0)
        self.assertIn("\x1b[1;36mTASKS:\x1b[0m", done.stdout)
        self.assertIn("\x1b[1;32madd <text>\x1b[0m", done.stdout)
        self.assertIn("\x1b[1m--- CK - Context Keeper ---\x1b[0m", done.stdout)
        # Descriptions (and inline aliases) stay neutral.
        self.assertIn("            Add a new task", done.stdout)
        self.assertIn("Delete a task (alias: rm)", done.stdout)

    def test_ansi_is_zero_width_alignment_preserved(self):
        plain = _run_binary_env(["-h"], _clean_color_env(NO_COLOR="1"))
        colored = _run_binary_env(["-h"], _clean_color_env(FORCE_COLOR="1"))
        self.assertEqual(plain.stdout, ckui.strip_ansi(colored.stdout))

    def test_no_color_wins_over_force_color(self):
        plain = _run_binary_env(["-h"], _clean_color_env(NO_COLOR="1"))
        both = _run_binary_env(
            ["-h"], _clean_color_env(NO_COLOR="1", FORCE_COLOR="1"))
        self.assertEqual(both.stdout, plain.stdout)

    def test_tty_detection_controls_color(self):
        class _FakeStdout:
            def __init__(self, tty: bool) -> None:
                self._tty = tty

            def isatty(self) -> bool:
                return self._tty

        with mock.patch.dict(os.environ, {
                "NO_COLOR": "", "FORCE_COLOR": "", "CLICOLOR_FORCE": ""}):
            with mock.patch("cklib.cli.sys.stdout", _FakeStdout(False)):
                plain = render_help()
            with mock.patch("cklib.cli.sys.stdout", _FakeStdout(True)):
                colored = render_help()
        self.assertNotIn("\x1b[", plain)
        self.assertIn("\x1b[1;36m", colored)
        self.assertEqual(plain, ckui.strip_ansi(colored))


class TestSubcommandHelp(unittest.TestCase):
    def test_add_and_st_help_exit_zero(self):
        for argv in (["add", "--help"], ["st", "--help"]):
            code, out, _err = _run(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("usage:", out)

    def test_ck_help_command_routes_to_subcommand_detail(self):
        code, out, _err = _run(["help", "st"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)


class TestUnknownCommandUnchanged(unittest.TestCase):
    def test_status_gives_one_liner_exit_2(self):
        code, _out, err = _run(["status"])
        self.assertEqual(code, 2)
        self.assertIn(
            "ck: error: unknown command 'status' "
            "(run `ck --help` for the full list)",
            err,
        )


if __name__ == "__main__":
    unittest.main()
