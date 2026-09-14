"""Tests for global multi-project management.

Covers:

- ``ck register`` / ``ck unregister`` (with --path and --name).
- ``ck prune`` removing missing entries.
- Dashboard table rendering for active focus, no-focus, missing,
  and corrupt PLAN.md states.
- Concurrent registration calls verifying file_lock integrity.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import List

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib.config import file_lock
from cklib.cli import main
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

    def _render(self, ck, *, verbose=False):
        return _render_dashboard(
            ck, list_projects=ckregistry.list_projects,
            parse_plan_file=_safe_parse, verbose=verbose,
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
            out = self._render(ck)
            # Exactly the four spec columns, in priority order:
            # Project | Focus Task | Progress | Last Active.
            header = [l for l in out.splitlines()
                      if l.startswith("| Project")]
            self.assertEqual(len(header), 1, f"no header row in:\n{out}")
            cells = [c.strip() for c in header[0].strip("|").split("|")]
            self.assertEqual(
                cells, ["Project", "Focus Task", "Progress", "Last Active"]
            )
            # Project row present
            data_rows = [l for l in out.splitlines()
                         if l.startswith("|") and "alpha" in l]
            self.assertTrue(data_rows, f"no data row for alpha in:\n{out}")

    def test_dashboard_active_focus(self):
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "alpha")
            ck = ContextKeeper(root=target)
            ck.add_task("implement feature X")
            ck.start(2)
            ck.register(path=target, name="alpha")

            out = self._render(ck)
            # Focused task renders as [<id>] [>] <text> in the Focus
            # Task column.
            self.assertIn("[2] [>] implement feature X", out)
            # Progress renders as a compact done/total (pct%) ratio.
            self.assertIn("0/2 (0.0%)", out)

    def test_dashboard_no_focus(self):
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "no-focus")
            ck = ContextKeeper(root=target)
            ck.add_task("first task")
            ck.add_task("second task")
            ck.register(path=target, name="no-focus")

            out = self._render(ck)
            self.assertIn("Focus Task", out)
            self.assertIn("(no focus)", out)

    def test_dashboard_missing_directory(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "soon-gone"
            target.mkdir()
            ck = ContextKeeper()
            ck.register(path=target, name="ghost")
            target.rmdir()  # remove the folder
            out = self._render(ck)
            self.assertIn("missing", out)
            self.assertIn("ghost", out)
            self.assertIn("[MISSING] ghost", out)
            self.assertIn(
                "💡 Found 1 missing project(s). Run 'ck prune' to cleanup.",
                out,
            )

    def test_list_global_missing_tag_and_cleanup_tip(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "gone"
            target.mkdir()
            ckregistry.register_project(target, name="gone-project")
            target.rmdir()

            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["list", "-g"])

            self.assertEqual(code, 0)
            self.assertIn("[MISSING] gone-project", output.getvalue())
            self.assertIn(
                "💡 Found 1 missing project(s). Run 'ck prune' to cleanup.",
                output.getvalue(),
            )

    def test_prune_cli_reports_paths_and_summary(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "purge-me"
            target.mkdir()
            ckregistry.register_project(target, name="purge-me")
            target.rmdir()

            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["prune"])

            text = output.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("Pruned 1 missing entries", text)
            self.assertIn(str(target.resolve()), text)
            self.assertIn(
                "Summary: 1 project(s) purged from the global registry.",
                text,
            )
            self.assertEqual(ckregistry.list_projects(), [])

    def test_dashboard_corrupt_plan(self):
        """A missing/unparseable PLAN.md must not crash the dashboard."""
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "corrupt")
            # Remove PLAN.md entirely — folder exists, plan missing.
            (target / ".ck" / "PLAN.md").unlink()
            ck = ContextKeeper()
            ck.register(path=target, name="corrupt")
            out = self._render(ck)
            self.assertIn("corrupt", out)

    def test_dashboard_empty_plan(self):
        """An empty PLAN.md is also handled gracefully."""
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "empty")
            plan = target / ".ck" / "PLAN.md"
            plan.write_text("", encoding="utf-8")
            ck = ContextKeeper()
            ck.register(path=target, name="empty")
            out = self._render(ck)
            # Empty plan: 0/0 tasks, no focus.
            self.assertIn("0/0 (0.0%)", out)
            self.assertIn("(no focus)", out)

    def test_dashboard_active_marker(self):
        """The current working directory gets the `*` suffix."""
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "active")
            ck = ContextKeeper(root=target)
            ck.register(path=target, name="active")
            out = self._render(ck)
            self.assertIn("active *", out)


class TestDashboardVerbose(_IsolatedRegistry, unittest.TestCase):
    """``ck dashboard -v`` block view: full triad per project."""

    def setUp(self):
        self._isolate()

    def _render(self, ck):
        return _render_dashboard(
            ck, list_projects=ckregistry.list_projects,
            parse_plan_file=_safe_parse, verbose=True,
        )

    def test_verbose_header_counts_projects(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            _project_dir(td_path, "alpha")
            _project_dir(td_path, "beta")
            ck = ContextKeeper()
            ck.register(path=td_path / "alpha", name="alpha")
            ck.register(path=td_path / "beta", name="beta")
            out = self._render(ck)
            self.assertIn("📭 МОИ ПРОЕКТЫ (2)", out)

    def test_verbose_block_layout_and_triad(self):
        """Spec-exact block: header, path, progress, triad, separator."""
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "alpha")
            ck = ContextKeeper(root=target)
            # Default plan ships one open task (id 1); add prev ->
            # done, focus -> focused, next -> open.
            ck.add_task("prev task")
            ck.add_task("focus task")
            ck.add_task("next task")
            ck.done("2")
            ck.start(3)
            ck.register(path=target, name="alpha")

            out = self._render(ck)
            lines = out.splitlines()
            self.assertEqual(lines[0], "📭 МОИ ПРОЕКТЫ (1)")
            # cwd marker: the project block carries [*]
            self.assertIn("🚀 alpha [*]", out)
            self.assertIn(f"    📍 {target.resolve()}", out)
            self.assertIn("    📊 Прогресс: 1/4 (25.0%)", out)
            self.assertIn("    🎯 Контекст:", out)
            # Vertical triad order: PREV < FOCUS < NEXT.
            idx_prev = out.index("⏮️  [2] prev task [x]")
            idx_focus = out.index("👉 [3] [>] focus task")
            idx_next = out.index("⏭️  [4] next task [ ]")
            self.assertLess(idx_prev, idx_focus)
            self.assertLess(idx_focus, idx_next)
            # Each block is isolated by a 61-char separator bar.
            self.assertIn("═" * 61, out)
            self.assertTrue(out.rstrip().endswith("═" * 61))

    def test_verbose_all_done_project(self):
        """All-done projects collapse the triad to a single line."""
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "finished")
            ck = ContextKeeper(root=target)
            ck.add_task("only task")
            ck.done("1")
            ck.done("2")
            ck.register(path=target, name="finished")

            out = self._render(ck)
            self.assertIn(
                "🎯 Контекст: (все задачи выполнены 🎉)", out
            )
            self.assertNotIn("👉", out)

    def test_verbose_no_focus_hint(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            target = _project_dir(td_path, "nofocus")
            ck = ContextKeeper(root=target)
            ck.add_task("an open task")
            ck.register(path=target, name="nofocus")

            # Render from a DIFFERENT root so the marker is [ ]
            # (non-cwd); the cwd variant is covered by the triad test.
            elsewhere = ContextKeeper(root=td_path / "elsewhere")
            out = self._render(elsewhere)
            self.assertIn("🚀 nofocus [ ]", out)
            self.assertIn("👉 (фокус не выбран)", out)

    def test_verbose_missing_and_corrupt_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            gone = td_path / "gone"
            gone.mkdir()
            ck = ContextKeeper()
            ck.register(path=gone, name="ghost")
            gone.rmdir()
            corrupt = _project_dir(td_path, "corrupt")
            # Folder exists but PLAN.md is gone -> corrupt block.
            (corrupt / ".ck" / "PLAN.md").unlink()
            ck.register(path=corrupt, name="corrupt")

            out = self._render(ck)
            self.assertIn("⚠️  missing", out)
            self.assertIn("⚠️  corrupt", out)
            self.assertIn("🚀 [MISSING] ghost [ ]", out)
            # Degraded blocks still render the header + separator.
            self.assertEqual(out.count("═" * 61), 2)

    def test_verbose_no_truncation_of_titles(self):
        """Unlike the table, the block view never truncates titles."""
        with tempfile.TemporaryDirectory() as td:
            target = _project_dir(Path(td), "longtitle")
            title = "x" * 120
            ck = ContextKeeper(root=target)
            ck.add_task(title)
            ck.start(2)
            ck.register(path=target, name="longtitle")

            out = self._render(ck)
            self.assertIn(f"👉 [2] [>] {title}", out)


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

    def test_seconds_ago(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(seconds=42)).astimezone().isoformat()
        self.assertEqual(_relative_time(ts), "42s ago")

    def test_minutes_ago(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(minutes=5)).astimezone().isoformat()
        self.assertEqual(_relative_time(ts), "5m ago")

    def test_hours_ago(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(hours=3)).astimezone().isoformat()
        self.assertEqual(_relative_time(ts), "3h ago")

    def test_yesterday(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(days=1)).astimezone().isoformat()
        self.assertEqual(_relative_time(ts), "yesterday")

    def test_days_ago(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(days=2)).astimezone().isoformat()
        self.assertEqual(_relative_time(ts), "2d ago")

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
