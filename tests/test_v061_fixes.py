"""Regression tests for the v0.6.1 fixes.

Covered surface:

- ``ck add`` appends new tasks with monotonically incrementing IDs and
  never overwrites existing tasks (consecutive adds).
- CLI invocations never leak a working-directory change: the caller's
  cwd is strictly restored on return and on error.
- The sandbox warning banner is padded by DISPLAY WIDTH, so every row
  and both borders share one exact terminal-column width (including
  wide Unicode and the double-width warning emoji).
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import sandbox as cksandbox
from cklib.cli import main
from cklib.config import working_directory
from cklib.core import ContextKeeper
from cklib.sandbox import render_sandbox_banner
from cklib.ui import display_width


class _IsolatedBase(unittest.TestCase):
    """Pin global config + sandbox anchor to a per-test temp dir.

    Also pins every dev/session toggle OFF so the tests are byte-stable
    whether run under pytest (conftest) or plain ``python -m unittest``.
    """

    _ENV_KEYS = (
        "CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE", "CK_SANDBOX_SHELL",
        "CK_SANDBOX_ROOT",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for key in ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                    "CK_SANDBOX_SHELL"):
            os.environ.pop(key, None)
        os.environ["CK_SANDBOX_ROOT"] = str(fake_home / ".sandbox")

        self._orig = {
            (ckconfig, n): getattr(ckconfig, n)
            for n in ("GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                      "LEGACY_GLOBAL_CONFIG_FILE")
        }
        self._orig.update({
            (ckregistry, n): getattr(ckregistry, n)
            for n in ("GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                      "LEGACY_GLOBAL_CONFIG_FILE")
        })
        new_dir = fake_home / ".config" / "ck"
        for mod in (ckconfig, ckregistry):
            mod.GLOBAL_CONFIG_DIR = new_dir
            mod.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
            mod.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"

    def tearDown(self):
        for (mod, name), value in self._orig.items():
            setattr(mod, name, value)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _fresh_project(self, name: str = "project") -> ContextKeeper:
        root = Path(self._tmp.name) / name
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.init()
        return ck


class TestConsecutiveAdd(_IsolatedBase):
    def test_core_adds_increment_ids_and_preserve_every_task(self):
        ck = self._fresh_project()
        ids = [ck.add_task(t) for t in ("alpha", "beta", "gamma")]
        self.assertEqual(ids, [1, 2, 3])

        text = ck.plan_file.read_text(encoding="utf-8")
        for title in ("alpha", "beta", "gamma"):
            self.assertEqual(text.count(f"- [ ] {title}"), 1,
                             f"{title!r} must appear exactly once:\n{text}")
        self.assertNotIn("Describe the first task", text)

    def test_cli_consecutive_adds_append_distinct_tasks(self):
        ck = self._fresh_project()
        original = os.getcwd()
        os.chdir(ck.root)
        try:
            for title in ("first", "second", "third"):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main(["add", title])
                self.assertEqual(code, 0)
        finally:
            os.chdir(original)

        text = ck.plan_file.read_text(encoding="utf-8")
        for title in ("first", "second", "third"):
            self.assertEqual(text.count(f"- [ ] {title}"), 1, text)

        tasks = ck.tasks()
        self.assertIn("first", tasks)
        self.assertIn("second", tasks)
        self.assertIn("third", tasks)


# --- section 2: working-directory guarantee -------------------------------

class TestWorkingDirectoryGuarantee(_IsolatedBase):
    def test_cli_preserves_working_directory_on_return(self):
        ck = self._fresh_project()
        original = os.getcwd()
        os.chdir(ck.root)
        try:
            before = os.getcwd()
            with redirect_stdout(io.StringIO()):
                main(["add", "keeps cwd"])
            self.assertEqual(os.getcwd(), before)
        finally:
            os.chdir(original)

    def test_cli_restores_cwd_after_internal_chdir(self):
        ck = self._fresh_project()
        original = os.getcwd()
        os.chdir(ck.root)
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir()
        real_add = ContextKeeper.add_task

        def sneaky_add(self, text):
            os.chdir(outside)  # simulate a leaking directory change
            return real_add(self, text)

        try:
            with mock.patch.object(ContextKeeper, "add_task", sneaky_add):
                with redirect_stdout(io.StringIO()):
                    main(["add", "sneaky"])
            self.assertEqual(os.getcwd(), str(ck.root))
        finally:
            os.chdir(original)

    def test_working_directory_context_restores_on_exception(self):
        original = os.getcwd()
        with self.assertRaises(RuntimeError):
            with working_directory(self._tmp.name):
                raise RuntimeError("boom")
        self.assertEqual(os.getcwd(), original)

    def test_working_directory_context_restores_on_success(self):
        original = os.getcwd()
        with working_directory(self._tmp.name):
            self.assertEqual(os.getcwd(), str(Path(self._tmp.name).resolve()))
        self.assertEqual(os.getcwd(), original)


# --- section 3: banner display width --------------------------------------

class TestBannerDisplayWidth(unittest.TestCase):
    def test_warning_emoji_is_two_columns(self):
        self.assertEqual(display_width("\u26a0\ufe0f"), 2)

    def test_all_banner_lines_share_one_display_width(self):
        out = render_sandbox_banner(color=False)
        lines = out.splitlines()
        widths = {display_width(line) for line in lines}
        self.assertEqual(len(widths), 1, f"ragged banner: {out!r}")
        # Borders are pure ASCII, so their display width equals len.
        self.assertEqual(next(iter(widths)), len(lines[0]))

    def test_banner_stays_aligned_with_wide_unicode_binary_path(self):
        wide_path = "/tmp/\u30c6\u30b9\u30c8/\u30d7\u30ed\u30b8\u30a7\u30af\u30c8/ck"
        with mock.patch.object(cksandbox, "active_binary_path",
                               return_value=Path(wide_path)):
            out = render_sandbox_banner(color=False)
        lines = out.splitlines()
        self.assertEqual(
            len({display_width(line) for line in lines}), 1,
            f"ragged wide banner: {out!r}")
        self.assertIn(wide_path, out)
        for line in lines[1:4]:
            self.assertTrue(line.startswith("\u2502") and line.endswith("\u2502"))
