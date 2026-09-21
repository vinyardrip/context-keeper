"""Tests for history archiving: rotation, compression, FIFO retention
and the archive-aware ``ck log --all`` reader.

Covers:

- :func:`cklib.history.archive_name` (compressed vs legacy naming,
  same-second collision dedupe suffix).
- :meth:`ContextKeeper._rotate_history` — trigger counting, `.md.gz`
  output via Python's built-in ``gzip`` module, gapless replacement,
  tail preservation, dev-mode redirect.
- FIFO retention: archives beyond ``MAX_BAK_FILES`` are purged oldest
  first, counting BOTH ``.md.gz`` and legacy ``.md.bak`` against one
  budget.
- :meth:`ContextKeeper.read_full_history` (``ck log --all``) —
  chronological concatenation, transparent decompression, legacy
  ``.md.bak`` backwards compatibility, corrupted-archive warning,
  empty-history placeholder.
- CLI wiring: ``log --all`` through ``main()`` (argparse path), the
  raw ``--all`` flag registry, and ``ck log`` remaining the editor
  flow when ``--all`` is absent.
"""

from __future__ import annotations

import gzip
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import history as ckhistory
from cklib.core import ContextKeeper

ENTRY = "### 2026-09-21 10:00 | [1] alpha\n- worked on alpha\n\n"


def _make_project(tmp: Path, entries: int = 5) -> tuple:
    """Initialize a project with ``entries`` history entries."""
    root = tmp / "project"
    root.mkdir()
    ck = ContextKeeper(root=root)
    ck.init()
    ck.history_file.write_text(
        "# History project\n\n" + ENTRY * entries, encoding="utf-8"
    )
    return ck, root


def _archives(ck_dir: Path) -> list:
    return sorted(
        p for p in ck_dir.iterdir() if ckhistory.is_history_archive(p)
    )


class _EnvGuard:
    """Save/restore the history env overrides around a test."""

    KEYS = ("CK_HISTORY_LIMIT", "CK_MAX_BAK_FILES",
            "CK_COMPRESS_ARCHIVES")

    def __init__(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}

    def __enter__(self):
        for k in self.KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# cklib.history helpers
# ---------------------------------------------------------------------------


class TestArchiveNaming(unittest.TestCase):
    def test_compressed_name(self):
        ts = datetime(2026, 9, 21, 12, 34, 56)
        self.assertEqual(
            ckhistory.archive_name(ts, compressed=True),
            "HISTORY_20260921_123456.md.gz",
        )

    def test_legacy_name(self):
        ts = datetime(2026, 9, 21, 12, 34, 56)
        self.assertEqual(
            ckhistory.archive_name(ts, compressed=False),
            "HISTORY_20260921_123456.md.bak",
        )

    def test_collision_dedupe_suffix(self):
        ts = datetime(2026, 9, 21, 12, 34, 56)
        self.assertEqual(
            ckhistory.archive_name(ts, compressed=True, seq=1),
            "HISTORY_20260921_123456_1.md.gz",
        )
        self.assertEqual(
            ckhistory.archive_name(ts, compressed=False, seq=12),
            "HISTORY_20260921_123456_12.md.bak",
        )

    def test_matcher_accepts_both_generations(self):
        ts = datetime(2026, 9, 21, 12, 34, 56)
        for name in (
            "HISTORY_20260921_123456.md.gz",
            "HISTORY_20260921_123456.md.bak",
            "HISTORY_20260921_123456_1.md.gz",
            "HISTORY_20260921_123456_2.md.bak",
        ):
            self.assertTrue(
                ckhistory.is_history_archive(Path(name)), name
            )
        for name in ("HISTORY.md", "HISTORY.md.bak", "HISTORY_x.md.gz",
                     "OTHER_20260921_123456.md.gz"):
            self.assertFalse(
                ckhistory.is_history_archive(Path(name)), name
            )


class TestReadArchive(unittest.TestCase):
    def test_roundtrip_gz(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "HISTORY_20260921_000000.md.gz"
            p.write_bytes(
                gzip.compress("hello history".encode("utf-8"))
            )
            text, warning = ckhistory.read_archive(p)
            self.assertEqual(text, "hello history")
            self.assertEqual(warning, "")

    def test_plain_bak(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "HISTORY_20260921_000000.md.bak"
            p.write_text("plain text", encoding="utf-8")
            text, warning = ckhistory.read_archive(p)
            self.assertEqual(text, "plain text")
            self.assertEqual(warning, "")

    def test_corrupted_gz_yields_warning(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "HISTORY_20260921_000000.md.gz"
            p.write_bytes(b"definitely not gzip")
            text, warning = ckhistory.read_archive(p)
            self.assertEqual(text, "")
            self.assertIn("Skipped unreadable archive", warning)

    def test_missing_file_yields_warning(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "HISTORY_20260921_000000.md.gz"
            text, warning = ckhistory.read_archive(p)
            self.assertEqual(text, "")
            self.assertIn("Skipped unreadable archive", warning)


class TestFifoCleanup(unittest.TestCase):
    def test_purges_oldest_beyond_budget(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            keep = []
            for i in range(5):
                p = d / f"HISTORY_2026092{i}_000000.md.gz"
                p.write_text("x", encoding="utf-8")
                keep.append(p.name)
            deleted = ckhistory.fifo_cleanup(d, max_files=2)
            remaining = [p.name for p in _archives(d)]
            self.assertEqual(deleted, keep[:3])
            self.assertEqual(remaining, keep[3:])

    def test_counts_legacy_and_gz_against_one_budget(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            names = [
                "HISTORY_20260920_000000.md.bak",
                "HISTORY_20260921_000000.md.gz",
                "HISTORY_20260922_000000.md.bak",
            ]
            for n in names:
                (d / n).write_text("x", encoding="utf-8")
            deleted = ckhistory.fifo_cleanup(d, max_files=1)
            self.assertEqual(deleted, [names[0], names[1]])
            self.assertEqual(
                [p.name for p in _archives(d)], [names[2]]
            )

    def test_zero_budget_purges_everything(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for i in range(3):
                (d / f"HISTORY_2026092{i}_000000.md.gz").write_text(
                    "x", encoding="utf-8"
                )
            deleted = ckhistory.fifo_cleanup(d, max_files=0)
            self.assertEqual(len(deleted), 3)
            self.assertEqual(_archives(d), [])

    def test_noop_under_budget(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "HISTORY_20260921_000000.md.gz").write_text(
                "x", encoding="utf-8"
            )
            self.assertEqual(ckhistory.fifo_cleanup(d, max_files=100), [])

    def test_chronological_order_ignores_directory_listing(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Created in REVERSE order; embedded timestamps decide.
            for i in (4, 3, 5):
                (d / f"HISTORY_2026092{i}_000000.md.gz").write_text(
                    "x", encoding="utf-8"
                )
            deleted = ckhistory.fifo_cleanup(d, max_files=1)
            # max_files=1 keeps only the NEWEST archive (25).
            self.assertEqual(
                deleted, ["HISTORY_20260923_000000.md.gz",
                          "HISTORY_20260924_000000.md.gz"]
            )
            self.assertEqual(
                [p.name for p in _archives(d)],
                ["HISTORY_20260925_000000.md.gz"],
            )


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


class TestRotateHistory(unittest.TestCase):
    def test_rotate_compressed_by_default(self):
        with _EnvGuard(), tempfile.TemporaryDirectory() as td:
            ck, root = _make_project(Path(td))
            ck._rotate_history()
            archives = _archives(ck.ck_path)
            self.assertEqual(len(archives), 1)
            self.assertTrue(archives[0].name.endswith(".md.gz"))
            with gzip.open(archives[0], "rt", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("### 2026-09-21 10:00 | [1] alpha", content)
            self.assertIn("# History project", content)
            # Live file: fresh header + preserved tail, gapless.
            live = ck.history_file.read_text(encoding="utf-8")
            self.assertTrue(live.startswith("# History"))
            self.assertEqual(
                sum(
                    1 for line in live.splitlines()
                    if line.startswith("### ")
                ),
                1,
            )

    def test_rotate_legacy_bak_when_compression_disabled(self):
        with _EnvGuard(), tempfile.TemporaryDirectory() as td:
            os.environ["CK_COMPRESS_ARCHIVES"] = "0"
            ck, root = _make_project(Path(td))
            ck._rotate_history()
            archives = _archives(ck.ck_path)
            self.assertEqual(len(archives), 1)
            self.assertTrue(archives[0].name.endswith(".md.bak"))
            self.assertIn(
                "### 2026-09-21 10:00 | [1] alpha",
                archives[0].read_text(encoding="utf-8"),
            )

    def test_same_second_rotations_get_dedupe_suffix(self):
        with _EnvGuard(), tempfile.TemporaryDirectory() as td:
            ck, root = _make_project(Path(td))
            for _ in range(3):
                ck.history_file.write_text(
                    "# History project\n\n" + ENTRY * 5,
                    encoding="utf-8",
                )
                ck._rotate_history()
            archives = _archives(ck.ck_path)
            self.assertEqual(len(archives), 3)
            # All three must be readable and distinct gz archives.
            full = ck.read_full_history()
            for archive in archives:
                self.assertTrue(archive.name.endswith(".md.gz"))
                with gzip.open(archive, "rt", encoding="utf-8") as fh:
                    self.assertIn("# History project", fh.read())
            self.assertIn("### 2026-09-21 10:00 | [1] alpha", full)
            # The live file sits last and is a fresh two-entry tail.
            self.assertTrue(full.rstrip().endswith("- worked on alpha"))

    def test_fifo_purge_after_rotation(self):
        with _EnvGuard(), tempfile.TemporaryDirectory() as td:
            os.environ["CK_MAX_BAK_FILES"] = "2"
            ck, root = _make_project(Path(td))
            for minute in range(4):
                ck.history_file.write_text(
                    "# History project\n\n" + ENTRY * 5,
                    encoding="utf-8",
                )
                # Distinct stamps per rotation so ordering is
                # deterministic regardless of same-second dedupe.
                with mock.patch("cklib.core.datetime") as core_dt:
                    core_dt.now.return_value = datetime(
                        2026, 9, 21, 12, 0, minute
                    )
                    ck._rotate_history()
            archives = _archives(ck.ck_path)
            self.assertEqual(len(archives), 2)
            self.assertEqual(
                [p.name for p in archives],
                ["HISTORY_20260921_120002.md.gz",
                 "HISTORY_20260921_120003.md.gz"],
            )

    def test_fifo_purge_after_rotation_distinct_stamps(self):
        with _EnvGuard(), tempfile.TemporaryDirectory() as td:
            os.environ["CK_MAX_BAK_FILES"] = "1"
            os.environ["CK_COMPRESS_ARCHIVES"] = "0"
            ck, root = _make_project(Path(td))
            stamps = [
                datetime(2026, 9, 21, 12, 0, 0),
                datetime(2026, 9, 21, 12, 0, 1),
                datetime(2026, 9, 21, 12, 0, 2),
            ]
            for ts in stamps:
                ck.history_file.write_text(
                    "# History project\n\n" + ENTRY * 5,
                    encoding="utf-8",
                )
                # Distinct stamps per rotation so ordering is
                # deterministic regardless of same-second dedupe.
                with mock.patch("cklib.core.datetime") as core_dt:
                    core_dt.now.return_value = ts
                    ck._rotate_history()
            archives = _archives(ck.ck_path)
            self.assertEqual(
                [p.name for p in archives],
                ["HISTORY_20260921_120002.md.bak"],
            )

    def test_dev_mode_rotation_redirects_to_sandbox(self):
        with _EnvGuard(), tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ck, root = _make_project(tmp)
            from cklib.sandbox import resolve_write_path
            with mock.patch("cklib.core.is_dev_mode",
                            return_value=True), \
                    mock.patch("cklib.sandbox.is_dev_mode",
                               return_value=True):
                before = ck.history_file.read_text(encoding="utf-8")
                ck._rotate_history()
                # Real history untouched (read-only dev guarantee).
                self.assertEqual(
                    ck.history_file.read_text(encoding="utf-8"), before
                )
                target = resolve_write_path(ck.ck_path)
                self.assertTrue(target.is_dir())
                self.assertEqual(len(_archives(target)), 1)
                # The sandboxed mirror took the rotation.
                mirrored = resolve_write_path(ck.history_file)
                self.assertTrue(mirrored.exists())
                self.assertEqual(
                    sum(
                        1 for line in mirrored.read_text(
                            encoding="utf-8"
                        ).splitlines()
                        if line.startswith("### ")
                    ),
                    1,
                )


# ---------------------------------------------------------------------------
# read_full_history (ck log --all)
# ---------------------------------------------------------------------------


class TestReadFullHistory(unittest.TestCase):
    def test_chronological_concat_with_live_file(self):
        with _EnvGuard(), tempfile.TemporaryDirectory() as td:
            ck, root = _make_project(Path(td))
            old = ck.ck_path / "HISTORY_20260901_000000.md.gz"
            old.write_bytes(
                gzip.compress("# History project\n\n- oldest\n\n"
                              .encode("utf-8"))
            )
            legacy = ck.ck_path / "HISTORY_20260905_000000.md.bak"
            legacy.write_text("# History project\n\n- legacy\n\n",
                              encoding="utf-8")
            full = ck.read_full_history()
            i_old = full.index("- oldest")
            i_legacy = full.index("- legacy")
            i_live = full.index("### 2026-09-21 10:00")
            self.assertLess(i_old, i_legacy)
            self.assertLess(i_legacy, i_live)

    def test_corrupted_archive_warns_without_breaking(self):
        with _EnvGuard(), tempfile.TemporaryDirectory() as td:
            ck, root = _make_project(Path(td))
            bad = ck.ck_path / "HISTORY_20260901_000000.md.gz"
            bad.write_bytes(b"\x00 not a gzip stream")
            good = ck.ck_path / "HISTORY_20260902_000000.md.bak"
            good.write_text("- good entry\n", encoding="utf-8")
            full = ck.read_full_history()
            self.assertIn("Skipped unreadable archive "
                          "HISTORY_20260901_000000.md.gz", full)
            self.assertIn("- good entry", full)
            self.assertIn("### 2026-09-21 10:00", full)

    def test_empty_history_placeholder(self):
        with _EnvGuard(), tempfile.TemporaryDirectory() as td:
            ck, root = _make_project(Path(td), entries=0)
            ck.history_file.write_text("", encoding="utf-8")
            self.assertEqual(
                ck.read_full_history(),
                "[i] No history entries found.",
            )

    def test_no_project_raises(self):
        from unittest import mock as _mock
        with _mock.patch("cklib.core.find_project_root",
                         return_value=None):
            ck = ContextKeeper(root=None)
        with self.assertRaises(ValueError):
            ck.read_full_history()


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestLogAllCLI(unittest.TestCase):
    class _IsolatedHome:
        """Pin the global registry to a per-test tmp HOME."""

        def setUp(self):
            self._tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self._tmp.cleanup)
            fake_home = Path(self._tmp.name)
            self._orig = (
                ckconfig.GLOBAL_CONFIG_DIR,
                ckconfig.GLOBAL_REGISTRY_FILE,
                ckconfig.LEGACY_GLOBAL_CONFIG_FILE,
            )
            new_dir = fake_home / ".config" / "ck"
            ckconfig.GLOBAL_CONFIG_DIR = new_dir
            ckconfig.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"
            from cklib import registry as ckregistry
            ckregistry.GLOBAL_CONFIG_DIR = new_dir
            ckregistry.GLOBAL_REGISTRY_FILE = (
                new_dir / "projects.json"
            )
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"

        def tearDown(self):
            ckconfig.GLOBAL_CONFIG_DIR = self._orig[0]
            ckconfig.GLOBAL_REGISTRY_FILE = self._orig[1]
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig[2]
            from cklib import registry as ckregistry
            ckregistry.GLOBAL_CONFIG_DIR = self._orig[0]
            ckregistry.GLOBAL_REGISTRY_FILE = self._orig[1]
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self._orig[2]

    def _project(self, tmp: Path):
        root = tmp / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()
        ck.history_file.write_text(
            "# History project\n\n" + ENTRY, encoding="utf-8"
        )
        (ck.ck_path / "HISTORY_20260901_000000.md.gz").write_bytes(
            gzip.compress("# History project\n\n- archived\n\n"
                          .encode("utf-8"))
        )
        return ck, root

    def _run_main(self, argv, cwd: Path) -> str:
        from cklib.cli import main
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

    def test_log_all_argparse_path(self):
        with tempfile.TemporaryDirectory() as td:
            ck, root = self._project(Path(td))
            out = self._run_main(["log", "--all"], root)
            self.assertIn("- archived", out)
            self.assertIn("### 2026-09-21 10:00", out)

    def test_log_all_raw_path(self):
        with tempfile.TemporaryDirectory() as td:
            ck, root = self._project(Path(td))
            out = self._run_main(["--all", "log"], root)
            self.assertIn("- archived", out)
            self.assertIn("### 2026-09-21 10:00", out)

    def test_log_all_corrupt_archive_warns(self):
        with tempfile.TemporaryDirectory() as td:
            ck, root = self._project(Path(td))
            (ck.ck_path / "HISTORY_20260901_120000.md.gz").write_bytes(
                b"junk"
            )
            out = self._run_main(["log", "--all"], root)
            self.assertIn("Skipped unreadable archive "
                          "HISTORY_20260901_120000.md.gz", out)

    def test_bare_log_does_not_print_full_history(self):
        with tempfile.TemporaryDirectory() as td:
            ck, root = self._project(Path(td))
            # No $EDITOR available in tests; the editor flow would
            # launch subprocess — patch it out and assert it runs.
            with mock.patch("cklib.core.subprocess.run") as run_mock:
                out = self._run_main(["log"], root)
            run_mock.assert_called_once()
            self.assertNotIn("- archived", out)


if __name__ == "__main__":
    unittest.main()
