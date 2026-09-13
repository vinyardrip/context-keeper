"""Tests for the editor resolution hierarchy (Step 5).

Strict precedence:

1. Project config (``.ck.json`` -> ``"editor"``) at the project root
2. ``$VISUAL`` environment variable
3. ``$EDITOR`` environment variable
4. System fallback (``nano`` if available, else ``vi``)
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from cklib.config import get_editor


def _fallbacks_available() -> dict[str, bool]:
    return {name: shutil.which(name) is not None for name in ("nano", "vi")}


class _Chdir:
    """Context manager: run inside a directory (no .ck created)."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self._orig_cwd = None

    def __enter__(self) -> Path:
        self.cwd.mkdir(parents=True, exist_ok=True)
        self._orig_cwd = Path.cwd()
        os.chdir(self.cwd)
        return self.cwd

    def __exit__(self, *exc) -> None:
        os.chdir(self._orig_cwd)


class _ChdirProject(_Chdir):
    """Context manager: run inside a temp project with ``.ck/``."""

    def __enter__(self) -> Path:
        super().__enter__()
        (self.cwd / ".ck").mkdir(parents=True, exist_ok=True)
        return self.cwd


class TestEditorResolutionHierarchy(unittest.TestCase):
    """Each level of the hierarchy overrides the ones below it."""

    def setUp(self):
        # Freeze env: no VISUAL/EDITOR leakage from the test runner.
        self._saved = {k: os.environ.pop(k, None) for k in ("VISUAL", "EDITOR")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    # ---- Level 1: project config ------------------------------- #

    def test_project_config_overrides_everything(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            root.mkdir()
            (root / ".ck.json").write_text(
                json.dumps({"editor": "proj-editor"}), encoding="utf-8"
            )
            with _ChdirProject(root):
                editor = get_editor(env={"VISUAL": "visual-ed", "EDITOR": "edit-ed"})
        self.assertEqual(editor, "proj-editor")

    def test_project_config_with_arguments(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            root.mkdir()
            (root / ".ck.json").write_text(
                json.dumps({"editor": "code --wait"}), encoding="utf-8"
            )
            with _ChdirProject(root):
                editor = get_editor(env={})
        self.assertEqual(editor, "code --wait")

    def test_empty_editor_key_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            root.mkdir()
            (root / ".ck.json").write_text(
                json.dumps({"editor": "   "}), encoding="utf-8"
            )
            with _ChdirProject(root):
                editor = get_editor(env={"VISUAL": "visual-ed"})
        self.assertEqual(editor, "visual-ed")

    def test_malformed_project_config_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            root.mkdir()
            (root / ".ck.json").write_text("{ not json !!", encoding="utf-8")
            with _ChdirProject(root):
                editor = get_editor(env={"EDITOR": "edit-ed"})
        self.assertEqual(editor, "edit-ed")

    def test_non_string_editor_value_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            root.mkdir()
            (root / ".ck.json").write_text(
                json.dumps({"editor": 42}), encoding="utf-8"
            )
            with _ChdirProject(root):
                editor = get_editor(env={"EDITOR": "edit-ed"})
        self.assertEqual(editor, "edit-ed")

    def test_no_ck_json_falls_through_to_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            root.mkdir()
            with _ChdirProject(root):
                editor = get_editor(env={"VISUAL": "visual-ed", "EDITOR": "edit-ed"})
        self.assertEqual(editor, "visual-ed")

    # ---- Level 2/3: VISUAL beats EDITOR ------------------------- #

    def test_visual_beats_editor(self):
        editor = get_editor(env={"VISUAL": "visual-ed", "EDITOR": "edit-ed"})
        self.assertEqual(editor, "visual-ed")

    def test_editor_used_when_visual_unset(self):
        editor = get_editor(env={"EDITOR": "edit-ed"})
        self.assertEqual(editor, "edit-ed")

    def test_editor_used_when_visual_empty(self):
        editor = get_editor(env={"VISUAL": "  ", "EDITOR": "edit-ed"})
        self.assertEqual(editor, "edit-ed")

    def test_values_are_stripped(self):
        editor = get_editor(env={"EDITOR": "  nano-wrapper  "})
        self.assertEqual(editor, "nano-wrapper")

    # ---- Level 4: system fallback -------------------------------- #

    def test_fallback_nano_or_vi_when_nothing_set(self):
        # Run from a directory with no .ck.json above it either.
        with tempfile.TemporaryDirectory() as td:
            with _ChdirProject(Path(td)):
                editor = get_editor(env={})
        avail = _fallbacks_available()
        if avail["nano"]:
            self.assertEqual(editor, "nano")
        else:
            self.assertEqual(editor, "vi")

    def test_fallback_is_never_micro(self):
        """The old fallback chain included `micro`; the new spec is
        strictly nano -> vi."""
        with tempfile.TemporaryDirectory() as td:
            with _ChdirProject(Path(td)):
                editor = get_editor(env={})
        self.assertIn(editor, ("nano", "vi"))

    # ---- Walk-up discovery -------------------------------------- #

    def test_config_found_from_subdirectory(self):
        """`.ck.json` at the project root is found from a nested cwd."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            (root / ".ck").mkdir(parents=True)
            (root / ".ck.json").write_text(
                json.dumps({"editor": "root-editor"}), encoding="utf-8"
            )
            sub = root / "src" / "deep"
            with _Chdir(sub):
                editor = get_editor(env={"EDITOR": "edit-ed"})
        self.assertEqual(editor, "root-editor")

    def test_real_environ_used_when_env_not_injected(self):
        """Calling get_editor() with defaults resolves against the
        actual os.environ + cwd without raising."""
        os.environ["EDITOR"] = "unit-test-editor"
        try:
            with tempfile.TemporaryDirectory() as td:
                with _ChdirProject(Path(td)):
                    editor = get_editor()
        finally:
            del os.environ["EDITOR"]
        self.assertEqual(editor, "unit-test-editor")


if __name__ == "__main__":
    unittest.main()
