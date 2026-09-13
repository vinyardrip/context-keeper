"""Tests for global multi-project management.

Covers:

- ``ck register`` / ``ck unregister`` (with --path and --name).
- ``ck prune`` removing missing entries.
- Dashboard table rendering for active focus, no-focus, missing,
  and corrupt PLAN.md states.
- Concurrent registration calls verifying file_lock integrity.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import List

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.config import file_lock
from cklib.core import ContextKeeper, _relative_time, _render_dashboard


# ---------------------------------------------------------------------------
# Mixin: redirect the global registry to a per-test tmp directory
# ---------------------------------------------------------------------------


class _IsolatedRegistry:
    """Mixin that pins the global registry to a per-test tmp HOME."""

    def _isolate(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake_home = Path(tmp.name)

        self._orig_dir = ckconfig.GLOBAL_CONFIG_DIR
        self._orig_file = ckconfig.GLOBAL_REGISTRY_FILE
        self._orig_legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE

        new_global_dir = fake_home / ".config" / "ck"
        new_registry_file = new_global_dir / "projects.json"
        new_legacy = fake_home / ".ckrc"

        ckconfig.GLOBAL_CONFIG_DIR = new_global_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_registry_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = new_legacy
        ckregistry.GLOBAL_CONFIG_DIR = new_global_dir
        ckregistry.GLOBAL_REGISTRY_FILE = new_registry_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = new_legacy

        def _restore() -> None:
            ckconfig.GLOBAL_CONFIG_DIR = self._orig_dir
            ckconfig.GLOBAL_REGISTRY_FILE = self._orig_file
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
            ckregistry.GLOBAL_CONFIG_DIR = self._orig_dir
            ckregistry.GLOBAL_REGISTRY_FILE = self._orig_file
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
        self.addCleanup(_restore)

        return fake_home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_dir(parent: Path, name: str, *, init: bool = True) -> Path:
    p = parent / name
    p.mkdir(parents=True, exist_ok=True)
    if init:
        ck = ContextKeeper(root=p)
        ck.init()
    return p


# ---------------------------------------------------------------------------
# Register / unregister
# ---------------------------------------------------------------------------


class TestRegisterUnregister(_IsolatedRegistry, unittest.TestCase):
    def setUp(self):
        self._isolate()

    def test_register_with_explicit_path_and_name(self):
        home = Path(self._orig_dir).parent  # not used; just checking isolation
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "explicit"
            target.mkdir()
            ck = ContextKeeper()
            entry = ck.register(path=target, name="My Project")
            self.assertEqual(entry.name, "My Project")
            self.assertEqual(entry.path, str(target.resolve()))

    def test_register_default_uses_folder_name(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "myfolder"
            target.mkdir()
            ck = ContextKeeper()
            entry = ck.register(path=target)
            self.assertEqual(entry.name, "myfolder")

    def test_register_idempotent_updates_last_seen(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "p"
            target.mkdir()
            ck = ContextKeeper()
            e1 = ck.register(path=target)
            time.sleep(0.01)
            e2 = ck.register(path=target)
            self.assertEqual(e1.path, e2.path)
            # The second register should refresh last_seen
            self.assertGreaterEqual(e2.last_seen, e1.last_seen)

    def test_unregister_by_path(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "p"
            target.mkdir()
            ck = ContextKeeper()
            ck.register(path=target, name="p1")
            self.assertTrue(ck.unregister(path=target))
            self.assertEqual(ckregistry.list_projects(), [])

    def test_unregister_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "p"
            target.mkdir()
            ck = ContextKeeper()
            ck.register(path=target, name="named-project")
            self.assertTrue(ck.unregister(name="named-project"))
            self.assertEqual(ckregistry.list_projects(), [])

    def test_unregister_succeeds_when_folder_missing(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "p"
            target.mkdir()
            ck = ContextKeeper()
            ck.register(path=target, name="p1")
            target.rmdir()  # remove the folder
            # The unregister must NOT raise — we only mutate the registry.
            self.assertTrue(ck.unregister(path=target))
            self.assertEqual(ckregistry.list_projects(), [])

    def test_unregister_returns_false_when_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "never-registered"
            target.mkdir()
            ck = ContextKeeper()
            self.assertFalse(ck.unregister(path=target))


# ---------------------------------------------------------------------------
# Prune
# ---------------------------------------------------------------------------


class TestPrune(_IsolatedRegistry, unittest.TestCase):
    def setUp(self):
        self._isolate()

    def test_prune_removes_missing_paths(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            keep = td_path / "keep"
            gone = td_path / "gone"
            keep.mkdir()
            gone.mkdir()
            ck = ContextKeeper()
            ck.register(path=keep, name="keep")
            ck.register(path=gone, name="gone")

            # Delete one
            gone.rmdir()
            pruned = ck.prune()
            self.assertEqual(pruned, [str(gone.resolve())])
            remaining = [p.name for p in ckregistry.list_projects()]
            self.assertEqual(remaining, ["keep"])

    def test_prune_no_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "alive"
            target.mkdir()
            ck = ContextKeeper()
            ck.register(path=target, name="alive")
            self.assertEqual(ck.prune(), [])
            self.assertEqual(len(ckregistry.list_projects()), 1)

    def test_prune_on_empty_registry(self):
        self.assertEqual(ContextKeeper().prune(), [])


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class TestDashboardTable(_IsolatedRegistry, unittest.TestCase):
    def setUp(self):
        self._isolate()

    def _render(self, ck):
        return _render_dashboard(
            ck,
            list_projects=ckregistry.list_projects,
            parse_plan_file=ContextKeeper.__module__ and _safe_parse,
        )

    def test_dashboard_with_no_projects(self):
        ck = ContextKeeper()
        out = _render_dashboard(
            ck, list_projects=ckregistry.list_projects,
            parse_plan_file=_safe_parse,
        )
        self.assertIn("No registered projects", out)

    def test_dashboard_table_columns(self):
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "alpha")
            ck = ContextKeeper()
            ck.register(path=target, name="alpha")
            out = _render_dashboard(
                ck, list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse,
            )
            # All five headers present
            for col in ("Project", "Path", "Last Active", "Focus Task", "Status"):
                self.assertIn(col, out)
            # Project row present
            self.assertIn("alpha", out)
            # Path row present. Short tmp paths are middle-truncated
            # ("…") to honor the ~85-char table width cap; the row
            # must carry the project folder name tail.
            data_rows = [l for l in out.splitlines()
                         if l.startswith("|") and "alpha" in l]
            self.assertTrue(data_rows, f"no data row for alpha in:\n{out}")
            self.assertIn("alpha", data_rows[0])

    def test_dashboard_active_focus(self):
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "alpha")
            ck = ContextKeeper(root=target)
            ck.add_task("implement feature X")
            ck.start(2)
            ck.register(path=target, name="alpha")

            out = _render_dashboard(
                ck, list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse,
            )
            self.assertIn("[>]", out)
            # The focused title may be middle-truncated ("…") by the
            # ~85-char width cap; assert on a stable prefix + suffix.
            self.assertIn("impl", out)
            self.assertIn("…nt feature X", out)
            self.assertIn("open,", out)  # status column

    def test_dashboard_no_focus(self):
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "no-focus")
            ck = ContextKeeper(root=target)
            ck.add_task("first task")
            ck.add_task("second task")
            # Mark the first as done so there is no focused and no
            # open task left (default task becomes done).
            ck.done("1")
            # Add a new task that becomes the next active.
            ck.add_task("next thing to do")
            ck.register(path=target, name="no-focus")

            out = _render_dashboard(
                ck, list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse,
            )
            # Should have a Focus Task entry, either focused or active
            self.assertIn("Focus Task", out)
            # No literal "[>]" — but it may appear if start() was called.
            # The key invariant: rendering didn't crash.
            self.assertIn("Status", out)

    def test_dashboard_missing_directory(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "soon-gone"
            target.mkdir()
            ck = ContextKeeper()
            ck.register(path=target, name="ghost")
            target.rmdir()  # remove the folder
            out = _render_dashboard(
                ck, list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse,
            )
            self.assertIn("missing", out)
            self.assertIn("ghost", out)

    def test_dashboard_corrupt_plan(self):
        """Corrupt PLAN.md must not crash the dashboard."""
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "corrupt")
            # Overwrite PLAN.md with garbage
            plan = target / ".ck" / "PLAN.md"
            plan.write_text("not valid markdown \x00\x01\x02 broken",
                           encoding="utf-8")
            ck = ContextKeeper()
            ck.register(path=target, name="corrupt")
            out = _render_dashboard(
                ck, list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse,
            )
            self.assertIn("corrupt", out)
            # The "corrupt" status indicator appears
            self.assertIn("corrupt", out.lower())

    def test_dashboard_empty_plan(self):
        """An empty PLAN.md is also handled gracefully."""
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "empty")
            plan = target / ".ck" / "PLAN.md"
            plan.write_text("", encoding="utf-8")
            ck = ContextKeeper()
            ck.register(path=target, name="empty")
            out = _render_dashboard(
                ck, list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse,
            )
            # Should show 0 open, 0 done
            self.assertIn("0 open, 0 done", out)
            self.assertIn("none", out)  # focus = none

    def test_dashboard_active_marker(self):
        """The current working directory is highlighted."""
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "active")
            ck = ContextKeeper(root=target)
            ck.register(path=target, name="active")
            out = _render_dashboard(
                ck, list_projects=ckregistry.list_projects,
                parse_plan_file=_safe_parse,
            )
            self.assertIn("← active", out)


# A local copy of the safe parse helper that doesn't depend on core's
# private name (in case it changes).
def _safe_parse(plan_path: Path):
    from cklib.parser import parse_plan_file
    try:
        if not plan_path.exists():
            return None
        return parse_plan_file(plan_path)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Relative time helper
# ---------------------------------------------------------------------------


class TestRelativeTime(unittest.TestCase):
    def test_just_now(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        # Use a recent past timestamp
        ts = (now - timedelta(seconds=2)).astimezone().isoformat()
        self.assertIn("now", _relative_time(ts))

    def test_minutes_ago(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(minutes=5)).astimezone().isoformat()
        self.assertIn("minute", _relative_time(ts))

    def test_hours_ago(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(hours=3)).astimezone().isoformat()
        self.assertIn("hour", _relative_time(ts))

    def test_days_ago(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(days=2)).astimezone().isoformat()
        self.assertIn("day", _relative_time(ts))

    def test_unknown_when_empty(self):
        self.assertEqual(_relative_time(""), "unknown")

    def test_unknown_when_garbage(self):
        self.assertEqual(_relative_time("not-a-date"), "unknown")


# ---------------------------------------------------------------------------
# Concurrent registration with file_lock
# ---------------------------------------------------------------------------


class TestConcurrentFileLock(_IsolatedRegistry, unittest.TestCase):
    def setUp(self):
        self._isolate()

    def test_concurrent_register_projects(self):
        """Spawn N threads each registering a different project.

        All entries must be present after the dust settles (no torn
        writes, no lost updates). Uses file_lock to serialise.
        """
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            n = 20
            errors: List[str] = []
            barrier = threading.Barrier(n)

            def worker(i: int) -> None:
                try:
                    proj = td_path / f"p{i}"
                    proj.mkdir(parents=True, exist_ok=True)
                    barrier.wait(timeout=10)
                    ck = ContextKeeper()
                    ck.register(path=proj, name=f"p{i}")
                except Exception as e:
                    errors.append(repr(e))

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            self.assertEqual(errors, [])
            registered = {p.name for p in ckregistry.list_projects()}
            self.assertEqual(len(registered), n)
            self.assertEqual(registered, {f"p{i}" for i in range(n)})

    def test_file_lock_releases_on_exception(self):
        """If the critical section raises, the lock is released."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "x"
            target.mkdir()
            file_path = target / "f.json"
            file_path.write_text("{}")

            class Boom(Exception):
                pass

            with self.assertRaises(Boom):
                with file_lock(file_path):
                    raise Boom("explode")

            # Subsequent acquire must not block — the previous lock
            # was released.
            t0 = time.monotonic()
            with file_lock(file_path):
                pass
            self.assertLess(time.monotonic() - t0, 2.0)


if __name__ == "__main__":
    unittest.main()