"""Tests for `ck tasks` and dashboard/list consolidation (Step 6).

- ``ck tasks`` prints the local project task list to STDOUT without
  invoking any external editor.
- Local view: ``ck list`` == ``ck tasks`` (local task list).
- Global view: ``ck list -g`` / ``--global`` == ``ck dashboard``.
- ``ck -h`` help text documents ``tasks`` and the -g/--global flags.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.cli import HELP_TEXT, main
from cklib.core import ContextKeeper, _render_tasks_listing
from cklib.models import TaskStatus
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
# Rendering: ck tasks output
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
# CLI dispatch: ck tasks / ck list / ck list -g / ck dashboard
# ---------------------------------------------------------------------------


class TestCliDispatch(_IsolatedHome, unittest.TestCase):
    """End-to-end through main(): local vs global view consolidation."""

    def _make_project(self, tmp: Path, name: str = "project") -> ContextKeeper:
        root = tmp / name
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()
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

    def test_ck_tasks_prints_local_list(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp)
            ck.add_task("alpha task")
            ck.add_task("beta task")

            out = self._run(["tasks"], ck.root)
            self.assertIn("[ ] 1. Описать первую задачу", out)
            self.assertIn("[ ] 2. alpha task", out)
            self.assertIn("[ ] 3. beta task", out)

    def test_ck_list_is_local_tasks(self):
        """Local view: ck list == ck tasks."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp)
            ck.add_task("local one")

            out_list = self._run(["list"], ck.root)
            out_tasks = self._run(["tasks"], ck.root)
            self.assertEqual(out_list, out_tasks)
            self.assertIn("local one", out_list)

    def test_ck_list_g_is_dashboard(self):
        """Global view: ck list -g == ck dashboard."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a = self._make_project(tmp, "alpha-proj")
            self._make_project(tmp, "beta-proj")

            out_lg = self._run(["list", "-g"], a.root)
            out_dash = self._run(["dashboard"], a.root)
            self.assertEqual(out_lg, out_dash)
            self.assertIn("GLOBAL DASHBOARD", out_lg)

    def test_ck_list_global_flag_is_dashboard(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out = self._run(["list", "--global"], ck.root)
            self.assertIn("GLOBAL DASHBOARD", out)

    def test_tasks_and_list_differ_from_dashboard(self):
        """Local and global views are genuinely different outputs."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out_tasks = self._run(["tasks"], ck.root)
            out_dash = self._run(["dashboard"], ck.root)
            self.assertNotIn("GLOBAL DASHBOARD", out_tasks)
            self.assertIn("GLOBAL DASHBOARD", out_dash)

    def test_dashboard_verbose_flag_is_block_view(self):
        """`ck dashboard -v` renders the verbose МОИ ПРОЕКТЫ blocks."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out = self._run(["dashboard", "-v"], ck.root)
            self.assertIn("МОИ ПРОЕКТЫ", out)
            self.assertNotIn("GLOBAL DASHBOARD", out)

    def test_dashboard_verbose_long_flag(self):
        """`ck dashboard --verbose` is equivalent to `-v`."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out_v = self._run(["dashboard", "-v"], ck.root)
            out_long = self._run(["dashboard", "--verbose"], ck.root)
            self.assertEqual(out_v, out_long)
            self.assertIn("МОИ ПРОЕКТЫ", out_long)

    def test_dashboard_default_is_compact_table(self):
        """Bare `ck dashboard` renders the compact table, not blocks."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck = self._make_project(tmp, "alpha-proj")

            out = self._run(["dashboard"], ck.root)
            self.assertIn("GLOBAL DASHBOARD", out)
            self.assertIn("| Project", out)
            self.assertNotIn("МОИ ПРОЕКТЫ", out)


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


class TestHelpText(unittest.TestCase):
    def test_help_includes_tasks(self):
        self.assertIn("tasks", HELP_TEXT)
        self.assertRegex(
            HELP_TEXT, r"tasks\s+Print the local task list"
        )

    def test_help_documents_global_flags(self):
        self.assertIn("list -g, --global", HELP_TEXT)
        self.assertIn("st --global", HELP_TEXT)

    def test_help_documents_dashboard_verbose(self):
        self.assertIn("dashboard -v, --verbose", HELP_TEXT)
        self.assertIn("triad context per project", HELP_TEXT)

    def test_help_groups_local_and_global_views(self):
        self.assertIn("Local (current project):", HELP_TEXT)
        self.assertIn("Global (all registered projects):", HELP_TEXT)

    def test_help_includes_editor_hierarchy(self):
        self.assertIn("$VISUAL", HELP_TEXT)
        self.assertIn(".ck.json", HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
