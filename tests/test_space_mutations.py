"""Tests for the unified space sequence engine (v0.8.15).

Coverage:

- ``cklib.sequence``: pure move/swap/normalize/remove primitives shared
  by task and space ordering (stable semantics, 1-based positions,
  range/unknown errors, inputs never mutated).
- Space order persistence in the global ``state.json`` under
  ``spaces_order``: defaults (``project`` + built-ins + custom), read
  without side effects, unknown keys preserved, stale names and
  duplicates reconciled.
- ``ck space move`` / ``swap`` / ``reorder``: domain behaviour and CLI
  output/exit codes, including the virtual ``project`` space.
- ``ck space delete``: ``-f`` / ``--force`` and ``-y`` bypasses, the
  interactive ``[y/N]`` prompt, non-interactive refusal, removal from
  the persisted order, and protection of ``project`` / built-ins.
- Strict separation: space mutations never touch ``PLAN.md`` tasks;
  ``ck delete`` (task deletion) is a different command entirely.
- ``ck space list`` and the dashboard table follow the persisted order.
- DEV MODE: the order write lands in the sandbox state copy, never in
  the real ``~/.config/ck/state.json``.
"""

from __future__ import annotations

# Filesystem isolation safety net (see tests/__init__).
import tests  # noqa: F401

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import sandbox as cksandbox
from cklib import sequence, spaces
from cklib.cli import main
from cklib.core import ContextKeeper


def _run_main(argv, *, tty=False, answer=None):
    """Run the CLI in-process; return (rc, stdout, stderr)."""
    patches = [
        mock.patch("sys.stdin.isatty", return_value=tty),
        mock.patch("sys.stdout.isatty", return_value=tty),
    ]
    if answer is not None:
        patches.append(mock.patch("builtins.input", return_value=answer))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        for patcher in patches:
            patcher.start()
        try:
            rc = main(argv)
        finally:
            for patcher in reversed(patches):
                patcher.stop()
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Pure sequence engine
# ---------------------------------------------------------------------------


class TestSequenceEngine(unittest.TestCase):

    def test_move_item_is_stable_and_one_based(self):
        items = ["a", "b", "c", "d"]
        self.assertEqual(sequence.move_item(items, "d", 1),
                         ["d", "a", "b", "c"])
        self.assertEqual(sequence.move_item(items, "a", 3),
                         ["b", "c", "a", "d"])
        # Input untouched (pure).
        self.assertEqual(items, ["a", "b", "c", "d"])

    def test_move_item_errors(self):
        with self.assertRaises(KeyError):
            sequence.move_item(["a"], "z", 1)
        with self.assertRaises(ValueError):
            sequence.move_item(["a", "b"], "a", 0)
        with self.assertRaises(ValueError):
            sequence.move_item(["a", "b"], "a", 3)

    def test_swap_items(self):
        self.assertEqual(sequence.swap_items(["a", "b", "c"], "a", "c"),
                         ["c", "b", "a"])
        self.assertEqual(sequence.swap_items(["a", "b"], "a", "a"),
                         ["a", "b"])
        with self.assertRaises(KeyError):
            sequence.swap_items(["a", "b"], "a", "z")

    def test_normalize_order(self):
        self.assertEqual(
            sequence.normalize_order(["c", "a", "c", "ghost"],
                                     ["a", "b", "c", "d"]),
            ["c", "a", "b", "d"])
        self.assertEqual(sequence.normalize_order([], ["a", "b"]),
                         ["a", "b"])
        self.assertEqual(sequence.normalize_order(["b"], []), [])

    def test_without(self):
        self.assertEqual(sequence.without(["a", "b", "a"], "a"), ["b"])


# ---------------------------------------------------------------------------
# Space ordering: defaults and persistence
# ---------------------------------------------------------------------------


class _IsolatedHome(unittest.TestCase):
    """Pin the global config (state + spaces + registry) to a tmp HOME."""

    _ENV_KEYS = ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                 "CK_SANDBOX_ROOT")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for key in ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE"):
            os.environ.pop(key, None)
        os.environ["CK_SANDBOX_ROOT"] = str(self.home / ".sandbox")

        self._orig = (
            ckconfig.GLOBAL_CONFIG_DIR,
            ckconfig.GLOBAL_REGISTRY_FILE,
            ckconfig.GLOBAL_STATE_FILE,
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE,
            ckregistry.GLOBAL_CONFIG_DIR,
            ckregistry.GLOBAL_REGISTRY_FILE,
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE,
        )
        new_dir = self.home / ".config" / "ck"
        ckconfig.GLOBAL_CONFIG_DIR = new_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
        ckconfig.GLOBAL_STATE_FILE = new_dir / "state.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self.home / ".ckrc"
        ckregistry.GLOBAL_CONFIG_DIR = new_dir
        ckregistry.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self.home / ".ckrc"

        def _restore() -> None:
            (ckconfig.GLOBAL_CONFIG_DIR,
             ckconfig.GLOBAL_REGISTRY_FILE,
             ckconfig.GLOBAL_STATE_FILE,
             ckconfig.LEGACY_GLOBAL_CONFIG_FILE,
             ckregistry.GLOBAL_CONFIG_DIR,
             ckregistry.GLOBAL_REGISTRY_FILE,
             ckregistry.LEGACY_GLOBAL_CONFIG_FILE) = self._orig
        self.addCleanup(_restore)

        def _restore_env() -> None:
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(_restore_env)

        # A real project so `ck space list` has an active context.
        self.root = self.home / "myproj"
        self.root.mkdir()
        self.ck = ContextKeeper(root=self.root)
        self.ck.init()
        self._old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._old_cwd)

    # ---- helpers ----

    @property
    def state_file(self) -> Path:
        return spaces.global_state_path()

    def state(self) -> dict:
        self.assertTrue(self.state_file.exists())
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def write_state(self, payload: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(payload), encoding="utf-8")

    def make_spaces(self, *names: str) -> None:
        for name in names:
            spaces.create_space(name)

    def plan_text(self) -> str:
        return self.ck.plan_file.read_text(encoding="utf-8")


class TestSpaceOrderBasics(_IsolatedHome):

    def test_default_order_includes_project_first(self):
        self.make_spaces("work", "blog")
        self.assertEqual(
            spaces.space_order(include_project=True),
            ["project", "local", "remote", "blog", "work"])

    def test_dashboard_order_excludes_project(self):
        self.make_spaces("work")
        self.assertEqual(
            spaces.space_order(include_project=False),
            ["local", "remote", "work"])
        from cklib.core import _space_order
        self.assertEqual(
            _space_order(), ["local", "remote", "work"])

    def test_reads_do_not_create_the_state_file(self):
        self.assertEqual(spaces.read_space_order(), [])
        spaces.space_order(include_project=True)
        self.assertFalse(self.state_file.exists())

    def test_corrupt_state_falls_back_to_defaults(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text("not json", encoding="utf-8")
        self.assertEqual(spaces.read_space_order(), [])
        self.assertEqual(spaces.space_order(include_project=True)[0],
                         "project")

    def test_stale_and_duplicate_entries_are_reconciled_on_read(self):
        self.make_spaces("work")
        self.write_state({"spaces_order": [
            "work", "ghost", "local", "work", 7, "remote"]})
        self.assertEqual(
            spaces.space_order(include_project=True),
            ["project", "work", "local", "remote"])


class TestSpaceMove(_IsolatedHome):

    def test_move_persists_order(self):
        self.make_spaces("work", "blog")
        order = spaces.move_space("work", 1)
        self.assertEqual(order, ["work", "project", "local", "remote",
                                 "blog"])
        self.assertEqual(self.state()["spaces_order"], order)
        self.assertEqual(spaces.space_order(include_project=True), order)

    def test_move_project_space(self):
        self.make_spaces("work")
        order = spaces.move_space("project", 2)
        self.assertEqual(order[:3], ["local", "project", "remote"])
        self.assertEqual(spaces.space_order(include_project=True), order)
        # The dashboard sequence simply omits the sentinel.
        self.assertEqual(spaces.space_order(include_project=False)[0],
                         "local")

    def test_move_to_current_position_is_a_no_op(self):
        order = spaces.space_order(include_project=True)
        self.assertEqual(spaces.move_space("local", 2), order)

    def test_out_of_range_is_rejected_and_not_persisted(self):
        self.make_spaces("work")
        for position in (0, -1, 99):
            with self.assertRaises(ValueError):
                spaces.move_space("work", position)
        self.assertFalse(self.state_file.exists())

    def test_unknown_space_is_rejected(self):
        with self.assertRaises(spaces.SpaceNotFoundError):
            spaces.move_space("ghost", 1)
        self.assertFalse(self.state_file.exists())

    def test_other_state_keys_are_preserved(self):
        self.write_state({"last_update_check": "2026-01-01T00:00:00+00:00"})
        spaces.move_space("local", 1)
        state = self.state()
        self.assertEqual(state["last_update_check"],
                         "2026-01-01T00:00:00+00:00")
        self.assertIn("spaces_order", state)

    def test_cli_move(self):
        self.make_spaces("work")
        rc, out, err = _run_main(["space", "move", "work", "1"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Moved space 'work' -> position 1", out)
        self.assertEqual(spaces.read_space_order()[0], "work")

    def test_cli_move_bad_position(self):
        rc, out, _ = _run_main(["space", "move", "local", "abc"])
        self.assertEqual(rc, 2)
        self.assertIn("Invalid position", out)

    def test_cli_move_out_of_range(self):
        rc, out, _ = _run_main(["space", "move", "local", "99"])
        self.assertEqual(rc, 2)
        self.assertIn("out of range", out)

    def test_cli_move_unknown_space_is_not_found(self):
        rc, out, err = _run_main(["space", "move", "ghost", "1"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", out)
        self.assertNotIn("Traceback", err)


class TestSpaceSwap(_IsolatedHome):

    def test_swap_persists(self):
        order = spaces.swap_spaces("local", "remote")
        self.assertEqual(order, ["project", "remote", "local"])
        self.assertEqual(self.state()["spaces_order"], order)

    def test_swap_with_project(self):
        order = spaces.swap_spaces("project", "remote")
        self.assertEqual(order, ["remote", "local", "project"])

    def test_swap_same_space_is_noop(self):
        order = spaces.space_order(include_project=True)
        self.assertEqual(spaces.swap_spaces("local", "local"), order)

    def test_swap_unknown_is_rejected(self):
        with self.assertRaises(spaces.SpaceNotFoundError):
            spaces.swap_spaces("local", "ghost")
        self.assertFalse(self.state_file.exists())

    def test_cli_swap(self):
        rc, out, err = _run_main(["space", "swap", "local", "remote"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Swapped spaces 'local' <-> 'remote'", out)
        self.assertEqual(spaces.read_space_order(),
                         ["project", "remote", "local"])


class TestSpaceReorder(_IsolatedHome):

    def test_reorder_normalizes_and_persists(self):
        self.make_spaces("work", "blog")
        self.write_state({"spaces_order": [
            "work", "ghost", "project", "work"]})
        order = spaces.reorder_spaces()
        self.assertEqual(
            order, ["work", "project", "local", "remote", "blog"])
        self.assertEqual(self.state()["spaces_order"], order)

    def test_reorder_adds_the_project_sentinel_first_when_missing(self):
        self.write_state({"spaces_order": ["remote", "local"]})
        order = spaces.reorder_spaces()
        self.assertEqual(order, ["project", "remote", "local"])

    def test_reorder_leaves_plan_tasks_untouched(self):
        self.ck.add_task("keep me")
        before = self.plan_text()
        spaces.reorder_spaces()
        self.assertEqual(self.plan_text(), before)

    def test_cli_reorder_prints_numbered_order(self):
        self.make_spaces("work")
        rc, out, err = _run_main(["space", "reorder"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Normalized space order:", out)
        self.assertIn("1. project", out)
        self.assertIn("4. work", out)

    def test_cli_reorder_rejects_arguments(self):
        rc, out, _ = _run_main(["space", "reorder", "work"])
        self.assertEqual(rc, 2)
        self.assertIn("takes no arguments", out)


class TestSpaceRenameKeepsPosition(_IsolatedHome):

    def test_rename_carries_the_persisted_slot(self):
        self.make_spaces("work")
        spaces.move_space("work", 1)
        info = spaces.rename_space("work", "office")
        self.assertEqual(info["from"], "work")
        self.assertEqual(spaces.read_space_order()[0], "office")
        self.assertNotIn("work", spaces.read_space_order())


# ---------------------------------------------------------------------------
# Deletion, confirmation and force flag
# ---------------------------------------------------------------------------


class TestSpaceDelete(_IsolatedHome):

    def test_force_flag_deletes_and_drops_from_order(self):
        self.make_spaces("work")
        spaces.move_space("work", 1)
        rc, out, err = _run_main(["space", "delete", "work", "-f"])
        self.assertEqual(rc, 0, err)
        self.assertFalse(spaces.space_path("work").exists())
        self.assertNotIn("work", self.state()["spaces_order"])

    def test_long_force_flag(self):
        self.make_spaces("work")
        rc, _, err = _run_main(["space", "delete", "work", "--force"])
        self.assertEqual(rc, 0, err)
        self.assertFalse(spaces.space_path("work").exists())

    def test_yes_flag_still_works(self):
        self.make_spaces("work")
        rc, _, err = _run_main(["space", "delete", "work", "-y"])
        self.assertEqual(rc, 0, err)
        self.assertFalse(spaces.space_path("work").exists())

    def test_interactive_confirmation_yes(self):
        self.make_spaces("work")
        rc, out, err = _run_main(
            ["space", "delete", "work"], tty=True, answer="y")
        self.assertEqual(rc, 0, err)
        self.assertFalse(spaces.space_path("work").exists())

    def test_interactive_confirmation_no_aborts(self):
        self.make_spaces("work")
        rc, out, _ = _run_main(
            ["space", "delete", "work"], tty=True, answer="n")
        self.assertEqual(rc, 2)
        self.assertIn("Aborted.", out)
        self.assertTrue(spaces.space_path("work").exists())

    def test_non_interactive_without_force_refuses(self):
        self.make_spaces("work")
        rc, out, err = _run_main(["space", "delete", "work"])
        self.assertEqual(rc, 2)
        self.assertIn("Refusing to delete space", out)
        self.assertTrue(spaces.space_path("work").exists())
        self.assertNotIn("Traceback", err)

    def test_project_space_cannot_be_deleted(self):
        rc, out, err = _run_main(["space", "delete", "project", "-f"])
        self.assertEqual(rc, 2)
        self.assertIn("default project space", out)
        self.assertNotIn("Traceback", err)

    def test_builtin_space_cannot_be_deleted(self):
        rc, out, _ = _run_main(["space", "delete", "local", "-f"])
        self.assertEqual(rc, 2)
        self.assertIn("built-in space", out)
        self.assertTrue(spaces.is_valid_space("local"))

    def test_unknown_space_delete_is_not_found(self):
        rc, out, _ = _run_main(["space", "delete", "ghost", "-f"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", out)

    def test_space_delete_never_touches_plan_tasks(self):
        self.make_spaces("work")
        self.ck.add_task("survives")
        before = self.plan_text()
        rc, _, err = _run_main(["space", "delete", "work", "-f"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.plan_text(), before)
        self.assertIn("survives", self.plan_text())


# ---------------------------------------------------------------------------
# Rendering follows the persisted order
# ---------------------------------------------------------------------------


class TestSpaceDisplayOrder(_IsolatedHome):

    def test_space_list_follows_moved_project(self):
        self.make_spaces("work")
        spaces.move_space("local", 1)  # before project
        out = self.ck.space_manager_list()
        self.assertLess(out.index("LOCAL"), out.index("myproj"))
        self.assertLess(out.index("myproj"), out.index("REMOTE"))

    def test_dashboard_spaces_table_follows_order(self):
        from cklib.core import _render_spaces_table, _space_order
        self.make_spaces("work", "blog")
        spaces.move_space("work", 1)
        spaces.move_space("blog", 2)
        self.assertEqual(_space_order()[:2], ["work", "blog"])
        out = _render_spaces_table()
        self.assertLess(out.index("WORK"), out.index("BLOG"))
        self.assertLess(out.index("BLOG"), out.index("LOCAL"))


# ---------------------------------------------------------------------------
# DEV MODE isolation
# ---------------------------------------------------------------------------


class TestDevModeOrderIsolation(_IsolatedHome):

    def test_order_write_lands_in_the_sandbox_state(self):
        self.make_spaces("work")
        production_space = spaces.space_path("work")
        production_text = production_space.read_text(encoding="utf-8")
        with mock.patch.dict(os.environ, {"CK_SANDBOX": "1"}):
            # Strict isolation: the sandbox has its OWN work space.
            spaces.create_space("work")
            order = spaces.move_space("work", 1)
            # Read-your-writes: the sandboxed order wins immediately.
            self.assertEqual(spaces.space_order(include_project=True),
                             order)
        self.assertFalse(self.state_file.exists(),
                         "dev mode must not write the real state file")
        self.assertEqual(production_space.read_text(encoding="utf-8"),
                         production_text)
        sandbox_state = (cksandbox.sandbox_root() / "config" / "state.json")
        self.assertTrue(sandbox_state.exists())
        self.assertEqual(
            json.loads(sandbox_state.read_text(encoding="utf-8"))
            ["spaces_order"], order)


if __name__ == "__main__":
    unittest.main()
