"""Tests for the self-update flow and non-blocking notifier.

Covers:

- ``ContextKeeper.update()`` behaviour on non-Git directories
  (graceful abort), dirty repos (refuse), and clean repos
  (fast-forward pull via mocked Git).
- The 24-hour update notifier: notice only when expired, never
  raise on network failure, always refresh ``last_update_check``.
- The ``install.sh`` shell script: default install, ``check`` and
  ``uninstall`` actions.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import core as ckcore
from cklib import git as gith
from cklib.core import ContextKeeper, UpdateResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRepo:
    """Context manager that creates a fake Git repository directory.

    Not actually a real Git repo (we mock the git module); just a
    directory we can point ``install_dir`` at.
    """

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        return self.path

    def __exit__(self, *exc):
        self.tmp.cleanup()


class _StubCk(ContextKeeper):
    """A ``ContextKeeper`` whose ``install_dir`` points at a stub path."""

    def __init__(self, install_dir: Path):
        # Skip ContextKeeper.__init__ — we don't need the project paths
        self._install_dir = install_dir

    @property
    def install_dir(self) -> Path:
        return self._install_dir


def _write_global_state(state: dict) -> None:
    """Write a state file at the canonical global path."""
    ckconfig.GLOBAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ckconfig.GLOBAL_STATE_FILE.write_text(
        json.dumps(state), encoding="utf-8",
    )


def _read_global_state() -> dict:
    if not ckconfig.GLOBAL_STATE_FILE.exists():
        return {}
    try:
        return json.loads(ckconfig.GLOBAL_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# ck update — error paths
# ---------------------------------------------------------------------------


class TestUpdateNonGitDirectory(unittest.TestCase):
    def test_non_git_install_dir_aborts_gracefully(self):
        """If install_dir is not a Git work tree, abort with the
        exact message required by the spec."""
        with _FakeRepo() as fake:
            # Ensure git.is_git_repo returns False by faking the
            # "no git" condition. We patch the helper instead.
            with mock.patch.object(gith, "is_git_repo", return_value=False):
                ck = _StubCk(fake)
                result = ck.update()
        self.assertFalse(result.ok)
        self.assertIn("Error: ContextKeeper was not installed via Git.",
                      result.message)
        self.assertEqual(result.action, "abort")


class TestUpdateDirtyRepo(unittest.TestCase):
    def test_dirty_repo_aborts_before_fetch(self):
        """If `git status --porcelain` shows anything, refuse to fetch."""
        with _FakeRepo() as fake:
            fetch_called = False

            def fake_fetch(*a, **kw):
                nonlocal fetch_called
                fetch_called = True
                return True

            with mock.patch.object(gith, "is_git_repo", return_value=True), \
                 mock.patch.object(gith, "is_dirty", return_value=True), \
                 mock.patch.object(gith, "fetch", side_effect=fake_fetch):
                ck = _StubCk(fake)
                result = ck.update()

        self.assertFalse(result.ok)
        self.assertIn("Local changes detected.", result.message)
        self.assertIn("stash or commit", result.message)
        self.assertFalse(fetch_called)
        self.assertEqual(result.action, "abort")


# ---------------------------------------------------------------------------
# ck update — fast-forward pull with mocks
# ---------------------------------------------------------------------------


class TestUpdateFastForward(unittest.TestCase):
    def test_clean_repo_fetches_and_pulls(self):
        with _FakeRepo() as fake:
            fetch_calls: list[tuple] = []
            pull_calls: list[tuple] = []

            def track_fetch(remote, branch, path=None, **kwargs):
                fetch_calls.append((remote, branch, path))
                return True

            def track_pull(remote, branch, path=None, **kwargs):
                pull_calls.append((remote, branch, path))
                return (True, "Already up to date.")

            with mock.patch.object(gith, "is_git_repo", return_value=True), \
                 mock.patch.object(gith, "is_dirty", return_value=False), \
                 mock.patch.object(gith, "fetch", side_effect=track_fetch), \
                 mock.patch.object(gith, "pull_ff_only",
                                   side_effect=track_pull):
                ck = _StubCk(fake)
                result = ck.update()

        self.assertTrue(result.ok)
        self.assertEqual(result.action, "updated")
        self.assertEqual(fetch_calls, [("origin", "main", fake)])
        self.assertEqual(pull_calls, [("origin", "main", fake)])

    def test_fetch_failure_returns_error(self):
        with _FakeRepo() as fake:
            pull_called = False

            def fake_pull(*a, **kw):
                nonlocal pull_called
                pull_called = True
                return (True, "")

            with mock.patch.object(gith, "is_git_repo", return_value=True), \
                 mock.patch.object(gith, "is_dirty", return_value=False), \
                 mock.patch.object(gith, "fetch", return_value=False), \
                 mock.patch.object(gith, "pull_ff_only",
                                   side_effect=fake_pull):
                ck = _StubCk(fake)
                result = ck.update()

        self.assertFalse(result.ok)
        self.assertEqual(result.action, "fetch-failed")
        self.assertFalse(pull_called)

    def test_pull_failure_returns_error(self):
        with _FakeRepo() as fake:
            with mock.patch.object(gith, "is_git_repo", return_value=True), \
                 mock.patch.object(gith, "is_dirty", return_value=False), \
                 mock.patch.object(gith, "fetch", return_value=True), \
                 mock.patch.object(gith, "pull_ff_only",
                                   return_value=(False, "conflict")):
                ck = _StubCk(fake)
                result = ck.update()

        self.assertFalse(result.ok)
        self.assertEqual(result.action, "pull-failed")
        self.assertIn("conflict", result.message)

    def test_update_writes_global_state_timestamp(self):
        """A successful update must persist ``last_update_check``."""
        with tempfile.TemporaryDirectory() as td:
            fake_state = Path(td) / "state.json"
            orig_cfg = ckconfig.GLOBAL_STATE_FILE
            orig_core = ckcore.GLOBAL_STATE_FILE
            ckconfig.GLOBAL_STATE_FILE = fake_state
            ckcore.GLOBAL_STATE_FILE = fake_state
            try:
                with _FakeRepo() as fake, \
                     mock.patch.object(gith, "is_git_repo", return_value=True), \
                     mock.patch.object(gith, "is_dirty", return_value=False), \
                     mock.patch.object(gith, "fetch", return_value=True), \
                     mock.patch.object(gith, "pull_ff_only",
                                       return_value=(True, "ok")):
                    ck = _StubCk(fake)
                    result = ck.update()
            finally:
                ckconfig.GLOBAL_STATE_FILE = orig_cfg
                ckcore.GLOBAL_STATE_FILE = orig_core

            self.assertTrue(result.ok)
            data = json.loads(fake_state.read_text(encoding="utf-8"))
            self.assertIn("last_update_check", data)
            self.assertIn("last_update_success", data)


# ---------------------------------------------------------------------------
# Non-blocking daily update notifier
# ---------------------------------------------------------------------------


class _StderrCapture:
    def __enter__(self):
        import sys
        self._buf = io.StringIO()
        self._old = sys.stderr
        sys.stderr = self._buf
        return self

    def __exit__(self, *exc):
        import sys
        sys.stderr = self._old

    @property
    def value(self) -> str:
        return self._buf.getvalue()


class TestUpdateNotifier(unittest.TestCase):
    def setUp(self):
        # Redirect BOTH the config module and the core module's view
        # of the global state path.
        self._tmp_state = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_state.cleanup)
        self._fake_state = Path(self._tmp_state.name) / "state.json"
        self._orig_state_cfg = ckconfig.GLOBAL_STATE_FILE
        self._orig_state_core = ckcore.GLOBAL_STATE_FILE
        ckconfig.GLOBAL_STATE_FILE = self._fake_state
        ckcore.GLOBAL_STATE_FILE = self._fake_state
        def _restore():
            ckconfig.GLOBAL_STATE_FILE = self._orig_state_cfg
            ckcore.GLOBAL_STATE_FILE = self._orig_state_core
        self.addCleanup(_restore)

    def test_emits_notice_when_expired(self):
        # 25h ago → expired.
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        _write_global_state({"last_update_check": old})

        with _FakeRepo() as fake, \
             _StderrCapture() as err, \
             mock.patch.object(ckcore, "_default_remote_head_check",
                               return_value=("aaa", "bbb")):
            ck = _StubCk(fake)
            emitted = ck.maybe_notify_update()

        self.assertTrue(emitted)
        self.assertIn("Notice: A new version of ck is available.", err.value)
        self.assertIn("ck update", err.value)

    def test_no_notice_when_fresh(self):
        # 1h ago → not yet expired.
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_global_state({"last_update_check": recent})

        with _FakeRepo() as fake, \
             _StderrCapture() as err, \
             mock.patch.object(ckcore, "_default_remote_head_check",
                               return_value=("aaa", "bbb")):
            ck = _StubCk(fake)
            emitted = ck.maybe_notify_update()

        self.assertFalse(emitted)
        self.assertEqual(err.value, "")

    def test_no_notice_when_up_to_date(self):
        # 25h ago → expired, but local == remote.
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        _write_global_state({"last_update_check": old})

        with _FakeRepo() as fake, \
             _StderrCapture() as err, \
             mock.patch.object(ckcore, "_default_remote_head_check",
                               return_value=("same", "same")):
            ck = _StubCk(fake)
            emitted = ck.maybe_notify_update()

        self.assertFalse(emitted)
        self.assertEqual(err.value, "")

    def test_timestamp_refreshed_on_failure(self):
        """The timestamp is updated even when the network check fails."""
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        _write_global_state({"last_update_check": old})

        with _FakeRepo() as fake, \
             _StderrCapture() as err, \
             mock.patch.object(ckcore, "_default_remote_head_check",
                               return_value=None):
            ck = _StubCk(fake)
            emitted = ck.maybe_notify_update()

        self.assertFalse(emitted)
        # Timestamp must have been refreshed.
        new_state = _read_global_state()
        self.assertIn("last_update_check", new_state)
        self.assertNotEqual(new_state["last_update_check"], old)

    def test_does_not_raise_on_network_error(self):
        """The notifier must never raise — even on unexpected errors."""
        with _FakeRepo() as fake, \
             _StderrCapture():
            def boom(*a, **kw):
                raise RuntimeError("network boom")
            with mock.patch.object(ckcore, "_default_remote_head_check",
                                   side_effect=boom):
                ck = _StubCk(fake)
                # Must not raise.
                emitted = ck.maybe_notify_update()
        self.assertFalse(emitted)


# ---------------------------------------------------------------------------
# install.sh
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent
_INSTALL_SH = _REPO_ROOT / "install.sh"


def _run_install_sh(*args: str) -> subprocess.CompletedProcess:
    """Run install.sh with the given args and return the CompletedProcess."""
    return subprocess.run(
        ["bash", str(_INSTALL_SH), *args],
        capture_output=True, text=True, timeout=30,
    )


class TestInstallSh(unittest.TestCase):
    def setUp(self):
        # Use a fake HOME so we don't touch the user's real ~/.local/bin.
        self._tmp_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_home.cleanup)
        self._orig_home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp_home.name
        self.addCleanup(self._restore_home)
        # The script may need git/python; both are guaranteed here.

    def _restore_home(self):
        if self._orig_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._orig_home

    def test_install_creates_symlink(self):
        if not _INSTALL_SH.exists():
            self.skipTest(f"install.sh not present at {_INSTALL_SH}")
        target = Path(self._tmp_home.name) / ".local" / "bin" / "ck"
        result = _run_install_sh()
        self.assertEqual(result.returncode, 0,
                         f"stderr: {result.stderr}")
        self.assertTrue(target.is_symlink(),
                        f"expected symlink at {target}")
        # Symlink should point to the entry script in the repo.
        self.assertTrue(str(target.resolve()).endswith("ck"))

    def test_install_idempotent(self):
        if not _INSTALL_SH.exists():
            self.skipTest("install.sh not present")
        first = _run_install_sh()
        second = _run_install_sh()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_check_prints_status(self):
        if not _INSTALL_SH.exists():
            self.skipTest("install.sh not present")
        result = _run_install_sh("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Should mention Python, git, EDITOR.
        out = (result.stdout + result.stderr).lower()
        self.assertTrue("python" in out or "git" in out,
                        f"missing diagnostic: {result.stdout}")

    def test_uninstall_removes_symlink(self):
        if not _INSTALL_SH.exists():
            self.skipTest("install.sh not present")
        _run_install_sh()  # install
        target = Path(self._tmp_home.name) / ".local" / "bin" / "ck"
        self.assertTrue(target.is_symlink())
        result = _run_install_sh("uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())
        self.assertFalse(target.is_symlink())

    def test_uninstall_when_not_installed_is_graceful(self):
        if not _INSTALL_SH.exists():
            self.skipTest("install.sh not present")
        result = _run_install_sh("uninstall")
        self.assertEqual(result.returncode, 0,
                         f"stderr: {result.stderr}")


if __name__ == "__main__":
    unittest.main()