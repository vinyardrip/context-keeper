"""Tests for Step 4.1: automated sandbox infrastructure.

Covers:

- ``ck sandbox setup`` / ``ck-dev sandbox setup`` builds the complete
  fixture layout under ``.sandbox/`` (registry mock, alpha bulk data,
  unregistered beta, plain gamma, registry-only orphaned-deleted).
- Bulk data volumes: 100+ ``history.log`` entries, multiple mock
  archives (``.ck/archive/`` + legacy ``.ck/*.md.bak``), HISTORY.md
  exactly at the rotation limit.
- STRICT host isolation: setup never touches the (patched) global
  config dir; dev-mode reads/writes after setup never touch it
  either.
- Dev-mode registry read-your-writes: with the fixture registry in
  place, ``dashboard`` shows ``alpha`` and ``[MISSING] orphaned-deleted``;
  without it, reads still come from the real registry (Step 3
  semantics preserved).
- ``ck prune`` / standalone ``ck register`` operate on the SANDBOX
  registry only.
- ``ck sandbox clean`` removes the sandbox; CLI usage errors.
- Version bump to 0.2.4.
"""

from __future__ import annotations

# Filesystem isolation safety net: importing the tests package
# pins CK_SANDBOX_ROOT to an OS-temp directory (see tests/__init__),
# so no test in this module can create or wipe the repository's own
# .sandbox/ — under ANY runner, including bare `unittest discover`.
import tests  # noqa: F401

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import sandbox
from cklib.cli import main
from cklib.config import HISTORY_LIMIT
from cklib.core import ContextKeeper, _count_history_entries
from cklib.parser import parse_plan_file
from cklib.sandbox import sandbox_root
from cklib.sandbox_setup import (
    ALPHA_PROJECT,
    ARCHIVE_FILE_COUNT,
    BULK_HISTORY_LOG_ENTRIES,
    LEGACY_ARCHIVE_COUNT,
    setup_sandbox,
)

from tests._isolated_checkout import make_checkout_copy


REPO_ROOT = Path(__file__).resolve().parent.parent


class _SandboxFixtureBase(unittest.TestCase):
    """Deterministic sandbox-fixture environment.

    Mirrors Step 3's ``_DevModeBase``: fresh ``.sandbox/`` per test,
    CK_* toggles popped, ``sys.argv`` pinned, global registry/config
    redirected into a per-test temp HOME (the "host" whose isolation
    we assert).
    """

    def setUp(self):
        sandbox.clean_sandbox(quiet=True)
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in ("CK_SANDBOX", "CK_DEV", "CK_DEBUG")
        }
        self._saved_argv = sandbox.sys.argv
        sandbox.sys.argv = ["ck"]

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._orig_dir = ckconfig.GLOBAL_CONFIG_DIR
        self._orig_file = ckconfig.GLOBAL_REGISTRY_FILE
        self._orig_state = ckconfig.GLOBAL_STATE_FILE
        self._orig_legacy = ckconfig.LEGACY_GLOBAL_CONFIG_FILE

        new_dir = fake_home / ".config" / "ck"
        ckconfig.GLOBAL_CONFIG_DIR = new_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
        ckconfig.GLOBAL_STATE_FILE = new_dir / "state.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"
        ckregistry.GLOBAL_CONFIG_DIR = new_dir
        ckregistry.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
        ckregistry.GLOBAL_STATE_FILE = new_dir / "state.json"
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"

        def _restore_config():
            ckconfig.GLOBAL_CONFIG_DIR = self._orig_dir
            ckconfig.GLOBAL_REGISTRY_FILE = self._orig_file
            ckconfig.GLOBAL_STATE_FILE = self._orig_state
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
            ckregistry.GLOBAL_CONFIG_DIR = self._orig_dir
            ckregistry.GLOBAL_REGISTRY_FILE = self._orig_file
            ckregistry.GLOBAL_STATE_FILE = self._orig_state
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE = self._orig_legacy
        self.addCleanup(_restore_config)

    def tearDown(self):
        sandbox.clean_sandbox(quiet=True)
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        sandbox.sys.argv = self._saved_argv

    # ---- helpers ----------------------------------------------------- #

    @property
    def host_config_dir(self) -> Path:
        return Path(self._tmp.name) / ".config" / "ck"

    def fixture_registry(self) -> dict:
        target = sandbox_root() / "config" / "projects.json"
        self.assertTrue(target.exists(), "fixture registry missing")
        return json.loads(target.read_text(encoding="utf-8"))


class TestSetupFixtures(_SandboxFixtureBase):
    """`setup_sandbox()` builds the complete Step 4.1 layout."""

    def test_setup_creates_full_layout(self):
        self.assertTrue(setup_sandbox(printer=lambda *_: None))
        root = sandbox_root()
        expected = [
            root / "config" / "projects.json",
            root / "projects" / "alpha" / ".ck" / "PLAN.md",
            root / "projects" / "alpha" / ".ck" / "HISTORY.md",
            root / "projects" / "alpha" / ".ck" / "history.log",
            root / "projects" / "alpha" / ".ck" / "archive",
            root / "projects" / "alpha" / ".ck" / "prompt.md",
            root / "projects" / "alpha" / ".ck" / "README.md",
            root / "projects" / "alpha" / ".ck" / ".gitignore",
            root / "projects" / "alpha" / ".ck" / "state.json",
            root / "projects" / "beta" / ".ck" / "PLAN.md",
            root / "projects" / "beta" / ".ck" / "state.json",
            root / "projects" / "gamma",
        ]
        for p in expected:
            self.assertTrue(p.exists(), f"missing fixture: {p}")

    def test_registry_mock_registers_alpha_and_orphaned(self):
        setup_sandbox(printer=lambda *_: None)
        data = self.fixture_registry()
        names = {p["name"] for p in data["projects"]}
        self.assertEqual(names, {"alpha", "orphaned-deleted"})
        # beta/gamma must NOT be registered.
        self.assertNotIn("beta", names)
        self.assertNotIn("gamma", names)

    def test_registry_paths_absolute_inside_sandbox(self):
        setup_sandbox(printer=lambda *_: None)
        data = self.fixture_registry()
        for entry in data["projects"]:
            p = Path(entry["path"])
            self.assertTrue(p.is_absolute())
            self.assertTrue(
                str(p).startswith(str(sandbox_root())),
                f"registry path escapes sandbox: {p}",
            )

    def test_orphaned_registered_but_missing_on_disk(self):
        setup_sandbox(printer=lambda *_: None)
        data = self.fixture_registry()
        by_name = {p["name"]: Path(p["path"]) for p in data["projects"]}
        self.assertTrue(by_name["alpha"].is_dir())
        self.assertFalse(by_name["orphaned-deleted"].exists())

    def test_alpha_plan_tasks_and_gap(self):
        setup_sandbox(printer=lambda *_: None)
        plan = (sandbox_root() / "projects" / "alpha" / ".ck"
                / "PLAN.md")
        tl = parse_plan_file(plan)
        # 6 tasks; no "task 5" title — the intentional gap at 5.
        self.assertEqual(tl.total, 6)
        for t in tl.tasks:
            self.assertNotIn("task 5", t.title)
        # Statuses: [x] [x] [>] [ ] [ ] [ ]
        statuses = [t.status.name for t in tl.tasks]
        self.assertEqual(
            statuses,
            ["DONE", "DONE", "FOCUSED", "OPEN", "OPEN", "OPEN"],
        )
        # Focus is task 3 (matches the registry active_task_id).
        self.assertEqual(tl.focused[0].id, 3)
        self.assertEqual(tl.focused[0].title, "Active focus task")
        # Gap detection fires: open tasks follow the done block.
        self.assertTrue(tl.gap_ids())

    def test_history_log_bulk_entries(self):
        setup_sandbox(printer=lambda *_: None)
        log = (sandbox_root() / "projects" / "alpha" / ".ck"
               / "history.log")
        entries = [
            ln for ln in log.read_text(encoding="utf-8").splitlines()
            if ln and not ln.startswith("#")
        ]
        self.assertGreaterEqual(len(entries), 100)
        self.assertEqual(len(entries), BULK_HISTORY_LOG_ENTRIES)
        # The task-numbering gap is preserved in the log refs.
        self.assertNotIn("[5]", log.read_text(encoding="utf-8"))

    def test_archive_directory_populated(self):
        setup_sandbox(printer=lambda *_: None)
        archive_dir = (sandbox_root() / "projects" / "alpha" / ".ck"
                       / "archive")
        archives = sorted(archive_dir.glob("HISTORY_*.md.bak"))
        self.assertEqual(len(archives), ARCHIVE_FILE_COUNT)
        # Archives are parseable HISTORY.md documents (entry count
        # below/at/above the limit for boundary testing).
        counts = {
            _count_history_entries(p.read_text(encoding="utf-8"))
            for p in archives
        }
        self.assertIn(HISTORY_LIMIT, counts)      # at limit
        self.assertTrue(any(c > HISTORY_LIMIT for c in counts))  # over
        self.assertTrue(any(c < HISTORY_LIMIT for c in counts))  # under

    def test_legacy_archives_in_ck_root(self):
        """Legacy rotation artifacts (``.ck/HISTORY_*.md.bak``) are
        also generated — cleanup routines must handle both layouts."""
        setup_sandbox(printer=lambda *_: None)
        ck = sandbox_root() / "projects" / "alpha" / ".ck"
        legacy = sorted(ck.glob("HISTORY_*.md.bak"))
        self.assertEqual(len(legacy), LEGACY_ARCHIVE_COUNT)

    def test_history_md_at_rotation_limit(self):
        setup_sandbox(printer=lambda *_: None)
        history = (sandbox_root() / "projects" / "alpha" / ".ck"
                   / "HISTORY.md")
        content = history.read_text(encoding="utf-8")
        self.assertEqual(_count_history_entries(content), HISTORY_LIMIT)

    def test_beta_initialized_but_unregistered(self):
        setup_sandbox(printer=lambda *_: None)
        beta = sandbox_root() / "projects" / "beta"
        self.assertTrue((beta / ".ck" / "PLAN.md").exists())
        tl = parse_plan_file(beta / ".ck" / "PLAN.md")
        self.assertGreaterEqual(tl.total, 1)
        data = self.fixture_registry()
        self.assertNotIn(
            "beta", {p["name"] for p in data["projects"]}
        )

    def test_gamma_is_plain_directory(self):
        setup_sandbox(printer=lambda *_: None)
        gamma = sandbox_root() / "projects" / "gamma"
        self.assertTrue(gamma.is_dir())
        self.assertFalse((gamma / ".ck").exists())

    def test_setup_is_idempotent_rebuild(self):
        setup_sandbox(printer=lambda *_: None)
        setup_sandbox(printer=lambda *_: None)
        data = self.fixture_registry()
        self.assertEqual(len(data["projects"]), 2)
        log = (sandbox_root() / "projects" / "alpha" / ".ck"
               / "history.log")
        entries = [
            ln for ln in log.read_text(encoding="utf-8").splitlines()
            if ln and not ln.startswith("#")
        ]
        self.assertEqual(len(entries), BULK_HISTORY_LOG_ENTRIES)

    def test_setup_never_touches_host_config(self):
        """Host isolation: the (patched) global config dir is not
        created, written, or modified by setup."""
        setup_sandbox(printer=lambda *_: None)
        self.assertFalse(self.host_config_dir.exists())

    def test_setup_replaces_previous_sandbox_content(self):
        stray = sandbox_root() / "projects" / "stray-artifacts"
        stray.mkdir(parents=True, exist_ok=True)
        (stray / "junk.txt").write_text("stale", encoding="utf-8")
        setup_sandbox(printer=lambda *_: None)
        self.assertFalse(stray.exists())


class TestDevModeRegistryIntegration(_SandboxFixtureBase):
    """Dev-mode registry read-your-writes against the fixtures."""

    def test_dashboard_reads_fixture_registry(self):
        setup_sandbox(printer=lambda *_: None)
        os.environ["CK_SANDBOX"] = "1"
        ck = ContextKeeper(root=REPO_ROOT)
        out = ck.dashboard()
        self.assertIn("alpha", out)
        self.assertIn("[MISSING] orphaned-deleted", out)
        # Host registry was never created.
        self.assertFalse(self.host_config_dir.exists())

    def test_dashboard_still_reads_real_registry_without_fixture(self):
        """Step 3 semantics preserved: with NO sandboxed registry
        copy, dev-mode reads come from the real registry."""
        root = Path(self._tmp.name) / "realproj"
        root.mkdir()
        prod_ck = ContextKeeper(root=root)
        prod_ck.init(register=True)  # production-mode registration
        self.assertTrue(ckconfig.GLOBAL_REGISTRY_FILE.exists())

        os.environ["CK_SANDBOX"] = "1"
        out = ContextKeeper(root=root).dashboard()
        self.assertIn("realproj", out)

    def test_prune_removes_orphaned_in_sandbox_only(self):
        setup_sandbox(printer=lambda *_: None)
        os.environ["CK_SANDBOX"] = "1"
        orphaned_path = str(
            (sandbox_root() / "projects" / "orphaned-deleted").resolve()
        )

        pruned = ContextKeeper(root=REPO_ROOT).prune()

        self.assertEqual(pruned, [orphaned_path])
        # Sandbox registry now holds only alpha.
        data = self.fixture_registry()
        self.assertEqual(
            [p["name"] for p in data["projects"]], ["alpha"]
        )
        names = [p.name for p in ckregistry.list_projects()]
        self.assertEqual(names, ["alpha"])
        # Host registry never created.
        self.assertFalse(self.host_config_dir.exists())

    def test_register_beta_into_sandbox_registry(self):
        """Standalone `ck register` (beta fixture) mutates ONLY the
        sandbox registry copy."""
        setup_sandbox(printer=lambda *_: None)
        os.environ["CK_SANDBOX"] = "1"
        beta = (sandbox_root() / "projects" / "beta").resolve()

        entry = ContextKeeper(root=REPO_ROOT).register(path=beta)

        self.assertEqual(entry.name, "beta")
        data = self.fixture_registry()
        names = {p["name"] for p in data["projects"]}
        self.assertEqual(names, {"alpha", "beta", "orphaned-deleted"})
        # Host registry never created.
        self.assertFalse(self.host_config_dir.exists())

    def test_host_registry_untouched_after_dev_writes(self):
        """A pre-existing host registry stays byte-identical and
        mtime-stable across a full dev session against the fixtures."""
        root = Path(self._tmp.name) / "realproj"
        root.mkdir()
        ContextKeeper(root=root).init(register=True)
        reg = ckconfig.GLOBAL_REGISTRY_FILE
        before = (reg.read_bytes(), reg.stat().st_mtime_ns)

        setup_sandbox(printer=lambda *_: None)
        os.environ["CK_SANDBOX"] = "1"
        ck = ContextKeeper(root=REPO_ROOT)
        ck.prune()
        ck.register(path=sandbox_root() / "projects" / "beta")

        self.assertEqual(reg.read_bytes(), before[0])
        self.assertEqual(reg.stat().st_mtime_ns, before[1])


class TestSandboxCli(_SandboxFixtureBase):
    """`ck sandbox setup` / `ck sandbox clean` CLI dispatch."""

    def _run_cli(self, argv: list) -> tuple:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def test_cli_setup_builds_fixtures(self):
        code, out = self._run_cli(["sandbox", "setup"])
        self.assertEqual(code, 0)
        self.assertIn("Sandbox mock environment built", out)
        self.assertTrue(
            (sandbox_root() / "config" / "projects.json").exists()
        )
        self.assertTrue(
            (sandbox_root() / "projects" / "alpha" / ".ck"
             / "PLAN.md").exists()
        )

    def test_cli_clean_removes_sandbox(self):
        self._run_cli(["sandbox", "setup"])
        code, out = self._run_cli(["sandbox", "clean"])
        self.assertEqual(code, 0)
        self.assertFalse(sandbox_root().exists())

    def test_cli_clean_missing_sandbox_is_success(self):
        code, _ = self._run_cli(["sandbox", "clean"])
        self.assertEqual(code, 0)

    def test_cli_bare_sandbox_prints_usage(self):
        code, out = self._run_cli(["sandbox"])
        self.assertEqual(code, 2)
        self.assertIn("Usage: ck sandbox <setup|clean>", out)

    def test_cli_unknown_sandbox_action(self):
        code, out = self._run_cli(["sandbox", "bogus"])
        self.assertEqual(code, 2)
        self.assertIn("Unknown sandbox action", out)

    def test_help_documents_sandbox_commands(self):
        code, out = self._run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("sandbox setup", out)
        self.assertIn("sandbox clean", out)


class TestCkDevEndToEnd(_SandboxFixtureBase):
    """The Step 4.1 verification flow, run as real subprocesses:

    ``./ck-dev sandbox setup`` then ``CK_SANDBOX=1 ./ck-dev dashboard``.

    ISOLATION: the subprocesses run against a throwaway CHECKOUT COPY
    (see tests/_isolated_checkout.py) whose git root — and therefore
    ``ck-dev``-anchored ``.sandbox/`` — lives inside this test's OS
    temp directory. The repository's own ``.sandbox/`` (the manual
    dev environment) is snapshotted and asserted byte-identical: the
    automated suite never builds into it or wipes it.
    """

    def setUp(self):
        super().setUp()
        try:
            self._checkout = make_checkout_copy(
                Path(self._tmp.name) / "checkout-copy")
        except RuntimeError as e:
            self.skipTest(f"cannot build isolated checkout copy: {e}")
        self._sandbox = self._checkout / ".sandbox"
        self._repo_sandbox_before = _snapshot_tree(REPO_ROOT / ".sandbox")

    def _assert_repo_sandbox_untouched(self):
        after = _snapshot_tree(REPO_ROOT / ".sandbox")
        self.assertEqual(
            after, self._repo_sandbox_before,
            "the repository's manual .sandbox/ was modified by a test",
        )

    def _run_ck_dev(self, args: list, extra_env: dict) -> tuple:
        env = {
            **os.environ,
            "HOME": str(Path(self._tmp.name)),
            "GIT_TERMINAL_PROMPT": "0",
            "CK_SANDBOX_ROOT": str(self._sandbox),
            **extra_env,
        }
        return subprocess.run(
            [sys.executable, str(self._checkout / "ck-dev"), *args],
            capture_output=True, text=True, timeout=90,
            cwd=str(self._checkout), env=env,
        )

    def test_setup_then_list_global(self):
        result = self._run_ck_dev(["sandbox", "setup"], {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Sandbox mock environment built", result.stdout)

        result = self._run_ck_dev(
            ["dashboard"], {"CK_SANDBOX": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("alpha", result.stdout)
        self.assertIn("[MISSING] orphaned-deleted", result.stdout)
        # Debug/interception noise stays off stdout.
        self.assertNotIn("[DEBUG]", result.stdout)

        # STRICT host isolation: the fake HOME global registry was
        # never created by either command.
        self.assertFalse(self.host_config_dir.exists())
        # The fixtures landed in the COPY's sandbox — never the
        # repository's manual one.
        self.assertTrue(
            (self._sandbox / "config" / "projects.json").is_file())
        self._assert_repo_sandbox_untouched()

    def test_setup_then_sandbox_clean(self):
        self._run_ck_dev(["sandbox", "setup"], {})
        self.assertTrue(self._sandbox.exists())
        result = self._run_ck_dev(["sandbox", "clean"], {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self._sandbox.exists())
        # The copy's clean removed the COPY sandbox only.
        self._assert_repo_sandbox_untouched()


def _snapshot_tree(root: Path) -> dict:
    """content + mtime_ns for every file under ``root`` (or {})."""
    snap: dict = {}
    if not root.exists():
        return snap
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            snap[str(p)] = (p.read_bytes(), st.st_mtime_ns)
    return snap


class TestVersionBump(unittest.TestCase):

    def test_version_is_at_least_025(self):
        parts = tuple(int(x) for x in ckconfig.VERSION.split("."))
        self.assertGreaterEqual(parts, (0, 2, 5))

    def test_pyproject_version_matches_config(self):
        import tomllib
        with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
            data = tomllib.load(fh)
        self.assertEqual(data["project"]["version"], ckconfig.VERSION)


class TestSetupSandboxSafeReinit(unittest.TestCase):
    """Safe re-initialization (FileNotFoundError regression).

    A rebuild triggered while a process sits INSIDE ``.sandbox/``
    used to ``rmtree`` the tree out from under the caller, dangling
    its working-directory descriptor — every later ``os.getcwd()``
    (i.e. any subsequent ``ck`` invocation) then crashed with
    ``FileNotFoundError: [Errno 2] No such file or directory``.
    """

    def _chdir(self, target: Path) -> None:
        self._saved_cwd = Path.cwd()
        self.addCleanup(os.chdir, self._saved_cwd)
        os.chdir(target)

    def test_reinit_from_inside_is_in_place_no_wipe(self):
        setup_sandbox(printer=lambda *_: None)
        proj = sandbox_root() / "projects" / "alpha"
        self._chdir(proj)

        marker = proj / "sentinel.txt"
        marker.write_text("keep me", encoding="utf-8")
        buf = io.StringIO()
        self.assertTrue(setup_sandbox(printer=buf.write))
        self.assertIn("IN PLACE", buf.getvalue())
        # The working directory (and its inode) survived untouched.
        self.assertTrue(marker.is_file(), "fixture dir was wiped")
        self.assertTrue(Path.cwd().is_dir())
        self.assertEqual(Path.cwd(), proj)

    def test_reinit_from_outside_still_wipes(self):
        setup_sandbox(printer=lambda *_: None)
        marker = sandbox_root() / "projects" / "alpha" / "sentinel.txt"
        marker.write_text("gone", encoding="utf-8")
        # The suite runs from the repo root, OUTSIDE the pinned
        # sandbox anchor — the full rebuild semantics are preserved.
        self.assertTrue(
            sandbox_root() not in Path.cwd().parents
            and Path.cwd() != sandbox_root()
        )
        self.assertTrue(setup_sandbox(printer=lambda *_: None))
        self.assertFalse(
            marker.exists(), "stale fixture survived a full rebuild")

    def test_forced_rebuild_from_inside_warns(self):
        setup_sandbox(printer=lambda *_: None)
        proj = sandbox_root() / "projects" / "alpha"
        self._chdir(proj)
        buf = io.StringIO()
        self.assertTrue(setup_sandbox(printer=buf.write, force=True))
        # Explicit override still wipes — but says so first.
        self.assertIn("Forced rebuild", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
