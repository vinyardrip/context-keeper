"""Tests for the self-update flow and non-blocking notifier.

Covers:

- ``ContextKeeper.update()`` behaviour on non-Git directories
  (graceful abort), dirty repos (refuse), and clean repos
  (fast-forward pull via mocked Git).
- The 24-hour update notifier: notice only when expired, never
  raise on network failure, always refresh ``last_update_check``.
- Strict guard clauses: ``CK_SANDBOX=1``, ``CK_DISABLE_UPDATE_CHECK=1``
  and a missing ``.git`` repo root abort instantly (no fetch, no
  timestamp refresh).
- The ``install.sh`` shell script: default install, ``check`` and
  ``uninstall`` actions.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
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
    directory we can point ``install_dir`` at. A ``.git`` entry is
    created so the notifier's repo-root guard clause passes; tests
    that exercise the "missing .git" guard remove it explicitly.
    """

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / ".git").mkdir()
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


class TestDevModeGlobalMutationGuard(unittest.TestCase):
    """DEV-MODE GUARDRAIL: sandbox sessions never mutate global state.

    ``ck install`` / ``ck uninstall`` attempted under ``CK_SANDBOX=1``
    refuse with a clean message and never touch ``~/.local/bin/ck``
    or the package snapshot (``ck update`` already aborts in
    :class:`ContextKeeper.update`).
    """

    def _run_blocked(self, argv: list) -> tuple[int, str]:
        from cklib.cli import main
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CK_SANDBOX": "1"},
                             clear=False):
            with redirect_stdout(buf):
                code = main(argv)
        return code, buf.getvalue()

    def test_install_refused_in_dev_mode(self):
        code, out = self._run_blocked(["install"])
        self.assertEqual(code, 0)  # clean refusal, not a crash
        self.assertIn("[!] Dev mode: `install` would mutate the global",
                      out)
        self.assertIn("Sandbox sessions never touch global state", out)

    def test_uninstall_refused_in_dev_mode(self):
        code, out = self._run_blocked(["uninstall"])
        self.assertEqual(code, 0)
        self.assertIn("[!] Dev mode: `uninstall` would mutate the global",
                      out)

    def test_update_aborts_in_dev_mode(self):
        code, out = self._run_blocked(["update"])
        self.assertEqual(code, 1)
        self.assertIn("Dev mode: self-update disabled", out)


class TestSemVerParsing(unittest.TestCase):
    """SemVer contract of :func:`cklib.config.parse_version`.

    All version checks (update comparisons, notifier, tests) must
    follow the established semantic-versioning rules: three numeric
    components, dotted; unparseable input degrades to (0, 0, 0)
    rather than raising.
    """

    def test_parse_release(self):
        self.assertEqual(ckconfig.parse_version("0.3.0"), (0, 3, 0))

    def test_parse_partial(self):
        self.assertEqual(ckconfig.parse_version("1.2"), (1, 2))

    def test_parse_garbage_degrades_to_zero(self):
        self.assertEqual(ckconfig.parse_version("not-a-version"),
                         (0, 0, 0))
        self.assertEqual(ckconfig.parse_version(""), (0, 0, 0))
        self.assertEqual(ckconfig.parse_version(None), (0, 0, 0))

    def test_ordering_is_semver(self):
        self.assertLess(ckconfig.parse_version("0.2.5"),
                        ckconfig.parse_version("0.3.0"))
        self.assertLess(ckconfig.parse_version("0.3.0"),
                        ckconfig.parse_version("0.10.0"))

    def test_packaged_version_is_semver(self):
        # "cklib.config.VERSION" itself must stay SemVer-clean.
        parts = ckconfig.VERSION.split(".")
        self.assertEqual(len(parts), 3)
        for part in parts:
            self.assertTrue(part.isdigit(),
                            f"VERSION {ckconfig.VERSION!r} not SemVer")


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
        # Guarantee the notifier's env-var guards see a clean
        # environment regardless of how the suite was launched
        # (e.g. via ck-dev, which exports CK_SANDBOX=1).
        env = {k: v for k, v in os.environ.items()
               if k not in ("CK_SANDBOX", "CK_DISABLE_UPDATE_CHECK")}
        env_patch = mock.patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _expired_state(self) -> None:
        """Seed the state file with a check timestamp 25h old."""
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        _write_global_state({"last_update_check": old})

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

    # ----------------------------------------------------------------- #
    # Strict guard clauses (must abort before ANY network / state work)
    # ----------------------------------------------------------------- #

    def _assert_instant_abort(self, ck, *, expected_ts: str) -> None:
        """Assert the notifier aborts instantly: fetch_fn never runs,
        no notice is emitted, and the timestamp is NOT refreshed."""
        calls: list = []

        def spy_fetch(repo_dir):
            calls.append(repo_dir)
            return ("aaa", "bbb")

        with _StderrCapture() as err:
            emitted = ck.maybe_notify_update(fetch_fn=spy_fetch)

        self.assertFalse(emitted)
        self.assertEqual(err.value, "")
        self.assertEqual(calls, [], "fetch_fn must never be reached")
        # Guard aborts must not touch the state file at all.
        self.assertEqual(
            _read_global_state().get("last_update_check", expected_ts),
            expected_ts,
        )

    def test_sandbox_env_aborts_instantly(self):
        """CK_SANDBOX=1 must skip the check entirely."""
        self._expired_state()
        with mock.patch.dict(os.environ, {"CK_SANDBOX": "1"}), \
                _FakeRepo() as fake:
            ck = _StubCk(fake)
            old = _read_global_state()["last_update_check"]
            self._assert_instant_abort(ck, expected_ts=old)

    def test_disable_update_check_env_aborts_instantly(self):
        """CK_DISABLE_UPDATE_CHECK=1 must skip the check entirely."""
        self._expired_state()
        with mock.patch.dict(os.environ, {"CK_DISABLE_UPDATE_CHECK": "1"}), \
                _FakeRepo() as fake:
            ck = _StubCk(fake)
            old = _read_global_state()["last_update_check"]
            self._assert_instant_abort(ck, expected_ts=old)

    def test_missing_git_dir_aborts_instantly(self):
        """install_dir without a .git entry must skip the check."""
        self._expired_state()
        with _FakeRepo() as fake:
            shutil.rmtree(fake / ".git")
            self.assertFalse((fake / ".git").exists())
            ck = _StubCk(fake)
            old = _read_global_state()["last_update_check"]
            self._assert_instant_abort(ck, expected_ts=old)

    def test_fetch_runs_when_guards_pass(self):
        """With all guards satisfied, fetch_fn IS reached (sanity
        check that the guards above do not over-suppress)."""
        self._expired_state()
        calls: list = []

        def spy_fetch(repo_dir):
            calls.append(repo_dir)
            return ("aaa", "bbb")

        with _FakeRepo() as fake, _StderrCapture():
            ck = _StubCk(fake)
            emitted = ck.maybe_notify_update(fetch_fn=spy_fetch)

        self.assertTrue(emitted)
        self.assertEqual(calls, [fake])


# ---------------------------------------------------------------------------
# ck update → installed-copy refresh (static-snapshot contract)
# ---------------------------------------------------------------------------


class TestUpdateRefreshesInstalledCopy(unittest.TestCase):
    """After a successful `ck update`, an EXISTING physical install is
    re-snapshotted; when nothing is installed the update stays a pure
    checkout pull (no surprise installation).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._bin = Path(self._tmp.name) / "bin"
        self._ck_target = self._bin / "ck"
        self._snapshot = Path(self._tmp.name) / "share" / "ck"

        # Global state writes land in the temp dir.
        fake_state = Path(self._tmp.name) / "state.json"
        self._orig_state_cfg = ckconfig.GLOBAL_STATE_FILE
        self._orig_state_core = ckcore.GLOBAL_STATE_FILE
        ckconfig.GLOBAL_STATE_FILE = fake_state
        ckcore.GLOBAL_STATE_FILE = fake_state
        self.addCleanup(self._restore_state)

        for target, value in (("cklib.cli.USER_INSTALL_PATH",
                               self._ck_target),
                              ("cklib.cli.SNAPSHOT_INSTALL_DIR",
                               self._snapshot),
                              ("cklib.core.USER_INSTALL_PATH",
                               self._ck_target),
                              ("cklib.core.SNAPSHOT_INSTALL_DIR",
                               self._snapshot)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore_state(self):
        ckconfig.GLOBAL_STATE_FILE = self._orig_state_cfg
        ckcore.GLOBAL_STATE_FILE = self._orig_state_core

    def _install(self):
        from cklib.cli import main as cli_main

        buf = io.StringIO()
        with mock.patch("sys.argv", ["ck", "install"]), \
                redirect_stdout(buf):
            self.assertEqual(cli_main(["install"]), 0)

    def _run_update(self) -> tuple:
        from cklib.cli import main as cli_main

        buf = io.StringIO()
        with mock.patch("sys.argv", ["ck", "update"]), \
                redirect_stdout(buf), \
                mock.patch.object(gith, "is_git_repo", return_value=True), \
                mock.patch.object(gith, "is_dirty", return_value=False), \
                mock.patch.object(gith, "fetch", return_value=True), \
                mock.patch.object(gith, "pull_ff_only",
                                  return_value=(True, "Updated to origin/main.")):
            code = cli_main(["update"])
        return code, buf.getvalue()

    def test_update_reinstall_snapshot_when_installed(self):
        self._install()
        # Mark the installed snapshot STALE.
        snap_cli = self._snapshot / "cklib" / "cli.py"
        snap_cli.write_text(
            snap_cli.read_text(encoding="utf-8") + "\nSTALE = True\n",
            encoding="utf-8",
        )

        code, out = self._run_update()
        self.assertEqual(code, 0, out)
        self.assertIn("Refreshing installed production copy", out)
        # The snapshot was rewritten from the checkout: STALE is gone.
        self.assertNotIn("STALE", snap_cli.read_text(encoding="utf-8"))
        # The launcher copy is still a regular file.
        self.assertTrue(self._ck_target.is_file())
        self.assertFalse(self._ck_target.is_symlink())

    def test_update_without_install_pulls_only(self):
        code, out = self._run_update()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Refreshing", out)
        self.assertFalse(self._ck_target.exists())
        self.assertFalse(self._snapshot.exists())


# ---------------------------------------------------------------------------
# Read-only command isolation (no notifier on the st/tasks/info path)
# ---------------------------------------------------------------------------


class TestReadOnlyCommandIsolation(unittest.TestCase):
    """Read-only commands must never reach the update notifier."""

    def setUp(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CK_SANDBOX", "CK_DISABLE_UPDATE_CHECK")}
        env_patch = mock.patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_read_only_commands_are_not_notifiable(self):
        from cklib.cli import _NOTIFIER_COMMANDS
        for cmd in ("st", "list", "dashboard", "log",
                    "info", "help", "version", "prune"):
            self.assertNotIn(cmd, _NOTIFIER_COMMANDS)
        for cmd in ("done", "save"):
            self.assertIn(cmd, _NOTIFIER_COMMANDS)

    def test_st_never_invokes_notifier(self):
        """`ck st` dispatch must not call maybe_notify_update."""
        from cklib import cli

        with mock.patch.object(cli, "_maybe_notify") as notify, \
                mock.patch("cklib.core.ContextKeeper") as ck_cls:
            ck_cls.return_value.status.return_value = "status"
            cli.main(["st"])
        notify.assert_not_called()
        ck_cls.return_value.maybe_notify_update.assert_not_called()


# ---------------------------------------------------------------------------
# Hardened ls-remote probe (git.py)
# ---------------------------------------------------------------------------


class TestLsRemoteHardening(unittest.TestCase):
    """git.ls_remote must fail silently under a 0.8s hard timeout."""

    def test_default_timeout_is_sub_second(self):
        import inspect
        sig = inspect.signature(gith.ls_remote)
        self.assertEqual(sig.parameters["timeout"].default, 0.8)

    def _completed(self, stdout: str = "", returncode: int = 0):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr="",
        )

    def test_returns_sha_on_success(self):
        with mock.patch.object(gith, "_resolve_git", return_value="/usr/bin/git"), \
                mock.patch.object(gith.subprocess, "run",
                                  return_value=self._completed(
                                      "abc123\trefs/heads/main\n")):
            self.assertEqual(gith.ls_remote("origin", "main"), "abc123")

    def test_timeout_fails_silently(self):
        """subprocess.TimeoutExpired must be swallowed → None, no raise."""
        def raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="git ls-remote", timeout=0.8)

        with mock.patch.object(gith, "_resolve_git", return_value="/usr/bin/git"), \
                mock.patch.object(gith.subprocess, "run",
                                  side_effect=raise_timeout):
            self.assertIsNone(gith.ls_remote("origin", "main"))

    def test_no_git_binary_returns_none(self):
        with mock.patch.object(gith, "_resolve_git", return_value=None):
            self.assertIsNone(gith.ls_remote("origin", "main"))

    def test_option_like_refspec_rejected(self):
        """Injection guard: option-like remote/branch never spawn git."""
        with mock.patch.object(gith, "_resolve_git", return_value="/usr/bin/git"), \
                mock.patch.object(gith.subprocess, "run") as run:
            self.assertIsNone(gith.ls_remote("--upload-pack=evil", "main"))
            self.assertIsNone(gith.ls_remote("origin", "--exec=evil"))
        run.assert_not_called()

    def test_failed_command_returns_none(self):
        with mock.patch.object(gith, "_resolve_git", return_value="/usr/bin/git"), \
                mock.patch.object(gith.subprocess, "run",
                                  return_value=self._completed(
                                      returncode=128)):
            self.assertIsNone(gith.ls_remote("origin", "main"))


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

    def test_install_creates_physical_copy(self):
        if not _INSTALL_SH.exists():
            self.skipTest(f"install.sh not present at {_INSTALL_SH}")
        target = Path(self._tmp_home.name) / ".local" / "bin" / "ck"
        snapshot = (Path(self._tmp_home.name) / ".local" / "share" / "ck"
                    / "cklib" / "__init__.py")
        result = _run_install_sh()
        self.assertEqual(result.returncode, 0,
                         f"stderr: {result.stderr}")
        # Physical copy: a REGULAR executable file, never a symlink.
        self.assertTrue(target.is_file(),
                        f"expected regular file at {target}")
        self.assertFalse(target.is_symlink(),
                         f"expected no symlink at {target}")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        # The cklib package snapshot ships with it.
        self.assertTrue(snapshot.is_file(), "cklib snapshot missing")

    def test_install_replaces_existing_symlink(self):
        if not _INSTALL_SH.exists():
            self.skipTest("install.sh not present")
        target = Path(self._tmp_home.name) / ".local" / "bin" / "ck"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(_REPO_ROOT / "ck")
        result = _run_install_sh()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())

    def test_install_idempotent(self):
        if not _INSTALL_SH.exists():
            self.skipTest("install.sh not present")
        first = _run_install_sh()
        second = _run_install_sh()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        target = Path(self._tmp_home.name) / ".local" / "bin" / "ck"
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())

    def test_check_prints_status(self):
        if not _INSTALL_SH.exists():
            self.skipTest("install.sh not present")
        result = _run_install_sh("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Should mention Python, git, EDITOR.
        out = (result.stdout + result.stderr).lower()
        self.assertTrue("python" in out or "git" in out,
                        f"missing diagnostic: {result.stdout}")

    def test_uninstall_removes_install_and_snapshot(self):
        if not _INSTALL_SH.exists():
            self.skipTest("install.sh not present")
        _run_install_sh()  # install
        home = Path(self._tmp_home.name)
        target = home / ".local" / "bin" / "ck"
        legacy_dev = home / ".local" / "bin" / "ck-dev"
        snapshot = home / ".local" / "share" / "ck"
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        # Legacy dev leftover is cleaned up too.
        legacy_dev.symlink_to(_REPO_ROOT / "ck-dev")
        result = _run_install_sh("uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())
        self.assertFalse(target.is_symlink())
        self.assertFalse(legacy_dev.exists())
        self.assertFalse(snapshot.exists())

    def test_uninstall_when_not_installed_is_graceful(self):
        if not _INSTALL_SH.exists():
            self.skipTest("install.sh not present")
        result = _run_install_sh("uninstall")
        self.assertEqual(result.returncode, 0,
                         f"stderr: {result.stderr}")


if __name__ == "__main__":
    unittest.main()