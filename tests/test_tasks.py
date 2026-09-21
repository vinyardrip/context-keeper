"""Tests for the CLI task-listing surface (Step 6, post-consolidation).

- ``ck list`` prints the local project task list to STDOUT without
  invoking any external editor.
- ``ck st -l`` / ``--list`` delegates to the same listing as ``ck list``.
- ``ck st -e`` / ``--edit`` delegates to ``ck edit`` (editor subprocess).
- ``ck st -g`` / ``--global`` delegates to ``ck dashboard``.
- ``ck -l`` / ``-e`` / ``-g`` (and their long forms) are top-level
  shorthands translating to the canonical ``ck st <flag>`` handling.
- ``tasks`` / ``status`` / ``list -g`` are gone: unknown commands and
  flags are rejected cleanly.
- ``ck -h`` help text documents the consolidated interface.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.cli import HELP_TEXT, main
from cklib.core import ContextKeeper, _render_tasks_listing
from cklib.parser import parse_plan


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
# Rendering: ck list output
# ---------------------------------------------------------------------------


class TestTasksRendering(unittest.TestCase):
    """``_render_tasks_listing`` is clean, grouped, pipe-friendly."""

    def test_renders_sections_and_statuses(self):
        text = (
            "# P\n"
            "## Current Sprint\n"
            "- [ ] first\n"
            "- [>] focused\n"
            "- [x] done one\n"
            "\n"
            "## Completed\n"
            "- [x] old done\n"
        )
        tl = parse_plan(text)
        out = _render_tasks_listing(tl)
        lines = out.splitlines()
        self.assertEqual(lines[0], "## Current Sprint")
        self.assertEqual(lines[1], "[ ] 1. first")
        self.assertEqual(lines[2], "[>] 2. focused")
        self.assertEqual(lines[3], "[x] 3. done one")
        # Section header re-emitted when the section changes
        self.assertEqual(lines[4], "## Completed")
        self.assertEqual(lines[5], "[x] 4. old done")

    def test_flat_list_without_sections(self):
        tl = parse_plan("- [ ] a\n- [ ] b\n")
        out = _render_tasks_listing(tl)
        self.assertEqual(out.splitlines(), ["[ ] 1. a", "[ ] 2. b"])

    def test_empty_plan_hint(self):
        tl = parse_plan("# nothing here\n")
        self.assertIn("No tasks", _render_tasks_listing(tl))

    def test_output_is_single_line_per_task(self):
        """Every task renders on exactly one line (pipe-friendly)."""
        text = (
            "## Current Sprint\n"
            "- [ ] alpha\n"
            "- [ ] beta\n"
            "- [x] gamma\n"
        )
        tl = parse_plan(text)
        out = _render_tasks_listing(tl)
        task_lines = [l for l in out.splitlines() if not l.startswith("##")]
        self.assertEqual(len(task_lines), tl.total)

    def test_grep_friendly_ids_and_titles(self):
        tl = parse_plan("- [ ] find me\n- [x] done\n")
        out = _render_tasks_listing(tl)
        self.assertIn("[ ] 1. find me", out)
        self.assertIn("[x] 2. done", out)


# ---------------------------------------------------------------------------
# Core method: ContextKeeper.tasks
# ---------------------------------------------------------------------------


class TestContextKeeperTasks(_IsolatedHome, unittest.TestCase):
    def _ck_with_plan(self, tmp: Path, text: str) -> ContextKeeper:
        root = tmp / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(text, encoding="utf-8")
        return ck

    def test_tasks_reads_local_plan(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck_with_plan(Path(td), (
                "# P\n## Current Sprint\n- [ ] alpha\n- [>] beta\n"
            ))
            out = ck.tasks()
            self.assertIn("[ ] 1. alpha", out)
            self.assertIn("[>] 2. beta", out)
            self.assertIn("## Current Sprint", out)

    def test_tasks_auto_repairs_corrupted_plan(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._ck_with_plan(Path(td), (
                "# P\n## Current Sprint\n- [ ] real\n"
                "\x1b[31mterminal noise\x1b[0m\n"
            ))
            out = ck.tasks()
            self.assertIn("[ ] 1. real", out)
            self.assertNotIn("\x1b", out)
            # The file was healed on disk by the read
            self.assertNotIn(
                "\x1b", ck.plan_file.read_text(encoding="utf-8")
            )

    def test_tasks_on_missing_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fresh"
            root.mkdir()
            ck = ContextKeeper(root=root)
            self.assertIn("No tasks", ck.tasks())


# ---------------------------------------------------------------------------
# CLI dispatch: ck list / ck st flags / ck dashboard
# ---------------------------------------------------------------------------


class TestCliDispatch(_IsolatedHome, unittest.TestCase):
    """End-to-end through main(): local vs global view consolidation."""

    def _make_project(self, tmp: Path, name: str = "project") -> ContextKeeper:
        root = tmp / name
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init(register=True)
        return ck

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

    def test_ck_list_prints_local_list(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp)
            ck.add_task("alpha task")
            ck.add_task("beta task")

            out = self._run(["list"], ck.root)
            self.assertIn("[ ] 1. Describe the first task", out)
            self.assertIn("[ ] 2. alpha task", out)
            self.assertIn("[ ] 3. beta task", out)

    def test_st_l_is_list(self):
        """`ck st -l` prints the same listing as `ck list`."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp)
            ck.add_task("local one")

            out_l = self._run(["st", "-l"], ck.root)
            out_list = self._run(["list"], ck.root)
            self.assertEqual(out_l, out_list)
            self.assertIn("local one", out_l)
            self.assertNotIn("GLOBAL DASHBOARD", out_l)

    def test_st_long_list_flag_is_list(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp)
            ck.add_task("local one")

            out_long = self._run(["st", "--list"], ck.root)
            out_short = self._run(["st", "-l"], ck.root)
            self.assertEqual(out_long, out_short)

    def test_st_g_is_dashboard(self):
        """`ck st -g` renders the same compact dashboard as `ck dashboard`."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a = self._make_project(tmp, "alpha-proj")
            self._make_project(tmp, "beta-proj")

            out_sg = self._run(["st", "-g"], a.root)
            out_dash = self._run(["dashboard"], a.root)
            self.assertEqual(out_sg, out_dash)
            self.assertIn("GLOBAL DASHBOARD", out_sg)
            self.assertNotIn("[ ] 1.", out_sg)  # not the local listing

    def test_st_g_long_flag_is_dashboard(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out = self._run(["st", "--global"], ck.root)
            self.assertIn("GLOBAL DASHBOARD", out)

    def test_st_e_delegates_to_edit_plan(self):
        """`ck st -e` routes through ContextKeeper.edit_plan (same as edit)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            with mock.patch.object(ContextKeeper, "edit_plan") as edit_plan:
                orig_cwd = Path.cwd()
                os.chdir(ck.root)
                try:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        code = main(["st", "-e"])
                finally:
                    os.chdir(orig_cwd)
            self.assertEqual(code, 0)
            edit_plan.assert_called_once_with()

    def test_st_e_long_flag_launches_editor_on_plan(self):
        """The real path launches the resolved editor on PLAN.md."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            calls = []
            with mock.patch(
                "cklib.core.subprocess.run",
                side_effect=lambda *a, **k: calls.append(a),
            ), mock.patch.dict(os.environ, {"EDITOR": "/bin/true",
                                            "VISUAL": ""}):
                orig_cwd = Path.cwd()
                os.chdir(ck.root)
                try:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        code = main(["st", "--edit"])
                finally:
                    os.chdir(orig_cwd)
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][0], "/bin/true")
            self.assertIn("PLAN.md", str(calls[0][0][1]))

    # -- top-level shorthands: `ck -l` / `-e` / `-g` ------------------- #

    def test_top_level_l_shorthand_is_list(self):
        """`ck -l` is an exact synonym for `ck st -l` (no argparse
        'unrecognized arguments' error any more)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp)
            ck.add_task("local one")

            out_short = self._run(["-l"], ck.root)
            out_st = self._run(["st", "-l"], ck.root)
            self.assertEqual(out_short, out_st)
            self.assertIn("local one", out_short)
            self.assertNotIn("unrecognized arguments", out_short)
            self.assertNotIn("GLOBAL DASHBOARD", out_short)

    def test_top_level_list_long_shorthand_is_list(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp)
            ck.add_task("local one")
            self.assertEqual(
                self._run(["--list"], ck.root),
                self._run(["-l"], ck.root),
            )

    def test_top_level_g_shorthand_is_dashboard(self):
        """`ck -g` renders the same compact dashboard as `ck st -g`."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a = self._make_project(tmp, "alpha-proj")
            self._make_project(tmp, "beta-proj")

            out_short = self._run(["-g"], a.root)
            out_st = self._run(["st", "-g"], a.root)
            self.assertEqual(out_short, out_st)
            self.assertIn("GLOBAL DASHBOARD", out_short)
            self.assertNotIn("unrecognized arguments", out_short)
            self.assertNotIn("[ ] 1.", out_short)  # not the local listing

    def test_top_level_global_long_shorthand_is_dashboard(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")
            out = self._run(["--global"], ck.root)
            self.assertIn("GLOBAL DASHBOARD", out)

    def test_top_level_e_shorthand_delegates_to_edit_plan(self):
        """`ck -e` routes through ContextKeeper.edit_plan (same as edit)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            with mock.patch.object(ContextKeeper, "edit_plan") as edit_plan:
                orig_cwd = Path.cwd()
                os.chdir(ck.root)
                try:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        code = main(["-e"])
                finally:
                    os.chdir(orig_cwd)
            self.assertEqual(code, 0)
            edit_plan.assert_called_once_with()
            self.assertNotIn("unrecognized arguments", buf.getvalue())

    def test_top_level_edit_long_shorthand_delegates_to_edit_plan(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            with mock.patch.object(ContextKeeper, "edit_plan") as edit_plan:
                orig_cwd = Path.cwd()
                os.chdir(ck.root)
                try:
                    with redirect_stdout(io.StringIO()):
                        code = main(["--edit"])
                finally:
                    os.chdir(orig_cwd)
            self.assertEqual(code, 0)
            edit_plan.assert_called_once_with()

    def test_help_documents_top_level_shorthands(self):
        self.assertIn("Shorthand for `ck st -l`", HELP_TEXT)
        self.assertIn("Shorthand for `ck st -e`", HELP_TEXT)
        self.assertIn("Shorthand for `ck st -g`", HELP_TEXT)

    def test_tasks_and_list_differ_from_dashboard(self):
        """Local and global views are genuinely different outputs."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out_list = self._run(["list"], ck.root)
            out_dash = self._run(["dashboard"], ck.root)
            self.assertNotIn("GLOBAL DASHBOARD", out_list)
            self.assertIn("GLOBAL DASHBOARD", out_dash)

    def test_dashboard_verbose_flag_is_block_view(self):
        """`ck dashboard -v` renders the verbose MY PROJECTS blocks."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out = self._run(["dashboard", "-v"], ck.root)
            self.assertIn("MY PROJECTS", out)
            self.assertNotIn("GLOBAL DASHBOARD", out)

    def test_dashboard_verbose_long_flag(self):
        """`ck dashboard --verbose` is equivalent to `-v`."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out_v = self._run(["dashboard", "-v"], ck.root)
            out_long = self._run(["dashboard", "--verbose"], ck.root)
            self.assertEqual(out_v, out_long)
            self.assertIn("MY PROJECTS", out_long)

    def test_dashboard_default_is_compact_table(self):
        """Bare `ck dashboard` renders the compact table, not blocks."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out = self._run(["dashboard"], ck.root)
            self.assertIn("GLOBAL DASHBOARD", out)
            self.assertIn("| Project", out)
            self.assertNotIn("MY PROJECTS", out)

    def test_removed_flags_are_rejected(self):
        """`list -g` / `dashboard -g` are no longer valid flags."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            for argv in (["list", "-g"], ["list", "--global"],
                         ["dashboard", "-g"], ["dashboard", "--global"]):
                buf, err = io.StringIO(), io.StringIO()
                orig_cwd = Path.cwd()
                os.chdir(ck.root)
                try:
                    with redirect_stdout(buf), redirect_stderr(err):
                        code = main(argv)
                finally:
                    os.chdir(orig_cwd)
                self.assertEqual(code, 2, f"argv={argv!r}")
                # Repo convention: errors print to stdout via _print_error.
                self.assertIn("ERROR", buf.getvalue(), f"argv={argv!r}")

    def test_removed_commands_are_rejected(self):
        """`tasks` / `status` are gone as top-level commands."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            for argv in (["tasks"], ["status"]):
                buf = io.StringIO()
                orig_cwd = Path.cwd()
                os.chdir(ck.root)
                try:
                    with redirect_stdout(buf):
                        code = main(argv)
                finally:
                    os.chdir(orig_cwd)
                self.assertNotEqual(code, 0, f"argv={argv!r}")
                self.assertNotIn("[ ] 1.", buf.getvalue())


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


class TestHelpText(unittest.TestCase):
    def test_help_includes_list(self):
        self.assertRegex(HELP_TEXT, r"(?m)^\s{2}list\s+Print task list")

    def test_help_documents_st_flags(self):
        self.assertIn("st [-l|-e|-g]", HELP_TEXT)
        self.assertIn("-l: list tasks", HELP_TEXT)
        self.assertIn("-e: edit plan", HELP_TEXT)
        self.assertIn("-g: global dashboard", HELP_TEXT)

    def test_help_documents_dashboard_verbose(self):
        self.assertIn("dashboard -v", HELP_TEXT)
        self.assertIn("triad context per project", HELP_TEXT)

    def test_help_does_not_document_removed_surface(self):
        # The removed commands must not appear as command entries
        # (the -l flag description legitimately contains the word
        # "tasks", so a substring check would be wrong).
        self.assertNotRegex(HELP_TEXT, r"(?m)^\s{2}tasks\b")
        self.assertNotRegex(HELP_TEXT, r"(?m)^\s{2}status\b")
        self.assertNotIn("list -g", HELP_TEXT)
        self.assertNotIn("st --global", HELP_TEXT)

    def test_help_groups_local_and_global_views(self):
        self.assertIn("Local (current project):", HELP_TEXT)
        self.assertIn("Global (all registered projects):", HELP_TEXT)

    def test_help_includes_editor_hierarchy(self):
        self.assertIn("$VISUAL", HELP_TEXT)
        self.assertIn(".ck.json", HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
