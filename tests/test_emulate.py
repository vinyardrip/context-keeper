"""Tests for the ``ck dev emulate`` history-rotation stress emulator.

Covers:

- SAFETY GUARD: outside a sandbox (no dev-mode env, no ``.sandbox/``)
  the command aborts with the documented warning, exit code 1, and
  creates nothing.
- Guard pass-through: an existing ``.sandbox/`` directory (or an
  active dev session) admits the emulator.
- Cycle output: batch generation, threshold log, pre-archive state,
  the ``[+] Rotated HISTORY.md -> …md.gz`` line, pauses (injected
  sleep — tests never really wait).
- Side effects: gzip archives under
  ``.sandbox/projects/sandbox_stress/.ck/``, FIFO retention capped at
  ``MAX_BAK_FILES``, the local ``.ck.json`` config override, and the
  final inspection panel.
- ``ck log --all`` reads the emulator's archives (production reader
  over emulator output).
- CLI wiring: ``dev emulate`` through ``main()`` in both the guarded
  and the admitted case.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cklib.emulate import (
    STRESS_MAX_BAK_FILES,
    STRESS_PROJECT,
    run_emulation,
    stress_project_dir,
)


def _isolated_sandbox_root() -> str:
    root = tempfile.mkdtemp(prefix="ck-emulate-")
    os.environ["CK_SANDBOX_ROOT"] = str(Path(root) / ".sandbox")
    return root


def _run_emulation(**overrides):
    from io import StringIO
    overrides.setdefault("pause", 0)
    overrides.setdefault("sleep", lambda _s: None)
    buf = StringIO()
    code = run_emulation(
        printer=lambda m: buf.write(m + "\n"),
        **overrides,
    )
    return code, buf.getvalue()


class TestEmulationCore(unittest.TestCase):
    def setUp(self):
        self._root = _isolated_sandbox_root()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self._root, ignore_errors=True)
        os.environ.pop("CK_SANDBOX_ROOT", None)

    def test_three_cycles_produce_gz_archives_capped_by_fifo(self):
        code, out = _run_emulation()
        self.assertEqual(code, 0)
        ck_dir = stress_project_dir() / ".ck"
        archives = sorted(
            p.name for p in ck_dir.glob("HISTORY_*.md.gz")
        )
        self.assertEqual(len(archives), STRESS_MAX_BAK_FILES)

    def test_output_contains_required_scenario_lines(self):
        code, out = _run_emulation()
        self.assertEqual(code, 0)
        for marker in (
            "Generating task batch for cycle 1",
            "Generating task batch for cycle 3",
            "HISTORY.md threshold reached",
            "Pre-archive state:",
            "[+] Rotated HISTORY.md -> ",
            ".md.gz",
            "[fifo] purged",
            "Emulation complete",
            "ck log --all",
            "ck dev clean",
        ):
            self.assertIn(marker, out)

    def test_local_config_override_written(self):
        _run_emulation(cycles=1)
        config = stress_project_dir() / ".ck" / ".ck.json"
        self.assertTrue(config.exists())
        import json
        data = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(data["HISTORY_LIMIT"], 5)
        self.assertEqual(data["MAX_BAK_FILES"], 2)
        self.assertTrue(data["COMPRESS_ARCHIVES"])

    def test_archives_are_valid_gzip_with_entries(self):
        import gzip
        _run_emulation()
        ck_dir = stress_project_dir() / ".ck"
        for p in ck_dir.glob("HISTORY_*.md.gz"):
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("### ", body)
            self.assertIn("stress task", body)

    def test_history_tail_preserved_and_appended(self):
        _run_emulation()
        history = stress_project_dir() / ".ck" / "HISTORY.md"
        text = history.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# History sandbox_stress"))
        self.assertIn("### ", text)

    def test_pause_between_steps(self):
        sleeps: list = []
        _run_emulation(cycles=2, sleep=sleeps.append)
        # Two pauses per cycle (after batch, after rotation).
        self.assertEqual(len(sleeps), 4)

    def test_log_all_reads_emulator_archives(self):
        from cklib.core import ContextKeeper
        _run_emulation()
        ck_dir = stress_project_dir() / ".ck"
        # A keeper bound at the stress project root: log --all reads
        # the archives via the production reader.
        ck = ContextKeeper(root=stress_project_dir())
        full = ck.read_full_history()
        self.assertIn("stress task", full)
        self.assertTrue(ck_dir.is_dir())


class TestEmulateGuard(unittest.TestCase):
    """``ck dev emulate`` must abort outside a sandbox context."""

    def _run_main(self, argv):
        from cklib.cli import main
        buf = None
        from io import StringIO
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def test_abort_outside_sandbox(self):
        with tempfile.TemporaryDirectory() as td:
            saved = {k: os.environ.get(k)
                     for k in ("CK_SANDBOX", "CK_DEV",
                               "CK_SANDBOX_ROOT")}
            for k in saved:
                os.environ.pop(k, None)
            try:
                with mock.patch(
                    "cklib.sandbox.sandbox_root",
                    return_value=Path(td) / ".sandbox",
                ):
                    code, out = self._run_main(["dev", "emulate"])
                self.assertEqual(code, 1)
                self.assertIn(
                    "[i] Not in sandbox mode — emulator aborted.", out
                )
                # Nothing was created.
                self.assertFalse((Path(td) / ".sandbox").exists())
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_admitted_when_sandbox_dir_exists(self):
        with tempfile.TemporaryDirectory() as td:
            saved = {k: os.environ.get(k)
                     for k in ("CK_SANDBOX", "CK_DEV",
                               "CK_SANDBOX_ROOT")}
            for k in saved:
                os.environ.pop(k, None)
            sbx = Path(td) / ".sandbox"
            sbx.mkdir()
            try:
                with mock.patch(
                    "cklib.sandbox.sandbox_root",
                    return_value=sbx,
                ), mock.patch("cklib.emulate.time.sleep"):
                    code, out = self._run_main(["dev", "emulate"])
                self.assertEqual(code, 0)
                self.assertIn("Emulation complete", out)
                self.assertTrue(
                    (sbx / "projects" / STRESS_PROJECT).is_dir()
                )
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_admitted_in_dev_mode_without_sandbox_dir(self):
        with tempfile.TemporaryDirectory() as td:
            saved = {k: os.environ.get(k)
                     for k in ("CK_SANDBOX", "CK_DEV",
                               "CK_SANDBOX_ROOT")}
            for k in saved:
                os.environ.pop(k, None)
            os.environ["CK_DEV"] = "1"
            sbx = Path(td) / ".sandbox"
            try:
                with mock.patch(
                    "cklib.sandbox.sandbox_root",
                    return_value=sbx,
                ), mock.patch("cklib.emulate.time.sleep"):
                    code, out = self._run_main(["dev", "emulate"])
                self.assertEqual(code, 0)
                self.assertIn("Emulation complete", out)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


class TestEmulateHelp(unittest.TestCase):
    def test_dev_emulate_in_help_and_usage(self):
        # The dev section of HELP_TEXT documents the emulator.
        from cklib.cli import HELP_TEXT
        self.assertIn("ck dev emulate", HELP_TEXT)

    def test_unknown_dev_action_lists_emulate(self):
        from cklib.cli import main
        buf = None
        from io import StringIO
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(["dev", "bogus"])
        self.assertEqual(code, 2)
        self.assertIn("emulate", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
