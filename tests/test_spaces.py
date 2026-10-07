"""Tests for Block 2: default-task replacement and out-of-project spaces.

Covered surface:

- ``ck add`` auto-REPLACES the untouched ``ck init`` seed task
  (``Describe the first task`` / ``Описать первую задачу``) instead of
  appending a second entry.
- Normal APPEND behaviour is preserved once the seed was edited,
  completed, focused, or accompanied by other tasks.
- ``ck local`` / ``ck remote`` manage ``~/.config/ck/spaces/*.md``
  with lazily-created parent directories.
- The ``[SYSTEM / OPS]`` block renders above the Git projects table in
  ``ck dashboard`` with focus + pending tasks from both spaces.
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
from cklib import spaces
from cklib.cli import main
from cklib.core import ContextKeeper


class _IsolatedHome(unittest.TestCase):
    """Pin global config (registry + spaces) to a per-test tmp HOME.

    Also pins the dev-mode toggles OFF (``CK_SANDBOX`` / ``CK_DEV`` /
    ``CK_SANDBOX_ACTIVE``) and the sandbox anchor, mirroring the
    pytest ``conftest`` autouse fixture — so these tests are
    byte-stable whether they run under pytest or plain
    ``python -m unittest`` (which does not load conftest).
    """

    _ENV_KEYS = ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                 "CK_SANDBOX_ROOT")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for key in ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE"):
            os.environ.pop(key, None)
        os.environ["CK_SANDBOX_ROOT"] = str(fake_home / ".sandbox")

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
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --------------------------------------------------------------------------- #
# 1. Default-task auto-replacement
# --------------------------------------------------------------------------- #


class TestDefaultTaskReplacement(_IsolatedHome):
    """A fresh plan's seed task is replaced, not duplicated."""

    def _fresh_project(self, tmp: Path, name: str = "project") -> ContextKeeper:
        root = tmp / name
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()
        return ck

    def test_first_add_replaces_seed_task(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._fresh_project(Path(td))
            self.assertIn(
                "Describe the first task",
                ck.plan_file.read_text(encoding="utf-8"),
            )

            new_id = ck.add_task("wire up the parser")

            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertEqual(new_id, 1)
            self.assertIn("- [ ] wire up the parser", text)
            self.assertNotIn("Describe the first task", text)
            # Exactly one task in the plan — no duplicate seed.
            self.assertEqual(ck.tasks().count(". "), 1)

    def test_replacement_recognizes_russian_seed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.ck_path.mkdir(parents=True, exist_ok=True)
            ck.plan_file.write_text(
                "# P\n## Current Sprint\n- [] Описать первую задачу\n"
                "\n## Completed\n",
                encoding="utf-8",
            )

            ck.add_task("собрать релиз")

            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [ ] собрать релиз", text)
            self.assertNotIn("Описать первую задачу", text)

    def test_cli_add_replaces_seed(self):
        with tempfile.TemporaryDirectory() as td:
            ck = self._fresh_project(Path(td))
            buf = io.StringIO()
            orig = Path.cwd()
            os.chdir(ck.root)
            try:
                with redirect_stdout(buf):
                    code = main(["add", "first real task"])
            finally:
                os.chdir(orig)
            self.assertEqual(code, 0)
            self.assertIn("Added task (id=1): first real task", buf.getvalue())
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertNotIn("Describe the first task", text)
            self.assertIn("- [ ] first real task", text)

    def test_edited_seed_is_appended_not_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.ck_path.mkdir(parents=True, exist_ok=True)
            ck.plan_file.write_text(
                "# P\n## Current Sprint\n- [ ] my custom seed\n"
                "\n## Completed\n",
                encoding="utf-8",
            )

            new_id = ck.add_task("second task")

            self.assertEqual(new_id, 2)
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [ ] my custom seed", text)
            self.assertIn("- [ ] second task", text)

    def test_multiple_tasks_append(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.ck_path.mkdir(parents=True, exist_ok=True)
            ck.plan_file.write_text(
                "# P\n## Current Sprint\n"
                "- [ ] Describe the first task\n- [ ] already here\n"
                "\n## Completed\n",
                encoding="utf-8",
            )

            new_id = ck.add_task("third task")

            self.assertEqual(new_id, 3)
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [ ] Describe the first task", text)
            self.assertIn("- [ ] already here", text)
            self.assertIn("- [ ] third task", text)

    def test_completed_seed_is_appended_not_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.ck_path.mkdir(parents=True, exist_ok=True)
            ck.plan_file.write_text(
                "# P\n## Current Sprint\n- [x] Describe the first task\n"
                "\n## Completed\n",
                encoding="utf-8",
            )

            new_id = ck.add_task("post-completion task")

            self.assertEqual(new_id, 2)
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [x] Describe the first task", text)
            self.assertIn("- [ ] post-completion task", text)

    def test_focused_seed_is_appended_not_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.ck_path.mkdir(parents=True, exist_ok=True)
            ck.plan_file.write_text(
                "# P\n## Current Sprint\n- [>] Describe the first task\n"
                "\n## Completed\n",
                encoding="utf-8",
            )

            new_id = ck.add_task("post-focus task")

            self.assertEqual(new_id, 2)
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [>] Describe the first task", text)
            self.assertIn("- [ ] post-focus task", text)

    def test_second_add_appends_after_replacement(self):
        """The shortcut fires only once; the next add is a plain append."""
        with tempfile.TemporaryDirectory() as td:
            ck = self._fresh_project(Path(td))
            self.assertEqual(ck.add_task("alpha"), 1)
            self.assertEqual(ck.add_task("beta"), 2)
            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [ ] alpha", text)
            self.assertIn("- [ ] beta", text)
            self.assertNotIn("Describe the first task", text)


# --------------------------------------------------------------------------- #
# 2. Out-of-project spaces (core API)
# --------------------------------------------------------------------------- #


class TestSpacesStorage(_IsolatedHome):
    def test_spaces_dir_created_lazily(self):
        self.assertFalse(spaces.spaces_dir().exists())
        spaces.add_space_task("local", "install drivers")
        self.assertTrue(spaces.spaces_dir().is_dir())
        self.assertTrue(spaces.space_path("local").is_file())

    def test_local_and_remote_are_separate_files(self):
        spaces.add_space_task("local", "local task")
        spaces.add_space_task("remote", "remote task")
        self.assertNotEqual(
            spaces.space_path("local"), spaces.space_path("remote")
        )
        self.assertIn("local task", spaces.space_path("local").read_text())
        self.assertIn("remote task", spaces.space_path("remote").read_text())
        self.assertNotIn("remote task", spaces.space_path("local").read_text())

    def test_add_assigns_incrementing_ids(self):
        self.assertEqual(spaces.add_space_task("local", "one"), 1)
        self.assertEqual(spaces.add_space_task("local", "two"), 2)
        listing = spaces.list_space("local")
        self.assertIn("[ ] 1. one", listing)
        self.assertIn("[ ] 2. two", listing)

    def test_list_empty_space(self):
        listing = spaces.list_space("remote")
        self.assertIn("[remote] 0/0 done", listing)

    def test_add_sanitizes_and_rejects_empty(self):
        spaces.add_space_task("local", "  spaced   text\t")
        self.assertIn("spaced text", spaces.list_space("local"))
        with self.assertRaises(ValueError):
            spaces.add_space_task("local", "   ")

    def test_unknown_space_rejected(self):
        with self.assertRaises(ValueError):
            spaces.add_space_task("bogus", "task")
        with self.assertRaises(ValueError):
            spaces.list_space("bogus")

    def test_focus_and_done_tasks_are_read(self):
        """Hand-authored focus/done markers are preserved on read."""
        path = spaces.space_path("local")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# local\n## Current Sprint\n"
            "- [>] focused thing\n- [ ] pending thing\n- [x] done thing\n"
            "\n## Completed\n",
            encoding="utf-8",
        )
        listing = spaces.list_space("local")
        self.assertIn("[>] 1. focused thing", listing)
        self.assertIn("[ ] 2. pending thing", listing)
        self.assertIn("[x] 3. done thing", listing)
        self.assertIn("[local] 1/3 done", listing)


# --------------------------------------------------------------------------- #
# 3. CLI dispatch: ck local / ck remote
# --------------------------------------------------------------------------- #


class TestSpacesCli(_IsolatedHome):
    def _run(self, argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def test_local_add_and_list(self):
        code, out = self._run(["local", "add", "update kernel"])
        self.assertEqual(code, 0)
        self.assertIn("local", out)
        code, out = self._run(["local", "list"])
        self.assertEqual(code, 0)
        self.assertIn("[ ] 1. update kernel", out)

    def test_remote_add_and_list(self):
        code, out = self._run(["remote", "add", "provision VPS"])
        self.assertEqual(code, 0)
        self.assertIn("remote", out)
        code, out = self._run(["remote", "list"])
        self.assertEqual(code, 0)
        self.assertIn("[ ] 1. provision VPS", out)

    def test_local_and_remote_do_not_bleed(self):
        self._run(["local", "add", "workstation task"])
        self._run(["remote", "add", "infra task"])
        _, local_out = self._run(["local", "list"])
        _, remote_out = self._run(["remote", "list"])
        self.assertIn("workstation task", local_out)
        self.assertNotIn("infra task", local_out)
        self.assertIn("infra task", remote_out)
        self.assertNotIn("workstation task", remote_out)

    def test_bare_space_prints_usage(self):
        code, out = self._run(["local"])
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck local", out)

    def test_add_without_text_is_usage_error(self):
        code, out = self._run(["remote", "add"])
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck remote add", out)

    def test_unknown_action_rejected(self):
        code, out = self._run(["local", "frobnicate"])
        self.assertEqual(code, 2)
        self.assertIn("Unknown", out)

    def test_aliases_are_not_supported(self):
        """Strictly explicit commands: no -s / sm / host / -m / virtual."""
        for argv in (["sm", "list"], ["host", "list"], ["virtual", "list"],
                     ["-s", "list"], ["-m", "list"]):
            code, _ = self._run(argv)
            self.assertNotEqual(code, 0, f"alias accepted: {argv!r}")


# --------------------------------------------------------------------------- #
# 4. [SYSTEM / OPS] dashboard overlay
# --------------------------------------------------------------------------- #


class TestSystemOpsBlock(_IsolatedHome):
    def test_block_header_and_empty_spaces(self):
        block = spaces.render_system_ops_block()
        self.assertEqual(block.splitlines()[0], "[SYSTEM / OPS]")
        self.assertIn("  LOCAL:", block)
        self.assertIn("  REMOTE:", block)
        self.assertIn("(empty)", block)

    def test_block_lists_focus_and_pending(self):
        path = spaces.space_path("local")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# local\n## Current Sprint\n"
            "- [>] install drivers\n- [ ] cleanup _tests\n",
            encoding="utf-8",
        )
        spaces.add_space_task("remote", "provision VPS")
        block = spaces.render_system_ops_block()
        self.assertIn("[>] [1] install drivers", block)
        self.assertIn("[ ] [2] cleanup _tests", block)
        self.assertIn("[ ] [1] provision VPS", block)

    def test_dashboard_renders_block_above_projects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "alpha"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.init(register=True)

            spaces.add_space_task("local", "workstation task")
            spaces.add_space_task("remote", "infra task")

            out = ContextKeeper(root=root).dashboard()
            self.assertIn("[SYSTEM / OPS]", out)
            self.assertIn("workstation task", out)
            self.assertIn("infra task", out)
            self.assertIn("GLOBAL DASHBOARD", out)
            self.assertLess(
                out.index("[SYSTEM / OPS]"), out.index("GLOBAL DASHBOARD")
            )

    def test_dashboard_renders_block_with_no_projects(self):
        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()
        self.assertIn("[SYSTEM / OPS]", out)
        self.assertIn("No registered projects", out)
        self.assertLess(
            out.index("[SYSTEM / OPS]"), out.index("No registered projects")
        )


if __name__ == "__main__":
    unittest.main()
