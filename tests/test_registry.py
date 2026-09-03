"""Tests for the global project registry.

Coverage (per security review):

- Legacy ``.ckrc`` is migrated AND permanently deleted.
- High-resolution (microsecond) timestamps are stored.
- ``list_projects`` sorts by ``last_seen`` descending, with name as
  the secondary sort key for deterministic ordering.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry


class IsolatedHomeMixin:
    """Mixin: redirect the global registry to a per-test tmp directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._orig_dir = ckconfig.GLOBAL_CONFIG_DIR
        self._orig_file = ckconfig.GLOBAL_REGISTRY_FILE
        self._orig_legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE

        new_global_dir = fake_home / ".config" / "ck"
        new_registry_file = new_global_dir / "projects.json"
        new_legacy = fake_home / ".ckrc"

        ckconfig.GLOBAL_CONFIG_DIR = new_global_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_registry_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = new_legacy
        registry.GLOBAL_CONFIG_DIR = new_global_dir
        registry.GLOBAL_REGISTRY_FILE = new_registry_file
        registry.LEGACY_GLOBAL_CONFIG_FILE = new_legacy

    def tearDown(self):
        ckconfig.GLOBAL_CONFIG_DIR = self._orig_dir
        ckconfig.GLOBAL_REGISTRY_FILE = self._orig_file
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
        registry.GLOBAL_CONFIG_DIR = self._orig_dir
        registry.GLOBAL_REGISTRY_FILE = self._orig_file
        registry.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy


class TestRegister(IsolatedHomeMixin, unittest.TestCase):
    def _project(self, name: str = "demo") -> Path:
        p = Path(self._tmp.name) / name
        p.mkdir()
        return p

    def test_register_creates_file_and_entry(self):
        p = self._project("alpha")
        entry = registry.register_project(p, name="alpha")
        self.assertEqual(entry.name, "alpha")
        self.assertEqual(entry.path, str(p.resolve()))

        data = json.loads(ckconfig.GLOBAL_REGISTRY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(data["projects"]), 1)
        self.assertEqual(data["projects"][0]["name"], "alpha")

    def test_register_idempotent(self):
        p = self._project("beta")
        e1 = registry.register_project(p)
        e2 = registry.register_project(p)
        self.assertEqual(e1.path, e2.path)
        data = json.loads(ckconfig.GLOBAL_REGISTRY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(data["projects"]), 1)

    def test_list_returns_newest_first(self):
        a = self._project("a")
        b = self._project("b")
        registry.register_project(a)
        registry.register_project(b)
        projects = registry.list_projects()
        self.assertEqual(projects[0].name, "b")
        self.assertEqual(projects[1].name, "a")

    def test_update_active_task(self):
        p = self._project()
        registry.register_project(p)
        registry.update_active_task(p, task_id=7, task_title="hello")
        entries = registry.list_projects()
        self.assertEqual(entries[0].active_task_id, 7)
        self.assertEqual(entries[0].active_task_title, "hello")

    def test_remove_project(self):
        p = self._project()
        registry.register_project(p)
        self.assertEqual(len(registry.list_projects()), 1)
        self.assertTrue(registry.remove_project(p))
        self.assertEqual(registry.list_projects(), [])
        self.assertFalse(registry.remove_project(p))


class TestTimestamps(IsolatedHomeMixin, unittest.TestCase):
    def test_microsecond_precision_in_last_seen(self):
        p = Path(self._tmp.name) / "p"
        p.mkdir()
        entry = registry.register_project(p)
        # ISO format: YYYY-MM-DDTHH:MM:SS.ffffff+HH:MM
        self.assertRegex(
            entry.last_seen,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}",
        )

    def test_secondary_sort_by_name_when_timestamps_collide(self):
        """Same timestamp → sort by name."""
        a = Path(self._tmp.name) / "alpha"
        a.mkdir()
        b = Path(self._tmp.name) / "beta"
        b.mkdir()
        c = Path(self._tmp.name) / "gamma"
        c.mkdir()

        # Register in non-alphabetical order, all within the same microsecond
        registry.register_project(c)
        registry.register_project(a)
        registry.register_project(b)

        # We can't perfectly force identical timestamps from Python,
        # so we monkey-patch the registry to use stable timestamps.
        from cklib.registry import ProjectEntry, _now_iso

        # Replace each project's last_seen with a fixed string
        reg = registry.Registry.load()
        for entry in reg.projects:
            entry.last_seen = "2026-09-03T12:00:00.000000+00:00"
        reg.save()

        projects = registry.list_projects()
        names = [p.name for p in projects]
        self.assertEqual(names, sorted(names))


class TestLegacyMigration(IsolatedHomeMixin, unittest.TestCase):
    def test_legacy_ckrc_migrated_and_deleted(self):
        legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE
        legacy.write_text(json.dumps({
            "projects": [
                {"name": "legacy1", "path": "/some/where",
                 "registered_at": "2024-01-01T00:00:00", "last_seen": ""},
            ]
        }), encoding="utf-8")

        projects = registry.list_projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].name, "legacy1")

        # Per the security review: legacy must be unlinked.
        self.assertFalse(legacy.exists())

    def test_legacy_only_migrated_once(self):
        legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE
        legacy.write_text(json.dumps({
            "projects": [{"name": "x", "path": "/x",
                          "registered_at": "", "last_seen": ""}]
        }), encoding="utf-8")

        registry.list_projects()
        self.assertFalse(legacy.exists())

        # A second load must not crash and must not resurrect the legacy file
        registry.list_projects()
        self.assertFalse(legacy.exists())

    def test_invalid_legacy_does_not_block_registry(self):
        legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE
        legacy.write_text("not json at all", encoding="utf-8")
        # Should return an empty registry, not crash
        self.assertEqual(registry.list_projects(), [])
        # Legacy file may or may not be deleted (it's not valid JSON);
        # the important thing is that we don't blow up.


class TestAtomicWrite(IsolatedHomeMixin, unittest.TestCase):
    def test_atomic_write_does_not_corrupt(self):
        p = Path(self._tmp.name) / "p"
        p.mkdir()
        registry.register_project(p)
        raw = ckconfig.GLOBAL_REGISTRY_FILE.read_text(encoding="utf-8")
        json.loads(raw)  # must round-trip
        leftovers = list(ckconfig.GLOBAL_REGISTRY_FILE.parent.glob(".ck-registry-*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()