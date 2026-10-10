"""Tests for context resolution & empty-state handling in `ck st`.

Covers the project-context resolution hierarchy for READ operations
(``ck st`` / ``ck list`` / ``ck st --all``):

1. LOCAL  — a PLAN.md (real, or a dev-mode sandbox mirror) in the
   bound directory;
2. UPWARD — ancestor directories up to (and including) the Git
   repository root; a project ABOVE the repo boundary is NOT picked
   up;
3. GLOBAL — most recently active registered project from the global
   registry.

Plus:

- the explicit uninitialized / no-project state (no fake ``0/0``
  dashboard) for bare directories;
- the ``ck note`` / ``ck start`` no-project guards (clear error, no
  orphaned state entries);
- task-note visibility (``* Note:``) for BOTH local and global
  resolution;
- the dev-mode desync fix: ``ck-dev add``/``ck note`` in an
  uninitialized directory stay visible to ``ck-dev st``.
"""

from __future__ import annotations

# Filesystem isolation safety net: importing the tests package
# pins CK_SANDBOX_ROOT to an OS-temp directory (see tests/__init__),
# so no test in this module can create or wipe the repository's own
# .sandbox/ — under ANY runner, including bare `unittest discover`.
import tests  # noqa: F401

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import sandbox as cksandbox
from cklib.ui import strip_ansi
from cklib.cli import main
from cklib.core import ContextKeeper, ProjectContext
from cklib.sandbox import resolve_write_path

REPO_HAS_GIT = shutil.which("git") is not None

PLAN_WITH_FOCUS = (
    "# Demo\n"
    "## Current Sprint\n"
    "- [>] focused task\n"
    "- [ ] open task\n"
)


def _clean_env(**overrides):
    """Env patcher: CK color/sandbox toggles removed by default."""
    base = {
        k: v for k, v in os.environ.items()
        if k not in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE",
                     "CK_SANDBOX", "CK_DEV")
    }
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=True)


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", str(path)],
        capture_output=True, check=True, timeout=30,
    )


class _IsolatedHome(unittest.TestCase):
    """Pin the global registry/config to a per-test tmp HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._orig = {
            (ckconfig, name): getattr(ckconfig, name)
            for name in (
                "GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                "GLOBAL_STATE_FILE", "LEGACY_GLOBAL_CONFIG_FILE",
            )
        }
        self._orig_reg = {
            (ckregistry, name): getattr(ckregistry, name)
            for name in (
                "GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                "LEGACY_GLOBAL_CONFIG_FILE",
            )
        }

        new_dir = fake_home / ".config" / "ck"
        for mod in (ckconfig, ckregistry):
            mod.GLOBAL_CONFIG_DIR = new_dir
            mod.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
            mod.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"
        ckconfig.GLOBAL_STATE_FILE = new_dir / "state.json"

        def _restore():
            for (mod, name), value in {**self._orig, **self._orig_reg}.items():
                setattr(mod, name, value)
        self.addCleanup(_restore)

    # -- project scaffolding ------------------------------------------- #

    def make_project(self, name: str, *, plan: str = PLAN_WITH_FOCUS,
                     note: str = "", parent: Path | None = None) -> Path:
        """A real on-disk project: .ck/PLAN.md (+ optional state note)."""
        root = (parent or Path(self._tmp.name)) / name
        root.mkdir(parents=True, exist_ok=True)
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(plan, encoding="utf-8")
        if note:
            ck.set_note(note)
        return root

    # -- CLI runner ------------------------------------------------------ #

    def run_cli(self, argv: list, cwd: Path) -> tuple:
        """Run main(argv) with cwd bound; returns (exit_code, stdout).

        The stdout is ANSI-STRIPPED. This module tests context
        resolution, never colour: the CLI colours its output by default
        (even into a pipe — see :func:`cklib.ui.color_enabled`), so
        assertions here are written against plain text.
        """
        buf = io.StringIO()
        orig_cwd = Path.cwd()
        os.chdir(cwd)
        try:
            with redirect_stdout(buf):
                code = main(argv)
        finally:
            os.chdir(orig_cwd)
        return code, strip_ansi(buf.getvalue())


# ---------------------------------------------------------------------------
# 1. Bare directory: explicit uninitialized state (no fake dashboard)
# ---------------------------------------------------------------------------


class TestBareDirectoryStatus(_IsolatedHome):
    def test_st_in_bare_dir_shows_uninitialized_message(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        with _clean_env():
            code, out = self.run_cli(["st"], bare)
        self.assertEqual(code, 0)
        self.assertIn("No active project found", out)
        self.assertIn("ck init", out)
        # NO fake metrics, NO empty dashboard scaffolding.
        self.assertNotIn("0/0", out)
        self.assertNotIn("WORK CONTEXT", out)
        self.assertNotIn("tasks done", out)
        self.assertNotIn("no focus selected", out)

    def test_st_with_ck_dir_but_no_plan_shows_uninitialized(self):
        """`.ck/` present but PLAN.md missing (e.g. manually deleted):
        still an uninitialized state, never a 0/0 dashboard."""
        root = Path(self._tmp.name) / "half-initialized"
        (root / ".ck").mkdir(parents=True)
        with _clean_env():
            code, out = self.run_cli(["st"], root)
        self.assertEqual(code, 0)
        self.assertIn("No active project found", out)
        self.assertNotIn("0/0", out)
        self.assertNotIn("WORK CONTEXT", out)

    def test_status_api_on_bare_root(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        ck = ContextKeeper(root=bare)
        self.assertIsNone(ck.resolve_context())
        out = ck.status()
        self.assertIn("No active project found", out)
        self.assertNotIn("0/0", out)

    def test_tasks_in_bare_dir_lists_nested_projects_not_a_guess(self):
        """A bare directory with nothing registered below it must NOT
        fall back to an arbitrary registry entry. `ck list` reports
        that the directory is not a project and shows the (empty)
        set of choices plus the explicit-targeting hint.
        """
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        with _clean_env():
            code, out = self.run_cli(["list"], bare)
        self.assertEqual(code, 0)
        self.assertIn("not a context-keeper project", out)
        self.assertIn("Available nested projects", out)
        self.assertIn("ck list <project_name>", out)

    def test_tasks_inside_empty_project_keeps_empty_hint(self):
        """The "No tasks" hint is still reachable — it belongs to a
        project that EXISTS but has an empty plan, not to the
        out-of-project discovery path.
        """
        project = Path(self._tmp.name) / "empty_proj"
        (project / ".ck").mkdir(parents=True)
        (project / ".ck" / "PLAN.md").write_text("", encoding="utf-8")
        with _clean_env():
            code, out = self.run_cli(["list"], project)
        self.assertEqual(code, 0)
        self.assertIn("No tasks", out)
        self.assertIn("ck add", out)

    def test_st_all_in_bare_dir_reports_missing_plan(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        with _clean_env():
            code, out = self.run_cli(["st", "--all"], bare)
        self.assertEqual(code, 0)
        self.assertIn("No active project found", out)
        self.assertIn("PLAN.md not found", out)


# ---------------------------------------------------------------------------
# 2. Upward traversal (bounded by the Git repository root)
# ---------------------------------------------------------------------------


@unittest.skipUnless(REPO_HAS_GIT, "git is required for traversal bounds")
class TestUpwardTraversal(_IsolatedHome):
    def _repo_project(self, name: str = "repo") -> Path:
        repo = Path(self._tmp.name) / name
        repo.mkdir()
        _git_init(repo)
        self.make_project(name, note="root project note", parent=Path(
            self._tmp.name))
        return repo

    def test_st_from_subdir_resolves_root_project_and_note(self):
        repo = self._repo_project()
        deep = repo / "src" / "module"
        deep.mkdir(parents=True)
        with _clean_env():
            code, out = self.run_cli(["st"], deep)
        self.assertEqual(code, 0)
        # The ROOT project is rendered (not an empty 0/0 dashboard).
        self.assertIn("> repo", out)
        self.assertIn("[>] Focus:", out)
        self.assertIn("- [1] focused task", out)
        self.assertIn(">> Upcoming:", out)
        self.assertIn("- [2] open task [ ]", out)
        # The stored note is visible under the resolved context.
        self.assertIn("* Note: root project note", out)
        self.assertNotIn("No active project found", out)

    def test_tasks_from_subdir_lists_root_tasks(self):
        repo = self._repo_project()
        deep = repo / "src"
        deep.mkdir()
        with _clean_env():
            code, out = self.run_cli(["list"], deep)
        self.assertEqual(code, 0)
        self.assertIn("[>] 1. focused task", out)
        self.assertIn("[ ] 2. open task", out)

    def test_st_all_from_subdir_prints_resolved_root_plan(self):
        repo = self._repo_project()
        deep = repo / "src"
        deep.mkdir()
        with _clean_env():
            code, out = self.run_cli(["st", "--all"], deep)
        self.assertEqual(code, 0)
        self.assertIn("FULL PLAN", out)
        self.assertIn("- [>] focused task", out)
        self.assertNotIn("PLAN.md not found", out)

    def test_traversal_stops_at_git_root(self):
        """A .ck project living ABOVE the Git repository root is
        deliberately not resolved from inside the repo."""
        outer = Path(self._tmp.name) / "outer"
        outer.mkdir()
        # The project lives at outer/, the Git repo at outer/repo/.
        self.make_project("outer", parent=Path(self._tmp.name))
        repo = outer / "repo"
        repo.mkdir()
        _git_init(repo)
        (repo / "sub").mkdir()

        with _clean_env():
            code, out = self.run_cli(["st"], repo / "sub")
        self.assertEqual(code, 0)
        # Bounded traversal: outer's project is invisible, and with an
        # empty global registry the explicit no-project state shows.
        self.assertIn("No active project found", out)
        self.assertNotIn("> outer", out)
        self.assertNotIn("0/0", out)

    def test_non_git_parent_resolution(self):
        """Without Git, the walk continues to the filesystem root
        (classic find_project_root behaviour)."""
        proj = self.make_project("plainproj")
        sub = proj / "nested" / "deep"
        sub.mkdir(parents=True)
        with _clean_env():
            code, out = self.run_cli(["st"], sub)
        self.assertEqual(code, 0)
        self.assertIn("> plainproj", out)
        self.assertIn("focused task", out)


# ---------------------------------------------------------------------------
# 3. Global registry fallback
# ---------------------------------------------------------------------------


class TestGlobalFallback(_IsolatedHome):
    def test_st_in_bare_dir_falls_back_to_registered_project(self):
        proj = self.make_project("registered", note="global note")
        ckregistry.register_project(proj, name="registered")

        bare = Path(self._tmp.name) / "elsewhere"
        bare.mkdir()
        with _clean_env():
            code, out = self.run_cli(["st"], bare)
        self.assertEqual(code, 0)
        self.assertIn("> registered", out)
        self.assertIn("[>] Focus:", out)
        self.assertIn("- [1] focused task", out)
        # The note stored in the registered project's state renders
        # through the global resolution path.
        self.assertIn("* Note: global note", out)
        # The header annotates WHICH project was resolved globally.
        self.assertIn(f"<- {proj}", out)
        self.assertNotIn("No active project found", out)

    def test_fallback_skips_registered_project_without_plan(self):
        empty = Path(self._tmp.name) / "planless"
        empty.mkdir()
        ckregistry.register_project(empty, name="planless")

        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        with _clean_env():
            code, out = self.run_cli(["st"], bare)
        self.assertEqual(code, 0)
        self.assertIn("No active project found", out)
        self.assertNotIn("planless", out)

    def test_fallback_skips_registered_project_missing_from_disk(self):
        gone = Path(self._tmp.name) / "gone"
        gone.mkdir()
        ckregistry.register_project(gone, name="gone")
        gone.rmdir()

        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        with _clean_env():
            code, out = self.run_cli(["st"], bare)
        self.assertEqual(code, 0)
        self.assertIn("No active project found", out)

    def test_fallback_prefers_most_recently_active(self):
        older = self.make_project("older-proj")
        ckregistry.register_project(older, name="older-proj")
        newer = self.make_project("newer-proj")
        # Registered later → fresher last_seen → wins the fallback.
        ckregistry.register_project(newer, name="newer-proj")

        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        with _clean_env():
            code, out = self.run_cli(["st"], bare)
        self.assertEqual(code, 0)
        self.assertIn("> newer-proj", out)
        self.assertNotIn("> older-proj", out)

    def test_local_project_wins_over_global_registry(self):
        """Hierarchy order: a resolvable local project shadows the
        global registry — even when the registry entry is newer."""
        proj = self.make_project("registered")
        ckregistry.register_project(proj, name="registered")
        local = self.make_project("local-proj", parent=Path(self._tmp.name))

        with _clean_env():
            code, out = self.run_cli(["st"], local)
        self.assertEqual(code, 0)
        self.assertIn("> local-proj", out)
        self.assertNotIn("<- ", out)  # no global-fallback annotation


# ---------------------------------------------------------------------------
# 3b. `ck st` OUTSIDE a project: nested discovery, never a silent pick
# ---------------------------------------------------------------------------


class TestStatusOutsideProject(_IsolatedHome):
    """``ck st`` applies the SAME discovery rules as ``ck list``.

    Before this behaviour existed, ``ck st`` outside every project
    fell straight through to the GLOBAL tier — "most recently active
    registered project" — and rendered it as if the user had asked
    for it. The commands pinned here:

    - several projects nested under the invocation directory → the
      choice notice, and NOTHING of any project's status;
    - exactly one nested project → its status, annotated with the
      path that was picked;
    - no argument outside → the same rules; ``ck st -l`` is
      byte-identical to ``ck list``;
    - ``ck st <project_name>`` → explicit targeting from anywhere,
      unknown name → exit 1;
    - ``ck st --all`` in the ambiguous case → the notice alone: no
      foreign project's PLAN.md, no bogus "PLAN.md not found".
    """

    def _workspace(self, *names) -> Path:
        """A parent folder holding ``names`` as nested projects.

        Each project gets DISTINCTIVE task text (``<name> task``) so
        a rendered block can always be attributed to one project —
        the whole point of these tests is proving WHICH project (if
        any) was printed.
        """
        ws = Path(self._tmp.name) / "workspace"
        ws.mkdir(exist_ok=True)
        for name in names:
            root = self.make_project(name, parent=ws)
            (root / ".ck" / "PLAN.md").write_text(
                f"# {name}\n\n## Current Sprint\n- [ ] {name} task\n",
                encoding="utf-8")
        return ws

    def test_multiple_nested_projects_are_listed_not_guessed(self):
        ws = self._workspace("alpha", "beta")

        with _clean_env():
            code, out = self.run_cli(["st"], ws)

        self.assertEqual(code, 0, out)
        self.assertIn("not a context-keeper project", out)
        self.assertIn("Available nested projects:", out)
        self.assertIn("  - alpha", out)
        self.assertIn("  - beta", out)
        self.assertIn("ck st <project_name>", out)
        # Crucially: NEITHER project's status block was rendered.
        self.assertNotIn("alpha task", out)
        self.assertNotIn("beta task", out)
        self.assertNotIn("CURRENT FOCUS", out)
        self.assertNotIn("tasks done", out)
        self.assertNotIn("<- ", out)

    def test_single_nested_project_is_rendered_with_its_path(self):
        ws = self._workspace()
        self.make_project("lonely", parent=ws)

        with _clean_env():
            code, out = self.run_cli(["st"], ws)

        self.assertEqual(code, 0, out)
        self.assertIn("> lonely", out)
        # The annotation says WHICH project the command adopted.
        self.assertIn(f"<- {ws / 'lonely'}", out)
        self.assertNotIn("Available nested projects", out)

    def test_registered_sibling_does_not_mask_the_scan(self):
        """Registry ∪ scan: one registered project among several
        physical ones must NOT read as "exactly one nested project"."""
        ws = self._workspace("zeta_registered", "alpha_unregistered")
        ckregistry.register_project(ws / "zeta_registered",
                                    name="zeta_registered")

        with _clean_env():
            code, out = self.run_cli(["st"], ws)

        self.assertEqual(code, 0, out)
        self.assertIn("Available nested projects:", out)
        self.assertIn("  - alpha_unregistered", out)
        self.assertIn("  - zeta_registered", out)
        self.assertNotIn("CURRENT FOCUS", out)
        self.assertNotIn("alpha_unregistered task", out)
        self.assertNotIn("zeta_registered task", out)

    def test_st_l_is_byte_identical_to_ck_list_outside_a_project(self):
        """``ck st -l`` is documented as the ``ck list`` view — same
        bytes, same discovery, outside every project included."""
        ws = self._workspace("alpha", "beta")

        with _clean_env():
            code_l, out_l = self.run_cli(["st", "-l"], ws)
            code_list, out_list = self.run_cli(["list"], ws)

        self.assertEqual(code_l, 0, out_l)
        self.assertEqual(code_list, 0, out_list)
        self.assertEqual(out_l, out_list)
        self.assertIn("Available nested projects", out_l)
        self.assertNotIn("tasks done", out_l)

    def test_explicit_name_targets_status_from_any_directory(self):
        ws = self._workspace("alpha", "beta")

        with _clean_env():
            code, out = self.run_cli(["st", "alpha"], ws)

        self.assertEqual(code, 0, out)
        self.assertIn("> alpha", out)
        self.assertNotIn("> beta", out)
        self.assertNotIn("Available nested projects", out)

    def test_unknown_name_exits_1_with_error(self):
        ws = self._workspace("alpha")

        with _clean_env():
            code, out = self.run_cli(["st", "nope"], ws)

        self.assertEqual(code, 1)
        self.assertIn("ERROR: Project 'nope' not found.", out)
        self.assertNotIn("> alpha", out)

    def test_named_target_with_all_prints_that_projects_plan(self):
        ws = self._workspace("alpha", "beta")

        with _clean_env():
            code, out = self.run_cli(["st", "alpha", "--all"], ws)

        self.assertEqual(code, 0, out)
        self.assertIn("> alpha", out)
        self.assertIn("FULL PLAN", out)
        self.assertIn("alpha task", out)
        self.assertNotIn("beta task", out)

    def test_st_all_in_ambiguous_directory_prints_no_foreign_plan(self):
        """Several candidates, none chosen: the notice stands alone.
        No other project's PLAN.md — and no bogus "PLAN.md not
        found" either, since every listed project HAS one."""
        ws = self._workspace("alpha", "beta")

        with _clean_env():
            code, out = self.run_cli(["st", "--all"], ws)

        self.assertEqual(code, 0, out)
        self.assertIn("Available nested projects:", out)
        self.assertNotIn("FULL PLAN", out)
        self.assertNotIn("alpha task", out)
        self.assertNotIn("beta task", out)
        self.assertNotIn("PLAN.md not found", out)

    def test_global_dashboard_flag_rejects_a_project_name(self):
        """``-g`` is the cross-project view: pairing it with a target
        is a usage error, never a silently ignored argument."""
        ws = self._workspace("alpha")

        with _clean_env():
            code, out = self.run_cli(["st", "alpha", "-g"], ws)

        self.assertEqual(code, 2)
        self.assertIn("takes no project name", out)

    def test_zero_nested_projects_keep_the_standard_global_fallback(self):
        """Nothing nested → the documented fallback stands (the
        registry decides, as it always did for this case)."""
        proj = self.make_project("registered")
        ckregistry.register_project(proj, name="registered")
        bare = Path(self._tmp.name) / "empty"
        bare.mkdir()

        with _clean_env():
            code, out = self.run_cli(["st"], bare)

        self.assertEqual(code, 0, out)
        self.assertIn("> registered", out)
        self.assertNotIn("Available nested projects", out)


# ---------------------------------------------------------------------------
# 4. Resolution sources (unit-level)
# ---------------------------------------------------------------------------


class TestResolveContextSources(_IsolatedHome):
    def test_source_local(self):
        proj = self.make_project("proj")
        ctx = ContextKeeper(root=proj).resolve_context()
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.source, "local")
        self.assertEqual(Path(ctx.root).resolve(), proj.resolve())

    def test_source_parent(self):
        proj = self.make_project("proj")
        sub = proj / "sub"
        sub.mkdir()
        ctx = ContextKeeper(root=sub).resolve_context()
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.source, "parent")
        self.assertEqual(Path(ctx.root).resolve(), proj.resolve())

    def test_source_global(self):
        proj = self.make_project("registered")
        ckregistry.register_project(proj, name="registered")
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        ctx = ContextKeeper(root=bare).resolve_context()
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.source, "global")
        self.assertEqual(Path(ctx.root).resolve(), proj.resolve())

    def test_source_none(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        self.assertIsNone(ContextKeeper(root=bare).resolve_context())

    def test_project_context_dataclass_fields(self):
        proj = self.make_project("proj")
        ctx = ProjectContext(root=proj, source="local")
        self.assertEqual(ctx.root, proj)
        self.assertEqual(ctx.source, "local")


# ---------------------------------------------------------------------------
# 5. ck note / ck start guards (no valid project plan bound)
# ---------------------------------------------------------------------------


class TestNoProjectGuards(_IsolatedHome):
    def test_note_in_bare_dir_errors_clearly_without_orphans(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        with _clean_env():
            code, out = self.run_cli(["note", "orphan note"], bare)
        self.assertEqual(code, 2)
        self.assertIn("No active project found", out)
        self.assertIn("ck init", out)
        # No orphaned state entries: nothing was created on disk.
        self.assertFalse((bare / ".ck").exists())
        self.assertEqual(list(bare.iterdir()), [])

    def test_start_in_bare_dir_errors_clearly(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        with _clean_env():
            code, out = self.run_cli(["start", "1"], bare)
        self.assertEqual(code, 2)
        self.assertIn("No active project found", out)
        self.assertFalse((bare / ".ck").exists())

    def test_note_api_guard_raises_value_error(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        ck = ContextKeeper(root=bare)
        with self.assertRaises(ValueError) as cm:
            ck.set_note("orphan")
        self.assertIn("No active project found", str(cm.exception))
        with self.assertRaises(ValueError):
            ck.start(1)
        # Nothing written anywhere.
        self.assertFalse(ck.state_file.exists())

    def test_note_dev_mode_creates_no_orphan_sandbox_state(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        cksandbox.clean_sandbox(quiet=True)
        self.addCleanup(cksandbox.clean_sandbox, quiet=True)
        with _clean_env(CK_SANDBOX="1"):
            code, out = self.run_cli(["note", "orphan"], bare)
            sandbox_state = resolve_write_path(
                bare / ".ck" / "state.json")
        self.assertEqual(code, 2)
        self.assertIn("No active project found", out)
        # No orphaned sandbox state entry either.
        self.assertFalse(sandbox_state.exists())
        self.assertFalse((bare / ".ck").exists())


# ---------------------------------------------------------------------------
# 6. Dev mode: the reported add/note -> st desync
# ---------------------------------------------------------------------------


class TestDevModeDesync(_IsolatedHome):
    """`ck-dev add` / `ck-dev note` in an uninitialized directory write
    to sandbox buffers; `ck-dev st` must resolve that sandbox project
    instead of rendering a fake empty dashboard."""

    def setUp(self):
        super().setUp()
        cksandbox.clean_sandbox(quiet=True)
        self._saved_argv = cksandbox.sys.argv
        cksandbox.sys.argv = ["ck"]

        def _restore_argv():
            cksandbox.sys.argv = self._saved_argv
        self.addCleanup(_restore_argv)
        self.addCleanup(cksandbox.clean_sandbox, quiet=True)

    def test_dev_add_note_then_st_shows_them(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()

        with _clean_env(CK_SANDBOX="1"):
            code1, out1 = self.run_cli(["add", "sandbox task"], bare)
            code2, out2 = self.run_cli(
                ["note", "sandbox note"], bare)
            code3, out3 = self.run_cli(["st"], bare)

        self.assertEqual(code1, 0, out1)
        self.assertEqual(code2, 0, out2)
        self.assertIn("Note saved", out2)

        # st resolves the sandbox-only project: task AND note visible.
        self.assertEqual(code3, 0, out3)
        self.assertIn("sandbox task", out3)
        self.assertIn("* Note: sandbox note", out3)
        self.assertNotIn("No active project found", out3)
        self.assertNotIn("0/0", out3)

        # The real (uninitialized) directory is still untouched.
        self.assertFalse((bare / ".ck").exists())

    def test_dev_st_in_bare_dir_without_registry_shows_uninitialized(self):
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        with _clean_env(CK_SANDBOX="1"):
            code, out = self.run_cli(["st"], bare)
        self.assertEqual(code, 0)
        self.assertIn("No active project found", out)
        self.assertNotIn("0/0", out)
        self.assertNotIn("WORK CONTEXT", out)


if __name__ == "__main__":
    unittest.main()
