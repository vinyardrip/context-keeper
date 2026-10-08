"""Tests for Block 2: default-task replacement and out-of-project spaces.

Covered surface:

- ``ck add`` auto-REPLACES the untouched ``ck init`` seed task
  (``Describe the first task`` / ``Описать первую задачу``) instead of
  appending a second entry.
- Normal APPEND behaviour is preserved once the seed was edited,
  completed, focused, or accompanied by other tasks.
- ``ck local`` / ``ck remote`` manage ``~/.config/ck/spaces/*.md``
  with lazily-created parent directories.
- Space command routing: ``ck <space> done|focus|note <ID>`` works
  for the built-in spaces (``local`` / ``remote``) AND for any
  custom space file discovered in ``~/.config/ck/spaces/``
  (dynamic space routing through the centralized
  :class:`cklib.spaces.SpaceManager`).
- ``ck <space> list`` renders through the SAME task-list
  engine as the project ``ck list`` (``##`` section headers,
  ``[ ]`` / ``[>]`` / ``[x]`` markers) and displays attached
  process notes (``* Note: <text>``) under their tasks.
- The unified ``SPACES (GLOBAL CONTEXTS)`` table renders above the Git
  projects table in ``ck dashboard``, using the SAME grid formatter
  (Space | Focus Task | Progress | Last Active) — no list-style dump.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import sandbox as cksandbox
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

    def test_replacement_recognizes_marked_seed(self):
        """A localized seed carrying the structural marker is replaced."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.ck_path.mkdir(parents=True, exist_ok=True)
            ck.plan_file.write_text(
                "# P\n## Current Sprint\n"
                "- [] Описать первую задачу <!-- ck:placeholder -->\n"
                "\n## Completed\n",
                encoding="utf-8",
            )

            ck.add_task("собрать релиз")

            text = ck.plan_file.read_text(encoding="utf-8")
            self.assertIn("- [ ] собрать релиз", text)
            self.assertNotIn("Описать первую задачу", text)
            self.assertNotIn("<!-- ck:placeholder -->", text)

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
# 4. Unified SPACES (GLOBAL CONTEXTS) dashboard table
# --------------------------------------------------------------------------- #


class TestSpacesTable(_IsolatedHome):
    """LOCAL / REMOTE render through the shared table pipeline."""

    def test_spaces_render_in_table_not_list_dump(self):
        spaces.add_space_task("local", "install drivers")
        spaces.add_space_task("remote", "provision VPS")

        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()

        # Exactly one header row with the four standard columns.
        header = [l for l in out.splitlines() if l.startswith("| Space")]
        self.assertEqual(len(header), 1, f"no spaces header in:\n{out}")
        cells = [c.strip() for c in header[0].strip("|").split("|")]
        self.assertEqual(
            cells, ["Space", "Focus Task", "Progress", "Last Active"])
        # Both spaces appear as table rows.
        self.assertIn("LOCAL", out)
        self.assertIn("REMOTE", out)
        # The old list-style section is gone.
        self.assertNotIn("[SYSTEM / OPS]", out)

    def test_spaces_cells_carry_focus_progress_and_last_active(self):
        path = spaces.space_path("local")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# local\n## Current Sprint\n"
            "- [>] install drivers\n- [ ] cleanup _tests\n",
            encoding="utf-8",
        )
        spaces.add_space_task("remote", "provision VPS")

        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()

        # Focus cell: [<id>] [>] head plus the focused task title.
        self.assertIn("[1] [>]", out)
        self.assertIn("install drivers", out)
        # Progress cell: single-line "<done>/<total> (<pct>%)".
        self.assertRegex(out, r"0/2 \(0\.0%\)")
        # Space cell carries name + path (contracted like projects).
        self.assertIn("spaces/local.md", out)
        # The unformatted task list is NOT dumped any more.
        self.assertNotIn("[SYSTEM / OPS]", out)
        self.assertNotIn("[ ] [2] cleanup _tests", out)

    def test_spaces_table_renders_above_projects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "alpha"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.init(register=True)

            spaces.add_space_task("local", "workstation task")
            spaces.add_space_task("remote", "infra task")

            out = ContextKeeper(root=root).dashboard()
            self.assertIn("SPACES (GLOBAL CONTEXTS)", out)
            self.assertIn("GLOBAL DASHBOARD", out)
            self.assertLess(
                out.index("SPACES (GLOBAL CONTEXTS)"),
                out.index("GLOBAL DASHBOARD"),
            )

    def test_spaces_table_renders_with_no_projects(self):
        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()
        self.assertIn("SPACES (GLOBAL CONTEXTS)", out)
        self.assertIn("No registered projects", out)
        self.assertLess(
            out.index("SPACES (GLOBAL CONTEXTS)"),
            out.index("No registered projects"),
        )


# --------------------------------------------------------------------------- #
# 5. Space command routing: done / focus / note (built-in spaces)
# --------------------------------------------------------------------------- #


class TestSpaceCommandRouting(_IsolatedHome):
    """``ck <space> done|focus|note <ID>`` for local and remote.

    Every action persists into the space's Markdown file through
    the centralized SpaceManager (the same Task/Plan engine a
    project PLAN.md uses) and is reflected by ``ck dashboard``
    / ``ck -g``.
    """

    def _run(self, argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def _seed(self, space: str, *titles: str) -> None:
        for title in titles:
            code, out = self._run([space, "add", title])
            self.assertEqual(code, 0, out)

    def _space_text(self, space: str) -> str:
        path = spaces.space_path(space)
        self.assertTrue(path.is_file(), f"{space}.md was not created")
        return path.read_text(encoding="utf-8")

    # ------------------------- done ------------------------- #

    def test_local_done_persists_to_space_file(self):
        self._seed("local", "first task", "second task")

        code, out = self._run(["local", "done", "1"])

        self.assertEqual(code, 0)
        self.assertIn("[ok] Marked done: 1", out)
        text = self._space_text("local")
        self.assertIn("- [x] first task", text)
        self.assertIn("- [ ] second task", text)
        _, listing = self._run(["local", "list"])
        self.assertIn("[x] 1. first task", listing)
        self.assertIn("[local] 1/2 done", listing)

    def test_local_done_range(self):
        self._seed("local", "alpha", "beta", "gamma")

        code, out = self._run(["local", "done", "1-2"])

        self.assertEqual(code, 0)
        self.assertIn("[ok] Marked done: 1, 2", out)
        text = self._space_text("local")
        self.assertIn("- [x] alpha", text)
        self.assertIn("- [x] beta", text)
        self.assertIn("- [ ] gamma", text)

    def test_local_done_bare_completes_current_focus(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "2"])

        code, out = self._run(["local", "done"])

        self.assertEqual(code, 0)
        self.assertIn("[ok] Marked done: 2", out)
        self.assertIn("- [x] second task", self._space_text("local"))

    def test_local_done_bare_without_focus_is_usage_error(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "done"])

        self.assertEqual(code, 2)
        self.assertIn("Usage: ck local done", out)

    def test_local_done_unknown_id_is_clean_error(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "done", "99"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("Unknown task ID", out)
        # Nothing was mutated.
        self.assertNotIn("[x]", self._space_text("local"))

    def test_local_done_invalid_spec_is_clean_error(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "done", "abc"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)

    def test_remote_done_persists_to_space_file(self):
        self._seed("remote", "provision VPS", "open firewall")

        code, out = self._run(["remote", "done", "1"])

        self.assertEqual(code, 0)
        self.assertIn("[ok] Marked done: 1", out)
        text = self._space_text("remote")
        self.assertIn("- [x] provision VPS", text)
        self.assertIn("- [ ] open firewall", text)
        _, listing = self._run(["remote", "list"])
        self.assertIn("[x] 1. provision VPS", listing)
        self.assertIn("[remote] 1/2 done", listing)

    def test_remote_done_bare_completes_current_focus(self):
        self._seed("remote", "provision VPS", "open firewall")
        self._run(["remote", "focus", "2"])

        code, out = self._run(["remote", "done"])

        self.assertEqual(code, 0)
        self.assertIn("[ok] Marked done: 2", out)
        self.assertIn("- [x] open firewall", self._space_text("remote"))

    def test_remote_done_unknown_id_is_clean_error(self):
        self._seed("remote", "provision VPS")

        code, out = self._run(["remote", "done", "42"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("Unknown task ID", out)

    # ------------------------- focus ------------------------- #

    def test_local_focus_persists_focus_marker(self):
        self._seed("local", "first task", "second task")

        code, out = self._run(["local", "focus", "2"])

        self.assertEqual(code, 0)
        self.assertIn("-> Focused [2]: second task", out)
        text = self._space_text("local")
        self.assertIn("- [>] second task", text)
        self.assertIn("- [ ] first task", text)
        _, listing = self._run(["local", "list"])
        self.assertIn("[>] 2. second task", listing)

    def test_local_focus_demotes_previous_focus(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "1"])

        code, out = self._run(["local", "focus", "2"])

        self.assertEqual(code, 0)
        self.assertIn("-> Focused [2]: second task", out)
        text = self._space_text("local")
        self.assertIn("- [>] second task", text)
        self.assertIn("- [ ] first task", text)

    def test_local_focus_is_idempotent(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "2"])

        code, out = self._run(["local", "focus", "2"])

        self.assertEqual(code, 0)
        self.assertIn("Task #2 is already focused.", out)
        # Exactly one focus marker survives.
        self.assertEqual(self._space_text("local").count("[>]"), 1)

    def test_local_focus_reset_with_zero(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "1"])

        code, out = self._run(["local", "focus", "0"])

        self.assertEqual(code, 0)
        self.assertIn("[ok] Focus reset.", out)
        self.assertNotIn("[>]", self._space_text("local"))

    def test_local_focus_unknown_id_is_clean_error(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "focus", "99"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("No task with id 99", out)

    def test_local_focus_invalid_id_is_clean_error(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "focus", "abc"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("Invalid task ID", out)

    def test_remote_focus_persists_focus_marker(self):
        self._seed("remote", "provision VPS", "open firewall")

        code, out = self._run(["remote", "focus", "2"])

        self.assertEqual(code, 0)
        self.assertIn("-> Focused [2]: open firewall", out)
        text = self._space_text("remote")
        self.assertIn("- [>] open firewall", text)
        self.assertIn("- [ ] provision VPS", text)

    def test_remote_focus_demotes_previous_focus(self):
        self._seed("remote", "provision VPS", "open firewall")
        self._run(["remote", "focus", "1"])

        code, out = self._run(["remote", "focus", "2"])

        self.assertEqual(code, 0)
        self.assertIn("-> Focused [2]: open firewall", out)
        text = self._space_text("remote")
        self.assertIn("- [>] open firewall", text)
        self.assertIn("- [ ] provision VPS", text)

    def test_remote_focus_reset_with_zero(self):
        self._seed("remote", "provision VPS")
        self._run(["remote", "focus", "1"])

        code, out = self._run(["remote", "focus", "0"])

        self.assertEqual(code, 0)
        self.assertIn("[ok] Focus reset.", out)
        self.assertNotIn("[>]", self._space_text("remote"))

    def test_remote_focus_unknown_id_is_clean_error(self):
        self._seed("remote", "provision VPS")

        code, out = self._run(["remote", "focus", "7"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("No task with id 7", out)

    # ------------------------- note ------------------------- #

    def test_local_note_persists_to_state_sidecar(self):
        self._seed("local", "first task", "second task")

        code, out = self._run(["local", "note", "1", "debugging auth"])

        self.assertEqual(code, 0)
        self.assertIn("* Note saved for [1]: debugging auth", out)
        # The note survives in the space's JSON sidecar (the
        # analogue of a project's .ck/state.json).
        state = json.loads(
            spaces.state_path("local").read_text(encoding="utf-8")
        )
        self.assertEqual(state["notes"]["1"], "debugging auth")
        self.assertEqual(
            spaces.SpaceManager("local").get_note(1), "debugging auth"
        )

    def test_local_note_updates_existing_note(self):
        self._seed("local", "first task")
        self._run(["local", "note", "1", "first note"])

        code, out = self._run(["local", "note", "1", "updated note"])

        self.assertEqual(code, 0)
        self.assertIn("* Note saved for [1]: updated note", out)
        state = json.loads(
            spaces.state_path("local").read_text(encoding="utf-8")
        )
        self.assertEqual(state["notes"]["1"], "updated note")

    def test_local_note_unknown_id_is_clean_error(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "note", "99", "x"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("No task with id 99", out)

    def test_local_note_without_text_is_usage_error(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "note", "1"])

        self.assertEqual(code, 2)
        self.assertIn("Usage: ck local note", out)

    def test_local_note_invalid_id_is_clean_error(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "note", "abc", "x"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("Invalid task ID", out)

    def test_remote_note_persists_to_state_sidecar(self):
        self._seed("remote", "provision VPS")

        code, out = self._run(["remote", "note", "1", "waiting for IP"])

        self.assertEqual(code, 0)
        self.assertIn("* Note saved for [1]: waiting for IP", out)
        state = json.loads(
            spaces.state_path("remote").read_text(encoding="utf-8")
        )
        self.assertEqual(state["notes"]["1"], "waiting for IP")
        self.assertEqual(
            spaces.SpaceManager("remote").get_note(1), "waiting for IP"
        )

    def test_remote_note_unknown_id_is_clean_error(self):
        self._seed("remote", "provision VPS")

        code, out = self._run(["remote", "note", "5", "x"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("No task with id 5", out)

    # ----------------- dashboard / ck -g reflection ----------------- #

    def test_local_done_reflected_in_dashboard(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "done", "1"])

        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()

        self.assertIn("LOCAL", out)
        self.assertIn("1/2 (50.0%)", out)

    def test_local_focus_reflected_in_dashboard(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "2"])

        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()

        self.assertIn("[2] [>]", out)
        self.assertIn("second task", out)

    def test_local_actions_reflected_in_global_dashboard_shorthand(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "2"])
        self._run(["local", "done", "1"])

        code, out = self._run(["-g"])

        self.assertEqual(code, 0)
        self.assertIn("SPACES (GLOBAL CONTEXTS)", out)
        self.assertIn("LOCAL", out)
        self.assertIn("[2] [>]", out)
        self.assertIn("second task", out)
        self.assertIn("1/2 (50.0%)", out)

    def test_remote_actions_reflected_in_dashboard(self):
        self._seed("remote", "provision VPS", "open firewall")
        self._run(["remote", "focus", "1"])
        self._run(["remote", "done", "2"])

        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()

        self.assertIn("REMOTE", out)
        self.assertIn("[1] [>]", out)
        self.assertIn("provision VPS", out)
        self.assertIn("1/2 (50.0%)", out)

    def test_local_and_remote_state_stay_isolated(self):
        self._seed("local", "local task")
        self._seed("remote", "remote task")
        self._run(["local", "note", "1", "local note"])

        # The local note never leaks into the remote sidecar.
        remote_state_path = spaces.state_path("remote")
        self.assertFalse(remote_state_path.exists())
        self.assertEqual(
            spaces.SpaceManager("remote").get_note(1), ""
        )


# --------------------------------------------------------------------------- #
# 6. Dynamic space routing (custom space files)
# --------------------------------------------------------------------------- #


class TestDynamicSpaceRouting(_IsolatedHome):
    """Any space file placed in ~/.config/ck/spaces/ routes uniformly."""

    def _run(self, argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def test_placed_space_file_is_discovered(self):
        # A space file "placed" by hand (no registration) is
        # discovered and routable with the full CLI interface.
        path = spaces.space_path("custom")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# custom\n## Current Sprint\n- [ ] pre-existing task\n"
            "\n## Completed\n",
            encoding="utf-8",
        )

        self.assertIn("custom", spaces.existing_space_names())

        code, out = self._run(["custom", "list"])
        self.assertEqual(code, 0)
        self.assertIn("[ ] 1. pre-existing task", out)

    def test_dynamic_space_full_command_surface(self):
        code, out = self._run(["custom", "add", "dynamic task"])
        self.assertEqual(code, 0)
        self.assertIn("[ok] Added task to custom space (id=1)", out)
        self.assertTrue(spaces.space_path("custom").is_file())

        code, out = self._run(["custom", "focus", "1"])
        self.assertEqual(code, 0)
        self.assertIn("-> Focused [1]: dynamic task", out)
        self.assertIn("- [>] dynamic task",
                      spaces.space_path("custom").read_text(encoding="utf-8"))

        code, out = self._run(["custom", "note", "1", "custom note"])
        self.assertEqual(code, 0)
        self.assertIn("* Note saved for [1]: custom note", out)
        state = json.loads(
            spaces.state_path("custom").read_text(encoding="utf-8")
        )
        self.assertEqual(state["notes"]["1"], "custom note")

        code, out = self._run(["custom", "done", "1"])
        self.assertEqual(code, 0)
        self.assertIn("[ok] Marked done: 1", out)
        self.assertIn("- [x] dynamic task",
                      spaces.space_path("custom").read_text(encoding="utf-8"))

        code, out = self._run(["custom", "list"])
        self.assertEqual(code, 0)
        self.assertIn("[custom] 1/1 done", out)

    def test_missing_space_file_is_clean_error(self):
        code, out = self._run(["nosuchspace", "list"])
        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("not found", out)

        code, out = self._run(["nosuchspace", "done", "1"])
        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)
        self.assertIn("not found", out)

    def test_add_creates_dynamic_space_lazily(self):
        self.assertFalse(spaces.space_path("fresh").exists())

        code, out = self._run(["fresh", "add", "brand new space"])

        self.assertEqual(code, 0)
        self.assertTrue(spaces.space_path("fresh").is_file())
        self.assertIn("- [ ] brand new space",
                      spaces.space_path("fresh").read_text(encoding="utf-8"))

    def test_dynamic_space_renders_in_dashboard(self):
        self._run(["custom", "add", "dynamic task"])
        self._run(["custom", "focus", "1"])

        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()

        self.assertIn("CUSTOM", out)
        self.assertIn("[1] [>]", out)
        self.assertIn("dynamic task", out)
        self.assertIn("spaces/custom.md", out)

    def test_unsafe_names_never_route(self):
        """Path separators / traversal attempts are rejected, not routed."""
        for argv in (["../evil", "add", "x"],
                     ["a/b", "add", "x"],
                     ["..", "add", "x"],
                     [".", "add", "x"],
                     ["", "add", "x"]):
            code, _ = self._run(argv)
            self.assertNotEqual(code, 0, f"unsafe name routed: {argv!r}")
        # Nothing was created outside the spaces directory.
        self.assertFalse((spaces.spaces_dir().parent / "evil.md").exists())

    def test_space_commands_work_without_project_root(self):
        """Space commands are global: no project root is required."""
        # The fake home has no .ck/ project anywhere — space
        # commands must still dispatch (the keeper is rootless).
        code, out = self._run(["local", "add", "no project needed"])
        self.assertEqual(code, 0, out)
        self.assertIn("no project needed",
                      spaces.space_path("local").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 7. SpaceManager unit surface (centralized engine)
# --------------------------------------------------------------------------- #


class TestSpaceManager(_IsolatedHome):
    """Direct unit tests for the centralized space engine."""

    def test_unknown_space_rejected_by_manager(self):
        with self.assertRaises(ValueError):
            spaces.SpaceManager("bogus")
        with self.assertRaises(ValueError):
            spaces.SpaceManager("../evil")

    def test_manager_roundtrip_add_focus_done_note(self):
        mgr = spaces.SpaceManager("local", create=True)
        first = mgr.add_task("first")
        second = mgr.add_task("second")
        self.assertEqual((first, second), (1, 2))

        result = mgr.focus(second)
        self.assertEqual(result["task_id"], 2)
        self.assertEqual(result["title"], "second")

        note = mgr.set_note(second, "mid-refactor")
        self.assertEqual(note["note"], "mid-refactor")
        self.assertEqual(mgr.get_note(second), "mid-refactor")

        transitioned = mgr.done("1")
        self.assertEqual(transitioned, [1])

        tl = mgr.load()
        self.assertEqual(tl.by_id(1).status.value, "done")
        self.assertEqual(tl.by_id(2).status.value, "focused")

    def test_manager_focus_demotes_and_resets(self):
        mgr = spaces.SpaceManager("local", create=True)
        mgr.add_task("first")
        mgr.add_task("second")
        mgr.focus(1)

        result = mgr.focus(2)
        self.assertEqual(result["demoted_id"], 1)

        reset = mgr.focus(0)
        self.assertEqual(reset["task_id"], 0)
        self.assertEqual(reset["demoted_id"], 2)
        self.assertIsNone(mgr.current_focus_id())

    def test_manager_done_rejects_unknown_and_invalid(self):
        mgr = spaces.SpaceManager("local", create=True)
        mgr.add_task("first")

        with self.assertRaises(KeyError):
            mgr.done("99")
        with self.assertRaises(ValueError):
            mgr.done("not-a-spec")

    def test_manager_note_rejects_unknown_and_empty(self):
        mgr = spaces.SpaceManager("local", create=True)
        mgr.add_task("first")

        with self.assertRaises(KeyError):
            mgr.set_note(99, "x")
        with self.assertRaises(ValueError):
            mgr.set_note(1, "   ")

    def test_manager_already_focused(self):
        mgr = spaces.SpaceManager("local", create=True)
        mgr.add_task("first")
        self.assertFalse(mgr.already_focused(1))
        mgr.focus(1)
        self.assertTrue(mgr.already_focused(1))
        self.assertFalse(mgr.already_focused(2))

    def test_existing_space_names_discovers_only_md_files(self):
        spaces_dir = spaces.spaces_dir()
        spaces_dir.mkdir(parents=True, exist_ok=True)
        (spaces_dir / "alpha.md").write_text(
            "# alpha\n## Current Sprint\n\n## Completed\n", encoding="utf-8")
        # Non-markdown files and unsafe stems are ignored.
        (spaces_dir / "beta.txt").write_text("noise", encoding="utf-8")

        names = spaces.existing_space_names()
        self.assertIn("alpha", names)
        self.assertNotIn("beta", names)

    def test_is_valid_space_covers_builtins_and_discovered(self):
        self.assertTrue(spaces.is_valid_space("local"))
        self.assertTrue(spaces.is_valid_space("remote"))
        self.assertFalse(spaces.is_valid_space("ghost"))
        spaces_dir = spaces.spaces_dir()
        spaces_dir.mkdir(parents=True, exist_ok=True)
        (spaces_dir / "ghost.md").write_text(
            "# ghost\n## Current Sprint\n\n## Completed\n", encoding="utf-8")
        self.assertTrue(spaces.is_valid_space("ghost"))


# --------------------------------------------------------------------------- #
# 8. Space list view alignment with the project `ck list` engine
# --------------------------------------------------------------------------- #


class TestSpaceListViewAlignment(_IsolatedHome):
    """`ck <space> list` uses the project list rendering engine.

    The space list body (everything below the ``[<space>]
    <done>/<total> done`` badge) is produced by the SAME
    ``cklib.core._render_tasks_listing`` engine as the
    project ``ck list``: ``##`` section headers, PLAN.md
    status markers, and — when notes are attached — an
    indented ``* Note: <text>`` line beneath each task.
    """

    def _run(self, argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def _seed(self, space: str, *titles: str) -> None:
        for title in titles:
            code, out = self._run([space, "add", title])
            self.assertEqual(code, 0, out)

    def test_local_list_matches_project_list_layout(self):
        """Space list body is the exact project-list layout."""
        self._seed("local", "open task", "focused task")
        self._run(["local", "focus", "2"])

        code, out = self._run(["local", "list"])

        self.assertEqual(code, 0)
        lines = out.splitlines()
        # Badge line first, then the project-list structure.
        self.assertEqual(lines[0], "[local] 0/2 done")
        self.assertEqual(lines[1], "## Current Sprint")
        self.assertEqual(lines[2], "[ ] 1. open task")
        self.assertEqual(lines[3], "[>] 2. focused task")

    def test_local_list_renders_notes_under_tasks(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "note", "1", "not yet started"])
        self._run(["local", "note", "2", "halfway through"])

        code, out = self._run(["local", "list"])

        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[2], "[ ] 1. first task")
        self.assertEqual(lines[3], "    * Note: not yet started")
        self.assertEqual(lines[4], "[ ] 2. second task")
        self.assertEqual(lines[5], "    * Note: halfway through")

    def test_local_list_note_attached_to_focused_task(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "2"])
        self._run(["local", "note", "2", "active note"])

        _, out = self._run(["local", "list"])

        lines = out.splitlines()
        self.assertIn("[>] 2. second task", lines)
        idx = lines.index("[>] 2. second task")
        self.assertEqual(lines[idx + 1], "    * Note: active note")

    def test_local_list_groups_completed_section(self):
        """Tasks under `## Completed` render in that section."""
        path = spaces.space_path("local")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# local\n## Current Sprint\n- [ ] pending\n"
            "\n## Completed\n- [x] finished\n",
            encoding="utf-8",
        )

        code, out = self._run(["local", "list"])

        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "[local] 1/2 done")
        self.assertEqual(lines[1], "## Current Sprint")
        self.assertEqual(lines[2], "[ ] 1. pending")
        self.assertEqual(lines[3], "## Completed")
        self.assertEqual(lines[4], "[x] 2. finished")

    def test_local_list_empty_space_shows_hint(self):
        code, out = self._run(["local", "list"])

        self.assertEqual(code, 0)
        self.assertEqual(
            out.rstrip("\n"),
            "[local] 0/0 done\n"
            "No tasks. Add one with `ck local add <text>`.",
        )

    def test_remote_list_matches_project_list_layout(self):
        self._seed("remote", "provision VPS", "open firewall")
        self._run(["remote", "focus", "1"])
        self._run(["remote", "note", "1", "waiting for IP"])

        code, out = self._run(["remote", "list"])

        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "[remote] 0/2 done")
        self.assertEqual(lines[1], "## Current Sprint")
        self.assertEqual(lines[2], "[>] 1. provision VPS")
        self.assertEqual(lines[3], "    * Note: waiting for IP")
        self.assertEqual(lines[4], "[ ] 2. open firewall")

    def test_space_list_body_is_engine_output(self):
        """The list body is byte-identical to the project engine."""
        self._seed("local", "alpha", "beta")
        self._run(["local", "focus", "1"])
        spaces.SpaceManager("local").set_note(2, "engine note")

        code, out = self._run(["local", "list"])
        self.assertEqual(code, 0)

        # Re-render the same plan through the project engine.
        from cklib.core import _render_tasks_listing
        tl = spaces.SpaceManager("local").load()
        body = _render_tasks_listing(
            tl, spaces.SpaceManager("local")._all_notes())

        lines = out.splitlines()
        self.assertEqual(lines[0], "[local] 0/2 done")
        self.assertEqual("\n".join(lines[1:]), body)

    def test_dynamic_space_list_uses_aligned_layout(self):
        self._seed("custom", "custom task")

        code, out = self._run(["custom", "list"])

        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "[custom] 0/1 done")
        self.assertEqual(lines[1], "## Current Sprint")
        self.assertEqual(lines[2], "[ ] 1. custom task")

    def test_done_tasks_keep_note_display(self):
        """A completed task's note still renders beneath it."""
        self._seed("local", "first task")
        self._run(["local", "note", "1", "done note"])
        self._run(["local", "done", "1"])

        _, out = self._run(["local", "list"])

        lines = out.splitlines()
        self.assertEqual(lines[0], "[local] 1/1 done")
        self.assertIn("[x] 1. first task", lines)
        idx = lines.index("[x] 1. first task")
        self.assertEqual(lines[idx + 1], "    * Note: done note")


# --------------------------------------------------------------------------- #
# 8. Command parity: st / status, edit, start alias
# --------------------------------------------------------------------------- #


class TestSpaceStatusEditStart(_IsolatedHome):
    """``ck <space> st`` / ``edit`` / ``start`` parity with the project CLI.

    - ``st`` (alias ``status``) renders the space through the SAME
      engine as the project ``ck st``
      (``cklib.core._render_local_status``), so a space status has
      the identical layout: header bar, progress line, CURRENT
      FOCUS with its process note, and the WORK CONTEXT sections;
    - ``edit`` resolves the editor with the project's standard
      resolution logic and opens the space's Markdown file;
    - ``start`` is an exact alias of ``focus``.
    """

    def _run(self, argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def _seed(self, space: str, *titles: str) -> None:
        for title in titles:
            code, out = self._run([space, "add", title])
            self.assertEqual(code, 0, out)

    def _tmpdir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def _fake_editor(self) -> tuple[Path, Path]:
        """Install a fake editor logging its single argument.

        Returns ``(editor, log)``. ``$VISUAL`` AND ``$EDITOR`` are
        both exported by the caller so editor resolution is
        deterministic regardless of the host environment (the
        project precedence checks ``$VISUAL`` before ``$EDITOR``).
        """
        d = self._tmpdir()
        editor = d / "fake-editor.sh"
        log = d / "editor.log"
        editor.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s" "$1" > "$CK_EDIT_LOG"\n',
            encoding="utf-8",
        )
        editor.chmod(0o755)
        return editor, log

    # ---- st / status ------------------------------------------------- #

    def test_local_st_layout_matches_project_st(self):
        self._seed("local", "first task", "second task", "third task")
        self._run(["local", "focus", "2"])
        self._run(["local", "note", "2", "halfway through"])
        self._run(["local", "done", "1"])

        code, out = self._run(["local", "st"])

        self.assertEqual(code, 0)
        lines = out.splitlines()
        bar = "=" * 61
        # 1) header bar + ` > SPACE [v<version>]`
        self.assertEqual(lines[0], bar)
        self.assertEqual(lines[1], f" > LOCAL [v{ckconfig.VERSION}]")
        # 2) progress line
        self.assertEqual(
            lines[2], " [%] Progress: 1/3 tasks done (33.3%)")
        # 3) current focus + its process note
        self.assertEqual(
            lines[4], " -> CURRENT FOCUS: [#2] second task")
        self.assertEqual(lines[5], "    * Note: halfway through")
        # 4) work context sections
        self.assertEqual(lines[7], " -> WORK CONTEXT:")
        self.assertEqual(lines[8], "    << Done:")
        self.assertEqual(lines[9], "       - [1] first task [x]")
        self.assertEqual(lines[10], "    [>] Focus:")
        self.assertEqual(lines[11], "       - [2] second task")
        self.assertEqual(lines[12], "       * Note: halfway through")
        self.assertEqual(lines[13], "    >> Upcoming:")
        self.assertEqual(lines[14], "       - [3] third task [ ]")
        self.assertEqual(lines[15], "    >> Backlog: 2 tasks remaining")
        self.assertEqual(lines[16], bar)

    def test_status_is_exact_alias_of_st(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "1"])
        self._run(["local", "note", "1", "active"])

        code_st, out_st = self._run(["local", "st"])
        code_status, out_status = self._run(["local", "status"])

        self.assertEqual(code_st, code_status)
        self.assertEqual(out_st, out_status)

    def test_local_st_without_focus_guides_to_start(self):
        self._seed("local", "alpha", "beta")

        code, out = self._run(["local", "st"])

        self.assertEqual(code, 0)
        self.assertIn("[!] No active focus set.", out)
        # The guidance names the SPACE spelling, not the project's.
        self.assertIn("Run 'ck local start <ID>'", out)
        self.assertIn("(e.g., 'ck local start 1') to set focus.", out)
        self.assertIn("    [>] Next:", out)
        self.assertIn("       - [1] alpha [ ]", out)

    def test_local_st_surfaces_paused_task_notes(self):
        """Noted, non-focused open tasks fill the paused ledger."""
        self._seed("local", "alpha", "beta", "gamma")
        self._run(["local", "focus", "2"])
        self._run(["local", "note", "1", "not started"])
        self._run(["local", "note", "2", "in progress"])
        self._run(["local", "note", "3", "waiting on deps"])

        code, out = self._run(["local", "st"])

        self.assertEqual(code, 0)
        self.assertIn("    Unfocused / Paused Context:", out)
        self.assertIn("       - [1] alpha", out)
        self.assertIn("         * Note: not started", out)
        self.assertIn("       - [3] gamma", out)
        self.assertIn("         * Note: waiting on deps", out)
        # The focused task's note is shown once, at the top — never
        # duplicated into the paused ledger.
        self.assertNotIn("         * Note: in progress", out)

    def test_st_takes_no_arguments(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "st", "extra"])

        self.assertEqual(code, 2)
        self.assertIn(
            "ERROR: `ck local st` takes no arguments.", out)

    def test_st_rejects_status_view_flags(self):
        """Space statuses take no view flags (no silent project st)."""
        self._seed("local", "first task")

        code, out = self._run(["local", "st", "-l"])

        self.assertEqual(code, 2)
        self.assertIn("Unknown flag", out)

    def test_remote_and_dynamic_spaces_render_status(self):
        self._seed("remote", "provision VPS")
        self._seed("custom", "custom task")
        self._run(["custom", "focus", "1"])

        code, out = self._run(["remote", "st"])
        self.assertEqual(code, 0)
        self.assertIn(f" > REMOTE [v{ckconfig.VERSION}]", out)
        self.assertIn("[%] Progress: 0/1 tasks done (0.0%)", out)

        code, out = self._run(["custom", "status"])
        self.assertEqual(code, 0)
        self.assertIn(f" > CUSTOM [v{ckconfig.VERSION}]", out)
        self.assertIn("-> CURRENT FOCUS: [#1] custom task", out)

    # ---- start alias -------------------------------------------------- #

    def test_local_start_focuses_task(self):
        self._seed("local", "first task", "second task")

        code, out = self._run(["local", "start", "2"])

        self.assertEqual(code, 0)
        self.assertEqual(out, "-> Focused [2]: second task\n")
        plan = spaces.space_path("local").read_text(encoding="utf-8")
        self.assertIn("- [>] second task", plan)

    def test_start_output_is_identical_to_focus(self):
        self._seed("local", "first task", "second task", "third task")
        _, focus_first = self._run(["local", "focus", "2"])
        _, focus_switch = self._run(["local", "focus", "3"])
        _, focus_reset = self._run(["local", "focus", "0"])
        self._run(["local", "focus", "0"])
        _, start_first = self._run(["local", "start", "2"])
        _, start_switch = self._run(["local", "start", "3"])
        _, start_reset = self._run(["local", "start", "0"])

        self.assertEqual(focus_first, start_first)
        self.assertEqual(focus_switch, start_switch)
        self.assertEqual(focus_reset, start_reset)
        # Both the demotion report and the reset report match too.
        self.assertEqual(
            focus_switch,
            "-> Focused [3]: third task\n"
            "[i] Task #2 second task lost focus (demoted to open).\n",
        )
        self.assertIn("[ok] Focus reset.", start_reset)

    def test_start_is_idempotent(self):
        self._seed("local", "first task")
        self._run(["local", "start", "1"])
        before = spaces.space_path("local").read_text(encoding="utf-8")

        code, out = self._run(["local", "start", "1"])

        self.assertEqual(code, 0)
        self.assertEqual(out, "Task #1 is already focused.\n")
        after = spaces.space_path("local").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_start_usage_and_invalid_ids(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "start"])
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck local start <ID>", out)

        code, out = self._run(["local", "start", "abc"])
        self.assertEqual(code, 2)
        self.assertIn("ERROR: Invalid task ID: 'abc'", out)

        code, out = self._run(["local", "start", "99"])
        self.assertEqual(code, 2)
        self.assertIn("No task with id 99", out)

    def test_start_completes_bare_done_target(self):
        """`start` moves focus, so bare `done` follows it."""
        self._seed("local", "first task", "second task")
        self._run(["local", "start", "2"])

        code, out = self._run(["local", "done"])

        self.assertEqual(code, 0)
        self.assertEqual(out, "[ok] Marked done: 2\n")
        plan = spaces.space_path("local").read_text(encoding="utf-8")
        self.assertIn("- [x] second task", plan)
        self.assertIn("- [ ] first task", plan)
        # Focus consumed by the completion — nothing left focused.
        self.assertNotIn("- [>]", plan)
        self.assertIsNone(
            spaces.SpaceManager("local").current_focus_id())

    # ---- edit --------------------------------------------------------- #

    def test_edit_opens_space_file_with_resolved_editor(self):
        self._seed("local", "first task")
        editor, log = self._fake_editor()

        with mock.patch.dict(os.environ, {
            "VISUAL": str(editor),
            "EDITOR": str(editor),
            "CK_EDIT_LOG": str(log),
        }):
            code, out = self._run(["local", "edit"])

        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(
            log.read_text(encoding="utf-8"),
            str(spaces.space_path("local")))

    def test_edit_prefers_project_ck_json_editor(self):
        """Editor resolution uses the project's standard precedence."""
        self._seed("local", "first task")
        editor, log = self._fake_editor()
        project = self._tmpdir()
        (project / ".ck").mkdir()
        (project / ".ck.json").write_text(
            json.dumps({"editor": str(editor)}) + "\n",
            encoding="utf-8",
        )
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)

        with mock.patch.dict(os.environ, {
            "VISUAL": "", "EDITOR": "", "CK_EDIT_LOG": str(log)}):
            code, _ = self._run(["local", "edit"])

        self.assertEqual(code, 0)
        self.assertEqual(
            log.read_text(encoding="utf-8"),
            str(spaces.space_path("local")))

    def test_edit_dynamic_space_target(self):
        self._seed("custom", "custom task")
        editor, log = self._fake_editor()

        with mock.patch.dict(os.environ, {
            "VISUAL": str(editor),
            "EDITOR": str(editor),
            "CK_EDIT_LOG": str(log),
        }):
            code, out = self._run(["custom", "edit"])

        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(
            log.read_text(encoding="utf-8"),
            str(spaces.space_path("custom")))

    def test_edit_in_dev_mode_targets_sandbox_copy(self):
        """Dev mode never hands the real space file to the editor."""
        self._seed("local", "first task")
        editor, log = self._fake_editor()
        # Activated AFTER seeding so the real (tmp-HOME) space file
        # exists and the redirect can be observed end to end.
        os.environ["CK_SANDBOX"] = "1"

        with mock.patch.dict(os.environ, {
            "VISUAL": str(editor),
            "EDITOR": str(editor),
            "CK_EDIT_LOG": str(log),
        }):
            code, out = self._run(["local", "edit"])

        self.assertEqual(code, 0)
        # Dev-mode warning banner precedes the (silent) editor run.
        self.assertIn("CK_SANDBOX is active", out)
        self.assertTrue(log.exists())
        invoked = Path(log.read_text(encoding="utf-8"))
        self.assertNotEqual(invoked, spaces.space_path("local"))
        self.assertTrue(cksandbox.is_within_sandbox(invoked))
        self.assertEqual(invoked.name, "local.md")

    def test_edit_takes_no_arguments(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "edit", "extra"])

        self.assertEqual(code, 2)
        self.assertIn(
            "ERROR: `ck local edit` takes no arguments.", out)

    def test_missing_space_errors_for_st_and_edit(self):
        for action in ("st", "status", "edit", "start"):
            code, out = self._run(["ghost", action])
            self.assertEqual(code, 2, out)
            self.assertIn(
                "ERROR: Space 'ghost' not found", out)
            self.assertIn("ck ghost add <text>", out)

    # ---- usage / help guidance ---------------------------------------- #

    def test_space_usage_hint_lists_every_action(self):
        code, out = self._run(["local"])

        self.assertEqual(code, 2)
        for token in ("add <text>", "list", "st", "status",
                      "done <ID|range>", "focus <ID>", "start <ID>",
                      "note <ID> <text>", "edit"):
            self.assertIn(token, out)

    def test_unknown_action_hint_lists_st_edit_start(self):
        code, out = self._run(["local", "frobnicate"])

        self.assertEqual(code, 2)
        self.assertIn("Unknown `ck local` action: 'frobnicate'", out)
        self.assertIn("st", out)
        self.assertIn("start <ID>", out)
        self.assertIn("edit", out)

    def test_help_documents_st_edit_and_start(self):
        from cklib.cli import HELP_TEXT

        self.assertIn("local st", HELP_TEXT)
        self.assertIn("status` is an exact alias", HELP_TEXT)
        self.assertIn("local start <ID>", HELP_TEXT)
        self.assertIn("local edit", HELP_TEXT)

    def test_status_ignores_error_exits(self):
        """Sanity: every happy-path command exits 0."""
        self._seed("local", "first task", "second task")
        self.assertEqual(self._run(["local", "st"])[0], 0)
        self.assertEqual(self._run(["local", "status"])[0], 0)
        self.assertEqual(self._run(["local", "start", "1"])[0], 0)
        self.assertEqual(
            spaces.SpaceManager("local").status(),
            self._run(["local", "st"])[1].rstrip("\n"))


if __name__ == "__main__":
    unittest.main()
