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
- Default global spaces (``local`` / ``remote``) auto-initialize at
  startup: missing built-ins are created from the canonical scaffold,
  existing space files are preserved byte-for-byte, custom spaces
  are never touched (v0.8.12).
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


def _grid_row_cells(rendered: str, label: str) -> list[str]:
    """The vertical lines of the grid row whose first cell is ``label``.

    A grid row is a run of consecutive ``│``-prefixed lines (the
    multi-line cell form), so the Progress cell is read from the SAME
    row rather than from a loose substring match — that is what makes
    the two-line layout assertable at all.
    """
    lines = rendered.splitlines()
    block: list[str] = []
    collecting = False
    for line in lines:
        if not line.startswith("│"):
            if collecting:
                break
            continue
        first = line.split("│")[1].strip()
        if not collecting:
            if first == label:
                collecting = True
                block.append(line)
        else:
            block.append(line)
    if not block:
        raise AssertionError(
            f"no grid row with first cell {label!r} in:\n{rendered}")
    cells_per_line = [len(l.split("│")) for l in block]
    if len(set(cells_per_line)) != 1:
        raise AssertionError(f"ragged grid row {block!r}")
    return [
        [l.split("│")[i].strip() for i in range(1, cells_per_line[0] - 1)]
        for l in block
    ]


def _assert_two_line_progress(case, rendered: str, label: str,
                              done: str, pct: str) -> None:
    """Progress renders as ``<done>/<total>`` + ``(<pct>%)`` on line 2.

    This is the SPACES/GLOBAL DASHBOARD parity contract: the ratio
    and the percentage occupy two SEPARATE lines of the same grid row,
    never one combined single-line cell.
    """
    cells = _grid_row_cells(rendered, label)
    case.assertEqual(len(cells), 2, f"expected a 2-line row: {cells}")
    case.assertTrue(cells[0][2].startswith(done),
                    f"line 1 progress cell: {cells[0][2]!r}")
    case.assertEqual(cells[1][2], pct,
                     f"line 2 progress cell: {cells[1][2]!r}")


class TestSpacesTable(_IsolatedHome):
    """LOCAL / REMOTE render through the shared table pipeline."""

    def test_spaces_render_in_table_not_list_dump(self):
        spaces.add_space_task("local", "install drivers")
        spaces.add_space_task("remote", "provision VPS")

        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()

        # Exactly one header row with the four standard columns.
        header = [l for l in out.splitlines() if l.startswith("│ Space")]
        self.assertEqual(len(header), 1, f"no spaces header in:\n{out}")
        cells = [c.strip() for c in header[0].strip("│").split("│")]
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
        # Progress cell: the TWO-LINE form "<done>/<total>" then
        # "(<pct>%)" — identical to the GLOBAL DASHBOARD table.
        _assert_two_line_progress(self, out, "LOCAL", "0/2", "(0.0%)")
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
        _assert_two_line_progress(self, out, "LOCAL", "1/2", "(50.0%)")

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
        _assert_two_line_progress(self, out, "LOCAL", "1/2", "(50.0%)")

    def test_remote_actions_reflected_in_dashboard(self):
        self._seed("remote", "provision VPS", "open firewall")
        self._run(["remote", "focus", "1"])
        self._run(["remote", "done", "2"])

        out = ContextKeeper(root=Path(tempfile.gettempdir())).dashboard()

        self.assertIn("REMOTE", out)
        self.assertIn("[1] [>]", out)
        self.assertIn("provision VPS", out)
        _assert_two_line_progress(self, out, "REMOTE", "1/2", "(50.0%)")

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

    def test_st_rejects_unknown_flags(self):
        """Only the documented view flags are accepted on `st`."""
        self._seed("local", "first task")

        code, out = self._run(["local", "st", "--all"])

        self.assertEqual(code, 2)
        self.assertIn("Unknown flag for `ck local st`: --all", out)

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
                      "[-l|-e|-g]", "done <ID|range>", "focus <ID>",
                      "start <ID>", "note <ID> <text>", "notes",
                      "edit", "dashboard [-v]"):
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


# --------------------------------------------------------------------------- #
# 9. Command parity: notes, st view flags, dashboard alias
# --------------------------------------------------------------------------- #


class TestSpaceNotesAndFlags(_IsolatedHome):
    """``ck <space> notes``, the ``st`` view flags and ``dashboard``.

    - ``notes`` renders through the SAME engine as the project
      ``ck notes`` (``cklib.core._render_notes_listing``);
    - ``st`` / ``status`` accept the same view flags as ``ck st``:
      ``-l`` (list), ``-e`` (edit), ``-g`` (global dashboard);
    - ``dashboard`` is a direct alias of ``ck dashboard``.
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
        """Install a fake editor logging its single argument."""
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

    # ---- notes -------------------------------------------------------- #

    def test_notes_lists_active_and_paused_notes(self):
        self._seed("local", "first task", "second task", "third task")
        self._run(["local", "focus", "2"])
        self._run(["local", "note", "2", "halfway through"])
        self._run(["local", "note", "3", "waiting on deps"])

        code, out = self._run(["local", "notes"])

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "[>] Active Focus:",
            "   - [2] second task",
            "     * Note: halfway through",
            "[!] Unfocused / Paused Context:",
            "   - [3] third task",
            "     * Note: waiting on deps",
        ])

    def test_notes_without_any_notes(self):
        self._seed("local", "first task")
        self._run(["local", "focus", "1"])

        code, out = self._run(["local", "notes"])

        self.assertEqual(code, 0)
        self.assertEqual(
            out.rstrip("\n"), "[i] No active process notes found.")

    def test_notes_on_empty_space(self):
        code, out = self._run(["remote", "notes"])

        self.assertEqual(code, 0)
        self.assertEqual(
            out.rstrip("\n"), "[i] No active process notes found.")

    def test_notes_hides_notes_of_completed_tasks(self):
        """Parity: only OPEN tasks carry live process notes."""
        self._seed("local", "first task")
        self._run(["local", "note", "1", "done note"])
        self._run(["local", "done", "1"])

        code, out = self._run(["local", "notes"])

        self.assertEqual(code, 0)
        self.assertEqual(
            out.rstrip("\n"), "[i] No active process notes found.")

    def test_notes_body_is_engine_output(self):
        """The listing is the project notes renderer's own output."""
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "1"])
        self._run(["local", "note", "1", "active"])
        self._run(["local", "note", "2", "waiting"])

        from cklib.core import _render_notes_listing
        mgr = spaces.SpaceManager("local")
        expected = _render_notes_listing(
            spaces._SpaceKeeperView(mgr), mgr.load())

        _, out = self._run(["local", "notes"])

        self.assertEqual(out.rstrip("\n"), expected)

    def test_remote_and_dynamic_space_notes(self):
        self._seed("remote", "provision VPS")
        self._seed("custom", "custom task")
        self._run(["remote", "note", "1", "waiting for IP"])
        self._run(["custom", "note", "1", "custom note"])

        code, out = self._run(["remote", "notes"])
        self.assertEqual(code, 0)
        self.assertIn("   - [1] provision VPS", out)
        self.assertIn("     * Note: waiting for IP", out)

        code, out = self._run(["custom", "notes"])
        self.assertEqual(code, 0)
        self.assertIn("   - [1] custom task", out)
        self.assertIn("     * Note: custom note", out)

    def test_notes_takes_no_arguments(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "notes", "extra"])

        self.assertEqual(code, 2)
        self.assertIn(
            "ERROR: `ck local notes` takes no arguments.", out)

    # ---- st view flags ------------------------------------------------ #

    def test_st_list_flag_equals_list_action(self):
        self._seed("local", "first task", "second task")
        self._run(["local", "focus", "1"])
        self._run(["local", "note", "1", "active note"])

        code_flag, out_flag = self._run(["local", "st", "-l"])
        code_long, out_long = self._run(["local", "status", "--list"])
        _, out_list = self._run(["local", "list"])

        self.assertEqual(code_flag, 0)
        self.assertEqual(out_flag, out_list)
        self.assertEqual(out_long, out_list)
        self.assertTrue(out_flag.startswith("[local] 0/2 done"))

    def test_st_edit_flag_opens_the_space_file(self):
        self._seed("local", "first task")
        editor, log = self._fake_editor()

        with mock.patch.dict(os.environ, {
            "VISUAL": str(editor),
            "EDITOR": str(editor),
            "CK_EDIT_LOG": str(log),
        }):
            code, out = self._run(["local", "st", "-e"])
            code_long, _ = self._run(["local", "status", "--edit"])

        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(code_long, 0)
        self.assertEqual(
            log.read_text(encoding="utf-8"),
            str(spaces.space_path("local")))

    def test_st_global_flag_prints_global_dashboard(self):
        self._seed("local", "first task")
        self._run(["local", "focus", "1"])

        code, out_flag = self._run(["local", "st", "-g"])
        _, out_long = self._run(["local", "status", "--global"])
        _, out_project = self._run(["dashboard"])

        self.assertEqual(code, 0)
        self.assertEqual(out_flag, out_long)
        self.assertEqual(out_flag, out_project)
        self.assertIn("SPACES (GLOBAL CONTEXTS)", out_flag)
        self.assertIn("LOCAL", out_flag)

    def test_dashboard_alias_equals_dashboard_command(self):
        self._seed("local", "first task")

        code, out = self._run(["local", "dashboard"])
        _, expected = self._run(["dashboard"])

        self.assertEqual(code, 0)
        self.assertEqual(out, expected)

    def test_dashboard_alias_verbose(self):
        self._seed("local", "first task")
        self._run(["local", "focus", "1"])

        code, out = self._run(["local", "dashboard", "-v"])
        _, expected = self._run(["dashboard", "-v"])

        self.assertEqual(code, 0)
        self.assertEqual(out, expected)
        self.assertIn("MY SPACES (", out)

    def test_dashboard_alias_takes_no_arguments(self):
        code, out = self._run(["local", "dashboard", "extra"])

        self.assertEqual(code, 2)
        self.assertIn(
            "ERROR: `ck local dashboard` takes no arguments.", out)

    def test_flags_are_rejected_on_flagless_actions(self):
        self._seed("local", "first task")
        for action in ("add", "list", "done", "focus", "start",
                       "note", "notes", "edit"):
            code, out = self._run(["local", action, "-l"])
            self.assertEqual(code, 2, f"{action}: {out}")
            self.assertIn(
                f"Unknown flag for `ck local {action}`: -l", out)

    def test_missing_space_errors_for_notes_and_dashboard(self):
        for action in ("notes", "dashboard", "st"):
            code, out = self._run(["ghost", action])
            self.assertEqual(code, 2, out)
            self.assertIn("ERROR: Space 'ghost' not found", out)


# --------------------------------------------------------------------------- #
# 10. Dashboard -v: per-space context triads
# --------------------------------------------------------------------------- #


class TestSpacesVerboseDashboard(_IsolatedHome):
    """``ck dashboard -v`` renders a styled CARD per space.

    Spaces get the SAME verbose treatment as registered projects:
    a rule-framed card whose body is the progress line plus the full
    PREV / FOCUS / NEXT context triad with process notes, painted by
    the shared triad renderer. Cards carry TOP and BOTTOM rules only —
    no vertical side rails.
    """

    RULE = "\u2500" * 61

    def _dashboard(self, *, verbose: bool = True) -> str:
        return ContextKeeper(
            root=Path(tempfile.gettempdir())).dashboard(verbose=verbose)

    def _spaces_section(self, out: str) -> str:
        return out[:out.index("MY PROJECTS")] if "MY PROJECTS" in out else out

    def test_verbose_renders_card_per_space(self):
        for title in ("first task", "second task", "third task"):
            spaces.add_space_task("local", title)
        spaces.SpaceManager("local").focus(2)
        spaces.SpaceManager("local").set_note(2, "halfway through")
        spaces.SpaceManager("local").set_note(3, "waiting on deps")
        spaces.SpaceManager("local").done("1")

        section = self._spaces_section(self._dashboard())

        # Heading + one CARD per space (both built-ins), framed by
        # ONE shared rule: two openings + one closing.
        self.assertIn("MY SPACES (2)", section)
        self.assertIn(" > LOCAL", section)
        self.assertIn(" > REMOTE", section)
        self.assertEqual(section.count(self.RULE), 3)
        # Path line, progress and the triad, inside the card.
        self.assertIn(f"@ {spaces.space_path('local')}", section)
        self.assertIn("[%] Progress: 1/3 tasks done (33.3%)", section)
        self.assertIn("-> Context:", section)
        self.assertIn("<< PREV", section)
        self.assertIn("[1] first task [x]", section)
        self.assertIn("[>] FOCUS", section)
        self.assertIn("[2] second task", section)
        self.assertIn("* Note: halfway through", section)
        self.assertIn(">> NEXT", section)
        self.assertIn("[3] third task [ ]", section)
        self.assertIn("[!] Unfocused / Paused Context:", section)
        self.assertIn("- [3] third task", section)
        self.assertIn("* Note: waiting on deps", section)

    def test_verbose_cards_have_no_side_rails(self):
        """Cards are delimited by rules only — no `|` borders."""
        for title in ("first task", "second task"):
            spaces.add_space_task("local", title)
        spaces.SpaceManager("local").focus(2)

        section = self._spaces_section(self._dashboard())

        for line in section.splitlines():
            self.assertNotIn("|", line)
            self.assertFalse(line.startswith("+"), line)

    def test_verbose_card_lines_carry_no_trailing_whitespace(self):
        for title in ("first task", "second task"):
            spaces.add_space_task("local", title)

        section = self._spaces_section(self._dashboard())

        for line in section.splitlines():
            self.assertEqual(line, line.rstrip(), repr(line))

    def test_verbose_card_body_is_indented_not_railed(self):
        spaces.add_space_task("local", "a task")

        section = self._spaces_section(self._dashboard())
        start = section.index(self.RULE)
        end = section.index(self.RULE, start + 1)
        body = section[start + len(self.RULE) + 1:end].splitlines()

        self.assertTrue(body)
        # Title first, then the (muted) path, then the triad body.
        self.assertEqual(body[0], " > LOCAL")
        self.assertTrue(body[1].startswith("   @ "))
        self.assertIn("    [%] Progress: 0/1 tasks done (0.0%)", body)

    def test_verbose_triad_vertical_order(self):
        for title in ("prev task", "focus task", "next task"):
            spaces.add_space_task("local", title)
        spaces.SpaceManager("local").done("1")
        spaces.SpaceManager("local").focus(2)

        section = self._spaces_section(self._dashboard())

        self.assertLess(section.index("<< PREV"),
                        section.index("[>] FOCUS"))
        self.assertLess(section.index("[>] FOCUS"),
                        section.index(">> NEXT"))
        self.assertLess(section.index("[1] prev task [x]"),
                        section.index("[2] focus task"))
        self.assertLess(section.index("[2] focus task"),
                        section.index("[3] next task [ ]"))

    def test_verbose_all_done_space_collapses_triad(self):
        spaces.add_space_task("local", "only task")
        spaces.SpaceManager("local").done("1")

        section = self._spaces_section(self._dashboard())

        self.assertIn("[%] Progress: 1/1 tasks done (100.0%)", section)
        self.assertIn("-> Context: (all tasks completed)", section)

    def test_verbose_empty_space_card(self):
        section = self._spaces_section(self._dashboard())

        self.assertIn("[%] Progress: 0/0 tasks done (0.0%)", section)
        self.assertIn("(none completed)", section)
        self.assertIn("[>] FOCUS", section)
        self.assertIn("(no focus selected)", section)
        self.assertIn("(no open tasks)", section)

    def test_verbose_has_no_duplicate_summary_table(self):
        """`-v` shows CARDS only — the compact table is not repeated."""
        spaces.add_space_task("local", "a task")

        out = self._dashboard(verbose=True)

        self.assertIn("MY SPACES", out)
        self.assertNotIn("SPACES (GLOBAL CONTEXTS)", out)
        self.assertNotIn("│ Space", out)

    def test_compact_dashboard_keeps_the_table(self):
        spaces.add_space_task("local", "a task")

        out = self._dashboard(verbose=False)

        self.assertIn("SPACES (GLOBAL CONTEXTS)", out)
        self.assertNotIn("MY SPACES", out)

    def test_verbose_dynamic_space_block_included(self):
        path = spaces.space_path("custom")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# custom\n## Current Sprint\n- [ ] custom task\n",
            encoding="utf-8",
        )

        section = self._spaces_section(self._dashboard())

        self.assertIn("MY SPACES (3)", section)
        self.assertIn(" > CUSTOM", section)

    def test_compact_dashboard_has_no_space_blocks(self):
        for title in ("first task", "second task"):
            spaces.add_space_task("local", title)

        out = self._dashboard(verbose=False)

        self.assertIn("SPACES (GLOBAL CONTEXTS)", out)
        self.assertNotIn("MY SPACES", out)
        self.assertNotIn("Unfocused / Paused Context:", out)

    def test_space_card_uses_project_triad_renderer(self):
        """Space cards and project cards share one renderer."""
        from cklib.core import _render_card_body, _render_verbose_triad

        for title in ("prev task", "focus task", "next task"):
            spaces.add_space_task("local", title)
        spaces.SpaceManager("local").done("1")
        spaces.SpaceManager("local").focus(2)
        spaces.SpaceManager("local").set_note(2, "halfway through")
        data = spaces.space_context("local")

        section = self._spaces_section(self._dashboard())

        # The space's card body is the shared renderer fed with the
        # space's own plan + note ledger.
        expected = _render_card_body(
            data["name"], data["path"], data["tl"], data["note"],
            data["paused"])
        for line in expected:
            self.assertIn(line, section)
        # And its body is exactly the shared triad output.
        body = _render_verbose_triad(data["tl"], data["note"], data["paused"])
        for line in body:
            self.assertIn(line, expected)


# --------------------------------------------------------------------------- #
# 12. Dynamic space discovery (regression: user spaces must be visible)
# --------------------------------------------------------------------------- #


class TestDynamicSpaceDiscovery(_IsolatedHome):
    """A user-created space is discovered WITHOUT any registration.

    Discovery keys off the space FILE, never off a hardcoded name list
    and never off the task count — a brand-new, still-empty space must
    show up in ``ck space list`` and in both dashboard views at once.
    """

    BAR = "\u2500" * 61

    def _run(self, argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def _dashboard(self, verbose: bool = False) -> str:
        return ContextKeeper(
            root=Path(tempfile.gettempdir())).dashboard(verbose=verbose)

    def test_created_space_is_listed_immediately_even_when_empty(self):
        """`ck space create` -> visible in `ck space list` right away."""
        self.assertNotIn("test-custom", spaces.existing_space_names())

        code, out = self._run(["space", "create", "test-custom"])
        self.assertEqual(code, 0, out)

        # Discovered dynamically (no registration step).
        self.assertIn("test-custom", spaces.existing_space_names())
        self.assertTrue(spaces.is_valid_space("test-custom"))

        code, listing = self._run(["space", "list"])
        self.assertEqual(code, 0, listing)
        self.assertIn("TEST-CUSTOM", listing)
        # Brand new and empty: 0/0, not skipped (two-line progress).
        _assert_two_line_progress(self, listing, "TEST-CUSTOM", "0/0",
                                  "(0.0%)")

    def test_created_space_appears_in_compact_dashboard(self):
        self._run(["space", "create", "test-custom"])

        out = self._dashboard(verbose=False)

        self.assertIn("SPACES (GLOBAL CONTEXTS)", out)
        self.assertIn("TEST-CUSTOM", out)
        self.assertIn("spaces/test-custom.md", out)

    def test_created_space_appears_in_verbose_dashboard(self):
        self._run(["space", "create", "test-custom"])

        out = self._dashboard(verbose=True)

        self.assertIn("MY SPACES", out)
        self.assertIn("> TEST-CUSTOM", out)

    def test_built_in_spaces_sort_before_custom_ones(self):
        self._run(["space", "create", "zeta"])
        self._run(["space", "create", "alpha"])

        code, listing = self._run(["space", "list"])

        self.assertEqual(code, 0, listing)
        self.assertLess(listing.index("LOCAL"), listing.index("ALPHA"))
        self.assertLess(listing.index("REMOTE"), listing.index("ALPHA"))
        # Custom spaces follow alphabetical order among themselves.
        self.assertLess(listing.index("ALPHA"), listing.index("ZETA"))

    def test_project_marker_stays_first(self):
        project = Path(tempfile.gettempdir()) / "disc-proj"
        (project / ".ck").mkdir(parents=True, exist_ok=True)
        (project / ".ck" / "PLAN.md").write_text(
            "# disc-proj\n## Current Sprint\n- [ ] a task\n",
            encoding="utf-8")
        self._run(["space", "create", "test-custom"])
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)

        code, listing = self._run(["space", "list"])

        self.assertEqual(code, 0, listing)
        rows = [i for i, line in enumerate(listing.splitlines())
                if line.startswith("│ ") and "│" in line[2:]]
        self.assertLess(rows[0], listing.index("LOCAL"))

    def test_dev_mode_space_is_discovered(self):
        """REGRESSION: a space created in a sandbox session is visible.

        Dev-mode writes land in ``.sandbox/``; a discovery pass that
        scanned only ``~/.config/ck/spaces/`` made the space invisible
        in every listing AND reported it as "not found" to routed
        actions.
        """
        os.environ["CK_SANDBOX"] = "1"
        self.addCleanup(os.environ.pop, "CK_SANDBOX", None)

        code, out = self._run(["space", "create", "test-custom"])
        self.assertEqual(code, 0, out)
        # It really did land in the sandbox, not in production.
        self.assertFalse(spaces.space_path("test-custom").exists())
        self.assertTrue(cksandbox.is_within_sandbox(
            spaces._write_path("test-custom")))

        # 1) discovered, 2) listed, 3) routable, 4) on the dashboard.
        self.assertIn("test-custom", spaces.existing_space_names())
        self.assertTrue(spaces.is_valid_space("test-custom"))
        code, listing = self._run(["space", "list"])
        self.assertEqual(code, 0, listing)
        self.assertIn("TEST-CUSTOM", listing)
        self.assertEqual(self._run(["test-custom", "list"])[0], 0)
        self.assertIn("TEST-CUSTOM", self._dashboard(verbose=False))
        self.assertIn("> TEST-CUSTOM", self._dashboard(verbose=True))

    def test_discovery_does_not_materialize_the_sandbox(self):
        """Probing is PURE: a read never creates the sandbox skeleton."""
        os.environ["CK_SANDBOX"] = "1"
        self.addCleanup(os.environ.pop, "CK_SANDBOX", None)

        self.assertEqual(spaces.existing_space_names(), [])
        self.assertFalse(spaces.is_valid_space("ghost"))

        sandboxed = cksandbox.sandbox_root() / "config" / "spaces"
        self.assertFalse(sandboxed.exists())

    def test_space_present_in_both_dirs_is_reported_once(self):
        self._run(["space", "create", "test-custom"])
        os.environ["CK_SANDBOX"] = "1"
        self.addCleanup(os.environ.pop, "CK_SANDBOX", None)
        self._run(["space", "create", "test-custom-sandbox"])

        names = spaces.existing_space_names()

        # A space living in both trees is discovered exactly once.
        self.assertEqual(names.count("test-custom"), 1)
        self.assertIn("test-custom-sandbox", names)
        self.assertEqual(names, sorted(set(names)))


# --------------------------------------------------------------------------- #
# 11. Space management: ck space list / create / rename / delete
# --------------------------------------------------------------------------- #


class _TtyBuffer(io.StringIO):
    """Capture buffer that reports itself as an interactive TTY.

    ``ck space delete`` only prompts when stdout is a terminal, so the
    captured output has to look like one. ``interactive`` is flipped
    to False to exercise the non-interactive refusal path.
    """

    interactive = True

    def isatty(self) -> bool:
        return self.interactive


class _TtyPrompt:
    """Context manager: an interactive stdin answering prompts.

    Records every prompt for assertion and returns ``answer``
    (``None`` simulates EOF / Ctrl+D). With ``interactive=False`` the
    terminal check fails, so the CLI must refuse without prompting.
    """

    def __init__(self, answer, *, interactive: bool = True) -> None:
        self.answer = answer
        self.interactive = interactive
        self.prompts: list[str] = []

    def __enter__(self) -> "_TtyPrompt":
        def fake_input(prompt: str = "") -> str:
            self.prompts.append(prompt)
            if self.answer is None:
                raise EOFError
            return self.answer

        self._patches = [
            mock.patch("sys.stdin.isatty",
                       return_value=self.interactive),
            mock.patch("builtins.input", side_effect=fake_input),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestSpaceManagement(_IsolatedHome):
    """``ck space <list|create|rename|delete>`` — the management layer."""

    def _run(self, argv) -> tuple[int, str]:
        self._buf = _TtyBuffer()
        with redirect_stdout(self._buf):
            code = main(argv)
        return code, self._buf.getvalue()

    def _tmpdir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    # ---- list --------------------------------------------------------- #

    def test_space_list_shows_all_spaces(self):
        spaces.add_space_task("local", "a local task")
        spaces.add_space_task("remote", "an infra task")

        code, out = self._run(["space", "list"])

        self.assertEqual(code, 0)
        self.assertIn("SPACES (GLOBAL CONTEXTS)", out)
        self.assertIn("LOCAL", out)
        self.assertIn("REMOTE", out)
        _assert_two_line_progress(self, out, "LOCAL", "0/1", "(0.0%)")
        # Same four columns as the dashboard spaces table.
        header = [l for l in out.splitlines() if l.startswith("│ Space")]
        self.assertEqual(len(header), 1, out)
        cells = [c.strip() for c in header[0].strip("│").split("│")]
        self.assertEqual(
            cells, ["Space", "Focus Task", "Progress", "Last Active"])

    def test_space_list_shows_current_project_marker(self):
        """The active PROJECT row is explicit — marked by ACCENT ONLY.

        The row is identified by bold/cyan highlighting of the name
        itself, never by a literal ``[PROJECT]`` prefix: a text tag
        widened the Space column and wrapped long project names.
        """
        project = Path(tempfile.gettempdir()) / "mgmt-proj"
        project.mkdir(exist_ok=True)
        (project / ".ck").mkdir(exist_ok=True)
        (project / ".ck" / "PLAN.md").write_text(
            "# mgmt-proj\n## Current Sprint\n- [>] focused task\n",
            encoding="utf-8")
        spaces.add_space_task("local", "a local task")
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)

        code, out = self._run(["space", "list"])

        self.assertEqual(code, 0)
        # NO text tag anywhere in the view.
        self.assertNotIn("[PROJECT]", out)
        # The project row comes FIRST — it is what un-prefixed
        # commands resolve against.
        self.assertLess(out.index("mgmt-proj"), out.index("LOCAL"))
        self.assertIn(
            "Un-prefixed commands (`ck st`, `ck list`, …) apply to "
            "the highlighted project (mgmt-proj)", out)

    def test_project_row_is_accented_not_labelled(self):
        """With colors on, the project name itself carries the accent."""
        project = Path(tempfile.gettempdir()) / "accent-proj"
        project.mkdir(exist_ok=True)
        (project / ".ck").mkdir(exist_ok=True)
        (project / ".ck" / "PLAN.md").write_text(
            "# accent-proj\n## Current Sprint\n- [ ] a task\n",
            encoding="utf-8")
        spaces.add_space_task("local", "a local task")
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)

        with mock.patch.dict(os.environ,
                             {"NO_COLOR": "", "FORCE_COLOR": "1"}):
            _, out = self._run(["space", "list"])

        from cklib.ui import strip_ansi, BOLD_MAGENTA
        row = next(l for l in out.splitlines()
                   if "accent-proj" in strip_ansi(l) and "│" in strip_ansi(l))
        # bold magenta wraps the name only — no tag, and the border
        # paint never leaks onto the content. This is the SAME colour
        # the dashboard tables use, so every view agrees.
        self.assertIn(f"{BOLD_MAGENTA}accent-proj\033[0m", row)
        self.assertNotIn("[PROJECT]", strip_ansi(row))
        self.assertNotIn("\033[36m", row)

    def test_space_list_without_project_notes_the_absence(self):
        spaces.add_space_task("local", "a local task")

        # A rootless keeper with no resolvable project context.
        from cklib.core import _render_space_manager_list
        out = _render_space_manager_list(None, None)

        self.assertNotIn("[PROJECT]", out)
        self.assertIn("No project in this directory", out)
        self.assertIn("LOCAL", out)

    def test_space_list_hides_project_row_when_unresolvable(self):
        """An uninitialized directory is NOT shown as a project."""
        empty = self._tmpdir()
        cwd = os.getcwd()
        os.chdir(empty)
        self.addCleanup(os.chdir, cwd)
        spaces.add_space_task("local", "a local task")

        code, out = self._run(["space", "list"])

        self.assertEqual(code, 0)
        self.assertNotIn("[PROJECT]", out)
        self.assertIn("No project in this directory", out)
        self.assertIn("LOCAL", out)

    def test_space_list_alias_ls(self):
        code_list, out_list = self._run(["space", "list"])
        code_ls, out_ls = self._run(["space", "ls"])

        self.assertEqual(code_ls, code_list)
        self.assertEqual(out_ls, out_list)

    def test_space_list_rejects_arguments(self):
        code, out = self._run(["space", "list", "extra"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR: `ck space list` takes no arguments.", out)

    # ---- create -------------------------------------------------------- #

    def test_create_scaffolds_default_template(self):
        code, out = self._run(["space", "create", "work"])

        self.assertEqual(code, 0)
        path = spaces.space_path("work")
        self.assertTrue(path.is_file())
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "# work\n\n## Current Sprint\n\n## Completed\n")
        self.assertIn("Created space 'work'", out)
        # The new space is immediately routable.
        self.assertIn("WORK", self._run(["space", "list"])[1])
        self.assertEqual(self._run(["work", "add", "first task"])[0], 0)

    def test_create_alias_new(self):
        code, _ = self._run(["space", "new", "aliased"])
        self.assertEqual(code, 0)
        self.assertTrue(spaces.space_path("aliased").is_file())

    def test_create_rejects_invalid_names(self):
        for name in ("with space", "sub/dir", "..", "..hidden",
                     "star*", "semi;colon", ""):
            with self.subTest(name=name):
                code, out = self._run(["space", "create", name])
                self.assertEqual(code, 2, out)
                self.assertIn("ERROR", out)
        # None of the REJECTED names materialised a file. (The dir
        # legitimately holds the startup auto-initialized defaults
        # local.md / remote.md since v0.8.12.)
        leftovers = [
            p for p in spaces.spaces_dir().glob("*.md")
            if p.stem not in ("local", "remote")
        ]
        self.assertEqual(leftovers, [],
                         f"rejected names created files: {leftovers}")

    def test_create_rejects_reserved_names(self):
        for name in sorted(spaces.RESERVED_SPACE_NAMES):
            with self.subTest(name=name):
                code, out = self._run(["space", "create", name])
                self.assertEqual(code, 2, out)
                self.assertIn("Reserved names", out)
        # None of them materialised a file.
        for name in spaces.RESERVED_SPACE_NAMES:
            if name in ("local", "remote"):
                continue
            self.assertFalse(spaces.space_path(name).exists())

    def test_create_rejects_redundant_md_suffix(self):
        code, out = self._run(["space", "create", "work.md"])

        self.assertEqual(code, 2)
        self.assertIn("added automatically", out)
        self.assertFalse(spaces.space_path("work.md").exists())

    def test_create_rejects_duplicate(self):
        self._run(["space", "create", "work"])

        code, out = self._run(["space", "create", "work"])

        self.assertEqual(code, 2)
        self.assertIn("already exists", out)

    def test_create_requires_a_name(self):
        code, out = self._run(["space", "create"])

        self.assertEqual(code, 2)
        self.assertIn("Usage: ck space create <name>", out)

    def test_create_rejects_multiple_names(self):
        code, out = self._run(["space", "create", "one", "two"])

        self.assertEqual(code, 2)
        self.assertIn("exactly one name", out)
        self.assertFalse(spaces.space_path("one").exists())

    # ---- rename -------------------------------------------------------- #

    def test_rename_moves_the_space_file(self):
        self._run(["space", "create", "work"])
        self._run(["work", "add", "a task"])

        code, out = self._run(["space", "rename", "work", "office"])

        self.assertEqual(code, 0)
        self.assertFalse(spaces.space_path("work").exists())
        self.assertTrue(spaces.space_path("office").is_file())
        # Task content survives the rename.
        self.assertIn("a task",
                      spaces.space_path("office").read_text(encoding="utf-8"))
        self.assertIn("Renamed space 'work' -> 'office'", out)
        # The new name is routable, the old one is not.
        self.assertEqual(self._run(["office", "list"])[0], 0)
        code, out = self._run(["work", "list"])
        self.assertEqual(code, 2)
        self.assertIn("not found", out)

    def test_rename_carries_the_sidecar(self):
        self._run(["space", "create", "work"])
        self._run(["work", "add", "a task"])
        self._run(["work", "note", "1", "process note"])
        self.assertTrue(spaces.state_path("work").is_file())

        code, out = self._run(["space", "rename", "work", "office"])

        self.assertEqual(code, 0)
        self.assertFalse(spaces.state_path("work").exists())
        self.assertTrue(spaces.state_path("office").is_file())
        # The note is still bound to the task after the rename.
        self.assertEqual(
            spaces.SpaceManager("office").get_note(1), "process note")
        self.assertIn("Process notes (sidecar) carried along.", out)

    def test_rename_works_without_a_sidecar(self):
        self._run(["space", "create", "work"])

        code, out = self._run(["space", "rename", "work", "office"])

        self.assertEqual(code, 0)
        self.assertNotIn("carried along", out)
        self.assertFalse(spaces.state_path("office").exists())

    def test_rename_alias_mv(self):
        self._run(["space", "create", "work"])

        code, _ = self._run(["space", "mv", "work", "office"])

        self.assertEqual(code, 0)
        self.assertTrue(spaces.space_path("office").is_file())

    def test_rename_requires_existing_source(self):
        code, out = self._run(["space", "rename", "ghost", "office"])

        self.assertEqual(code, 2)
        self.assertIn("not found", out)
        self.assertIn("ck space create ghost", out)
        self.assertFalse(spaces.space_path("office").exists())

    def test_rename_rejects_taken_or_invalid_target(self):
        self._run(["space", "create", "work"])
        self._run(["space", "create", "other"])

        code, out = self._run(["space", "rename", "work", "other"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", out)

        code, out = self._run(["space", "rename", "work", "bad name"])
        self.assertEqual(code, 2)
        self.assertIn("Invalid space name", out)

        # The source is untouched by either failed rename.
        self.assertTrue(spaces.space_path("work").is_file())

    def test_rename_rejects_reserved_target(self):
        self._run(["space", "create", "work"])

        code, out = self._run(["space", "rename", "work", "list"])

        self.assertEqual(code, 2)
        self.assertIn("Reserved names", out)

    def test_rename_requires_two_arguments(self):
        code, out = self._run(["space", "rename", "only"])

        self.assertEqual(code, 2)
        self.assertIn("Usage: ck space rename <old> <new>", out)

    # ---- delete -------------------------------------------------------- #

    def _answer_prompt(self, answer, *, interactive: bool = True):
        """Present an interactive terminal answering with ``answer``."""
        return _TtyPrompt(answer, interactive=interactive)

    def test_delete_prompts_and_cancels_on_no(self):
        self._run(["space", "create", "work"])

        with self._answer_prompt("n") as prompt:
            code, out = self._run(["space", "delete", "work"])

        self.assertEqual(code, 2)
        self.assertEqual(
            prompt.prompts,
            ["Are you sure you want to permanently delete space "
             "'work' and its process notes? [y/N]: "])
        self.assertIn("[i] Aborted.", out)
        self.assertTrue(spaces.space_path("work").is_file())

    def test_delete_proceeds_on_yes(self):
        self._run(["space", "create", "work"])

        with self._answer_prompt("y"):
            code, out = self._run(["space", "delete", "work"])

        self.assertEqual(code, 0)
        self.assertFalse(spaces.space_path("work").exists())
        self.assertIn("Deleted space 'work'", out)

    def test_delete_prompt_aborts_on_eof(self):
        self._run(["space", "create", "work"])

        with self._answer_prompt(None):
            code, out = self._run(["space", "delete", "work"])

        self.assertEqual(code, 2)
        self.assertIn("[i] Aborted.", out)
        self.assertTrue(spaces.space_path("work").is_file())

    def test_delete_refuses_non_interactive_without_yes(self):
        """Piped / non-TTY stdin must NEVER delete silently."""
        self._run(["space", "create", "work"])
        self._buf.interactive = False

        with self._answer_prompt("y", interactive=False) as prompt:
            code, out = self._run(["space", "delete", "work"])

        self.assertEqual(code, 2)
        # No prompt was even shown — the CLI refused up front.
        self.assertEqual(prompt.prompts, [])
        self.assertIn("non-interactive terminal", out)
        self.assertIn("Pass -y", out)
        self.assertTrue(spaces.space_path("work").is_file())

    def test_delete_reports_sidecar_removal(self):
        self._run(["space", "create", "work"])
        self._run(["work", "add", "a task"])
        self._run(["work", "note", "1", "process note"])
        self.assertTrue(spaces.state_path("work").is_file())

        code, out = self._run(["space", "delete", "work", "-y"])

        self.assertEqual(code, 0)
        self.assertFalse(spaces.space_path("work").exists())
        self.assertFalse(spaces.state_path("work").exists())
        self.assertIn("Process notes (sidecar) deleted too.", out)

    def test_delete_yes_flag_bypasses_the_prompt(self):
        self._run(["space", "create", "work"])

        for flag in ("-y", "--yes", "--force"):
            with self.subTest(flag=flag):
                self._run(["space", "create", "work"])
                with mock.patch("builtins.input") as inp:
                    code, out = self._run(
                        ["space", "delete", "work", flag])
                self.assertEqual(code, 0, out)
                inp.assert_not_called()
                self.assertFalse(spaces.space_path("work").exists())

    def test_delete_refuses_builtin_spaces(self):
        for name in ("local", "remote"):
            with self.subTest(name=name):
                code, out = self._run(["space", "delete", name, "-y"])
                self.assertEqual(code, 2)
                self.assertIn("built-in space", out)

    def test_delete_refuses_unconfirmed_api_call(self):
        """The library refuses to delete without explicit confirmation."""
        self._run(["space", "create", "work"])

        with self.assertRaises(ValueError):
            spaces.delete_space("work")

        self.assertTrue(spaces.space_path("work").is_file())

    def test_delete_requires_existing_space(self):
        code, out = self._run(["space", "delete", "ghost", "-y"])

        # "Not found" is its own outcome: exit 1, distinct from the
        # exit 2 used for usage errors.
        self.assertEqual(code, 1)
        self.assertIn("ERROR: Space 'ghost' not found", out)

    def test_delete_missing_space_errors_before_prompting(self):
        """REGRESSION: existence is checked BEFORE the prompt.

        A space that does not exist can never be deleted, so the CLI
        must report the error immediately — never ask the user to
        confirm a no-op (and never block a piped script on a prompt
        whose only sensible answer is "yes").
        """
        with self._answer_prompt("y") as prompt:
            code, out = self._run(["space", "delete", "ghost"])

        self.assertEqual(code, 1)
        # No prompt was ever shown, not even on a real TTY.
        self.assertEqual(prompt.prompts, [])
        self.assertIn("ERROR: Space 'ghost' not found", out)
        self.assertNotIn("Are you sure", out)

    def test_delete_missing_space_does_not_read_stdin(self):
        """Non-interactive: the error must not depend on stdin."""
        with self._answer_prompt("y", interactive=False) as prompt:
            code, out = self._run(["space", "delete", "ghost"])

        self.assertEqual(code, 1)
        self.assertEqual(prompt.prompts, [])
        self.assertIn("ERROR: Space 'ghost' not found", out)
        self.assertNotIn("non-interactive", out)

    def test_delete_builtin_errors_before_prompting(self):
        """A protected built-in is reported without asking anything."""
        for name in ("local", "remote"):
            with self.subTest(name=name):
                with self._answer_prompt("y") as prompt:
                    code, out = self._run(["space", "delete", name])
                self.assertEqual(code, 2)
                self.assertEqual(prompt.prompts, [])
                self.assertIn("built-in space", out)

    def test_delete_invalid_name_errors_before_prompting(self):
        with self._answer_prompt("y") as prompt:
            code, out = self._run(["space", "delete", "bad name"])

        self.assertEqual(code, 2)
        self.assertEqual(prompt.prompts, [])
        self.assertIn("ERROR", out)

    def test_space_not_found_is_a_distinct_error_type(self):
        """`not found` is its own domain error, still a ValueError."""
        self.assertTrue(
            issubclass(spaces.SpaceNotFoundError, ValueError))
        with self.assertRaises(spaces.SpaceNotFoundError):
            spaces.validate_deletable_space("ghost")
        # Every OTHER guard raises a plain ValueError, so callers can
        # tell "missing" apart from "invalid".
        with self.assertRaises(ValueError) as ctx:
            spaces.validate_deletable_space("local")
        self.assertNotIsInstance(
            ctx.exception, spaces.SpaceNotFoundError)

    def test_validate_deletable_space_is_the_preflight_contract(self):
        """The pre-flight helper is importable and self-contained."""
        self.assertFalse(spaces.space_path("ghost").exists())
        with self.assertRaises(ValueError) as ctx:
            spaces.validate_deletable_space("ghost")
        self.assertIn("not found", str(ctx.exception))
        # Built-ins are refused by the same helper.
        with self.assertRaises(ValueError):
            spaces.validate_deletable_space("local")

        self._run(["space", "create", "work"])
        self.assertEqual(
            spaces.validate_deletable_space("work"),
            spaces.space_path("work"))

    def test_delete_space_validates_target_before_confirmation(self):
        """Library order: a missing space reports THAT, not 'unconfirmed'."""
        with self.assertRaises(ValueError) as ctx:
            spaces.delete_space("ghost")

        self.assertIn("not found", str(ctx.exception))

    def test_delete_rejects_invalid_name(self):
        code, out = self._run(["space", "delete", "bad name", "-y"])

        self.assertEqual(code, 2)
        self.assertIn("ERROR", out)

    def test_delete_requires_a_name(self):
        code, out = self._run(["space", "delete"])

        self.assertEqual(code, 2)
        self.assertIn("Usage: ck space delete <name> [-y]", out)

    def test_delete_has_no_short_alias(self):
        """Deliberately no `rm`/`del` alias — the verb is spelled out."""
        self._run(["space", "create", "work"])

        for action in ("rm", "del", "remove", "destroy"):
            with self.subTest(action=action):
                code, out = self._run(["space", action, "work"])
                self.assertEqual(code, 2, out)
                self.assertIn("Unknown `ck space` action", out)
                code, out = self._run(["space", action, "work", "-y"])
                self.assertEqual(code, 2, out)
                self.assertIn("Unknown flag for `ck space "
                              f"{action}`: -y", out)
        # The space survived every rejected alias.
        self.assertTrue(spaces.space_path("work").is_file())

    # ---- shared -------------------------------------------------------- #

    def test_bare_space_shows_usage(self):
        code, out = self._run(["space"])

        self.assertEqual(code, 2)
        for token in ("list", "create <name>", "rename <old> <new>",
                      "delete <name>"):
            self.assertIn(token, out)

    def test_unknown_space_action_reports_error(self):
        code, out = self._run(["space", "frobnicate"])

        self.assertEqual(code, 2)
        self.assertIn("Unknown `ck space` action: 'frobnicate'", out)
        self.assertIn("Usage: ck space <", out)

    def test_only_delete_accepts_flags(self):
        self._run(["space", "create", "work"])

        for action in (["list"], ["create", "other"], ["rename"]):
            with self.subTest(action=action):
                code, out = self._run(["space", *action, "-y"])
                self.assertEqual(code, 2, out)
                self.assertIn("Unknown flag for `ck space", out)

    def test_help_documents_space_management(self):
        from cklib.cli import HELP_TEXT

        self.assertIn("Space Management", HELP_TEXT)
        self.assertIn("space list", HELP_TEXT)
        self.assertIn("space create <name>", HELP_TEXT)
        self.assertIn("space rename <old> <new>", HELP_TEXT)
        self.assertIn("space delete <name>", HELP_TEXT)

    def test_management_works_from_any_directory(self):
        """`ck space …` is global — no project root required."""
        elsewhere = self._tmpdir()
        cwd = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, cwd)

        code, out = self._run(["space", "create", "global-ops"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self._run(["space", "list"])[0], 0)

    def test_dev_mode_management_never_touches_production(self):
        """In a sandbox session the whole lifecycle stays in .sandbox/."""
        self._run(["space", "create", "work"])
        production = spaces.space_path("work")
        os.environ["CK_SANDBOX"] = "1"
        self.addCleanup(os.environ.pop, "CK_SANDBOX", None)

        code, _ = self._run(["space", "create", "sandboxed"])
        self.assertEqual(code, 0)
        sandboxed = spaces.space_path("sandboxed")
        self.assertFalse(sandboxed.exists())       # production untouched
        self.assertTrue(cksandbox.is_within_sandbox(
            spaces._write_path("sandboxed")))

        # A sandboxed space is NOT free: create must not overwrite it.
        code, out = self._run(["space", "create", "sandboxed"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", out)

        # Rename + delete both stay inside the sandbox too.
        code, _ = self._run(["space", "rename", "sandboxed", "moved"])
        self.assertEqual(code, 0)
        self.assertFalse(spaces.space_path("moved").exists())
        code, _ = self._run(["space", "delete", "moved", "-y"])
        self.assertEqual(code, 0)
        # The production space is untouched throughout.
        self.assertTrue(production.is_file())
        self.assertIn("a", self._run(["work", "list"])[1])


# --------------------------------------------------------------------------- #
# 8. Default-space auto-initialization at startup (v0.8.12)
# --------------------------------------------------------------------------- #


class TestDefaultSpacesAutoInit(_IsolatedHome):
    """Startup auto-init creates missing built-ins, preserves existing.

    ``ensure_default_spaces`` must be idempotent, strictly
    non-destructive (an existing ``local.md`` / ``remote.md`` is kept
    byte-for-byte, sidecar included), limited to the built-in names,
    silent, and it must never raise.
    """

    def test_missing_defaults_created_from_scaffold(self):
        self.assertEqual(spaces.ensure_default_spaces(),
                         ["local", "remote"])
        for name in ("local", "remote"):
            path = spaces.space_path(name)
            self.assertTrue(path.is_file())
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                spaces._EMPTY_SPACE.format(space=name),
            )

    def test_second_run_is_a_noop(self):
        self.assertEqual(spaces.ensure_default_spaces(),
                         ["local", "remote"])
        stamps = {name: spaces.space_path(name).stat().st_mtime_ns
                  for name in ("local", "remote")}
        self.assertEqual(spaces.ensure_default_spaces(), [])
        for name in ("local", "remote"):
            self.assertEqual(spaces.space_path(name).stat().st_mtime_ns,
                             stamps[name],
                             f"{name}.md was rewritten on re-run")

    def test_existing_space_preserved_byte_for_byte(self):
        """A populated space file AND its note sidecar survive."""
        original = (
            "# local\n\n## Current Sprint\n- [>] 7. precious task\n\n"
            "## Completed\n"
        )
        spaces.space_path("local").parent.mkdir(
            parents=True, exist_ok=True)
        spaces.space_path("local").write_text(original,
                                               encoding="utf-8")
        sidecar = spaces.state_path("local")
        sidecar.write_text('{"notes": {"7": "keep me"}}',
                           encoding="utf-8")

        created = spaces.ensure_default_spaces()

        # Only the MISSING default is created; local is untouched.
        self.assertEqual(created, ["remote"])
        self.assertEqual(spaces.space_path("local").read_text(
            encoding="utf-8"), original)
        self.assertEqual(sidecar.read_text(encoding="utf-8"),
                         '{"notes": {"7": "keep me"}}')
        self.assertTrue(spaces.space_path("remote").is_file())

    def test_custom_spaces_never_created_or_touched(self):
        alpha = spaces.space_path("alpha")
        alpha.parent.mkdir(parents=True, exist_ok=True)
        alpha.write_text("# alpha\n\n- [ ] mine\n", encoding="utf-8")

        created = spaces.ensure_default_spaces()

        self.assertEqual(created, ["local", "remote"])
        self.assertEqual(alpha.read_text(encoding="utf-8"),
                         "# alpha\n\n- [ ] mine\n")

    def test_under_lock_reprobe_drops_race_loser(self):
        """The in-lock re-probe never clobbers a concurrent first write."""
        answers = {
            "local": iter([False, True]),    # 2nd probe: file appeared
            "remote": iter([False, False]),
        }
        with mock.patch.object(
                spaces, "_space_exists_anywhere",
                side_effect=lambda name: next(answers[name])):
            created = spaces.ensure_default_spaces()

        self.assertEqual(created, ["remote"])
        self.assertFalse(spaces.space_path("local").exists())
        self.assertTrue(spaces.space_path("remote").is_file())

    def test_silent_and_never_raises_on_broken_config_tree(self):
        buf = io.StringIO()
        with mock.patch.object(spaces, "_write_path",
                               side_effect=OSError("read-only fs")):
            with redirect_stdout(buf):
                self.assertEqual(spaces.ensure_default_spaces(), [])
        self.assertEqual(buf.getvalue(), "")

    def test_cli_startup_initializes_defaults(self):
        """Any command materializes the defaults — from any cwd."""
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        cwd = os.getcwd()
        os.chdir(elsewhere.name)
        self.addCleanup(os.chdir, cwd)

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["space", "list"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertTrue(spaces.space_path("local").is_file())
        self.assertTrue(spaces.space_path("remote").is_file())
        # The hook is transparent: no auto-init chatter on stdout.
        self.assertNotIn("auto-init", buf.getvalue().lower())

    def test_cli_startup_preserves_existing_space(self):
        spaces.add_space_task("remote", "prod server pinned")
        before = spaces.space_path("remote").read_text(encoding="utf-8")

        with redirect_stdout(io.StringIO()):
            code = main(["dashboard"])

        self.assertEqual(code, 0)
        self.assertEqual(spaces.space_path("remote").read_text(
            encoding="utf-8"), before)
        self.assertTrue(spaces.space_path("local").is_file())

    def test_dev_mode_session_skips_auto_init(self):
        """A sandbox session never materializes global config."""
        os.environ["CK_SANDBOX"] = "1"
        self.addCleanup(os.environ.pop, "CK_SANDBOX", None)
        from cklib.cli import _ensure_default_spaces

        buf = io.StringIO()
        with redirect_stdout(buf):
            _ensure_default_spaces()
        self.assertEqual(buf.getvalue(), "")
        self.assertFalse(spaces.spaces_dir().exists())


if __name__ == "__main__":
    unittest.main()
