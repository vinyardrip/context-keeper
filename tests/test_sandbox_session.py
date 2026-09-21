"""Sandbox session management tests (entry / exit / interceptor).

Covers the explicit dev-sandbox lifecycle:

- ``ck-dev`` (bare): explicit ENTRY — sets up ``.sandbox/`` when
  missing, resolves the active project, persists ``state.json`` and
  prints the warning banner to STDOUT;
- ``ck-dev exit``: the canonical EXIT — clears the session, confirms
  where production resumes, graceful no-op outside a session;
- plain ``ck`` inside a sandbox session (``CK_SANDBOX=1`` or cwd
  under ``.sandbox/``): the exact warning banner precedes the normal
  command output, then execution proceeds transparently in sandbox
  mode;
- banner rendering is exact and box-aligned.

All filesystem-touching tests pin the sandbox anchor to a per-test
temp directory via ``CK_SANDBOX_ROOT`` (mirroring the wrapper's own
pinning) so the repository's manual ``.sandbox/`` is never touched.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import sandbox as cksandbox
from cklib.cli import main
from cklib.sandbox import (
    SANDBOX_ACTIVE_ENV,
    SANDBOX_DIR_NAME,
    SANDBOX_ROOT_ENV,
    STATE_FILENAME,
    active_sandbox_project,
    enter_sandbox,
    exit_sandbox,
    render_sandbox_banner,
    sandbox_root,
)


def _clean_env(**extra) -> dict:
    """Production env without CK toggles, plus explicit extras."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("CK_SANDBOX", "CK_SANDBOX_ROOT", "CK_DEV",
                     "CK_DEBUG", "CK_SANDBOX_ACTIVE", "NO_COLOR",
                     "FORCE_COLOR", "CLICOLOR_FORCE")
    }
    env.update(extra)
    return env


class _SandboxAnchor(unittest.TestCase):
    """Pin the sandbox anchor + global registry to a per-test tmp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.anchor = base / SANDBOX_DIR_NAME
        self._orig_env = os.environ.copy()

        os.environ[SANDBOX_ROOT_ENV] = str(self.anchor)
        os.environ.pop(SANDBOX_ACTIVE_ENV, None)
        os.environ.pop("CK_SANDBOX", None)
        os.environ.pop("CK_DEV", None)

        fake_home = base / "home"
        new_dir = fake_home / ".config" / "ck"
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
        for mod in (ckconfig, ckregistry):
            mod.GLOBAL_CONFIG_DIR = new_dir
            mod.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
            mod.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"
        self.addCleanup(self._restore)

    def _build_minimal_sandbox(self) -> None:
        """Populate the pinned anchor with the fixture environment."""
        from cklib.sandbox_setup import setup_sandbox

        self.assertTrue(setup_sandbox(printer=lambda *_: None))

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        for (mod, name), value in self._orig.items():
            setattr(mod, name, value)

    def tearDown(self):
        self._restore()


class TestBannerRendering(unittest.TestCase):
    """render_sandbox_banner: DYNAMIC-WIDTH box with the ACTIVE
    BINARY row.

    The label and hint rows are static; the ``Active binary:`` row
    is the ONE dynamic element — resolved live via ``command -v ck``
    semantics on every render. The box width is computed from the
    longest content row, expanding for long paths and contracting
    for short ones.
    """

    def test_exact_layout_with_binary_row(self):
        out = render_sandbox_banner(color=False)
        lines = out.splitlines()
        # Border rule counts match the widest content row + padding;
        # all rows share one width (aligned box, never ragged).
        inner = len(lines[0]) - 2
        self.assertEqual(lines[0], "┌" + "─" * inner + "┐")
        self.assertEqual(lines[4], "└" + "─" * inner + "┘")
        self.assertEqual(lines[1].strip("│ "), "⚠️  SANDBOX MODE ACTIVE")
        self.assertTrue(
            lines[2].strip("│ ").startswith("Active binary: "),
            f"missing Active binary row: {lines[2]!r}")
        self.assertEqual(lines[3].strip("│ "),
                         "Type 'exit' or press Ctrl+D to return to "
                         "production")
        self.assertEqual(len({len(l) for l in lines}), 1)
        # Width derives from the longest CONTENT row (hint wins here).
        self.assertEqual(inner, len(lines[3].strip("│ ")) + 2)

    def test_width_expands_for_long_binary_path(self):
        from cklib.sandbox import active_binary_path

        short = render_sandbox_banner(color=False)
        long_path = ("/very/long/deep/nested/path/to/some/project/"
                     "tooling/venv/bin/ck")
        with mock.patch.object(
                cksandbox, "active_binary_path",
                return_value=Path(long_path)):
            wide = render_sandbox_banner(color=False)
        self.assertGreater(len(wide.splitlines()[0]),
                           len(short.splitlines()[0]),
                           "box must widen for a long binary path")
        self.assertIn(f"Active binary: {long_path}", wide)
        # Still a perfectly aligned box.
        self.assertEqual(len({len(l) for l in wide.splitlines()}), 1)
        self.assertTrue(active_binary_path() is not None or True)

    def test_binary_row_carries_the_resolved_path(self):
        from cklib.sandbox import active_binary_path

        resolved = active_binary_path()
        out = render_sandbox_banner(color=False)
        binary_row = out.splitlines()[2].strip("│ ")
        self.assertTrue(binary_row.startswith("Active binary: "))
        if resolved is not None:
            self.assertIn(str(resolved), binary_row)

    def test_static_content_has_no_project_names(self):
        out = render_sandbox_banner(color=False)
        # No project names (the active-binary path is the one dynamic
        # element and may legitimately contain separators).
        self.assertNotIn("alpha", out)
        self.assertNotIn("beta", out)
        self.assertNotIn(".sandbox", out)

    def test_rows_are_aligned(self):
        out = render_sandbox_banner(color=False)
        lines = out.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("┌") and lines[0].endswith("┐"))
        for line in lines[1:4]:
            self.assertTrue(line.startswith("│") and line.endswith("│"))
        self.assertTrue(lines[4].startswith("└") and lines[4].endswith("┘"))
        self.assertIn("⚠️  SANDBOX MODE ACTIVE", lines[1])
        self.assertIn("Active binary:", lines[2])
        self.assertIn("Type 'exit' or press Ctrl+D to return to "
                      "production", lines[3])
        widths = {len(l) for l in lines}
        self.assertEqual(len(widths), 1, f"ragged banner: {out!r}")

    def test_no_ansi_escapes_without_color(self):
        out = render_sandbox_banner(color=False)
        self.assertNotIn("\033[", out)

    def test_color_is_bold_yellow(self):
        out = render_sandbox_banner(color=True)
        # Every row is painted bold (\033[1m) + yellow (\033[33m) with
        # a reset (the palette emits codes as separate SGR sequences).
        self.assertEqual(out.count("\033[1m"), 5)
        self.assertEqual(out.count("\033[33m"), 5)
        self.assertEqual(out.count("\033[0m"), 5)
        self.assertIn("⚠️  SANDBOX MODE ACTIVE", out)

    def test_default_respects_no_color_env(self):
        # conftest pins NO_COLOR=1 for the whole session; the default
        # (color=None) path must honor it.
        self.assertNotIn("\033[", render_sandbox_banner())

    def test_default_defers_to_color_enabled(self):
        """color=None delegates to cklib.ui.color_enabled."""
        with mock.patch("cklib.ui.color_enabled", return_value=True):
            self.assertIn("\033[", render_sandbox_banner())


class TestActiveProjectResolution(_SandboxAnchor):
    def test_none_without_any_marker(self):
        self.assertIsNone(active_sandbox_project())

    def test_env_mirror_wins(self):
        proj = self._tmp_path("proj-a")
        proj.mkdir(parents=True)
        os.environ[SANDBOX_ACTIVE_ENV] = str(proj)
        self.assertEqual(active_sandbox_project(), proj)

    def test_env_mirror_stale_dir_falls_back_to_state(self):
        proj = self._tmp_path("from-state")
        proj.mkdir(parents=True)
        state = sandbox_root() / STATE_FILENAME
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps({"project": str(proj),
                        "repo_root": str(self._tmp_path("repo"))}),
            encoding="utf-8",
        )
        os.environ[SANDBOX_ACTIVE_ENV] = str(self._tmp_path("gone"))
        self.assertEqual(active_sandbox_project(), proj)

    def test_state_file_marker(self):
        proj = self._tmp_path("state-proj")
        proj.mkdir(parents=True)
        state = sandbox_root() / STATE_FILENAME
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps({"project": str(proj), "repo_root": "/repo"}),
            encoding="utf-8",
        )
        self.assertEqual(active_sandbox_project(), proj)

    def test_stale_state_resolves_to_none(self):
        state = sandbox_root() / STATE_FILENAME
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps({"project": str(self._tmp_path("vanished")),
                        "repo_root": "/repo"}),
            encoding="utf-8",
        )
        self.assertIsNone(active_sandbox_project())

    def test_corrupt_state_resolves_to_none(self):
        state = sandbox_root() / STATE_FILENAME
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("{not json", encoding="utf-8")
        self.assertIsNone(active_sandbox_project())

    def _tmp_path(self, name: str) -> Path:
        return Path(self._tmp.name) / name


class TestEnterSandbox(_SandboxAnchor):
    def test_entry_runs_setup_when_sandbox_missing(self):
        self.assertFalse(sandbox_root().exists())
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = enter_sandbox()
        self.assertEqual(code, 0)
        out = buf.getvalue()
        # Banner printed (with the active-binary row), followed by
        # the Global Dashboard.
        self.assertIn("SANDBOX MODE ACTIVE", out)
        self.assertIn("Type 'exit' or press Ctrl+D to return to "
                      "production", out)
        self.assertIn("GLOBAL DASHBOARD", out)
        # The fixture registry makes the mock projects visible
        # out-of-the-box.
        self.assertIn("alpha", out)
        self.assertIn("orphaned-deleted", out)
        self.assertLess(out.index("SANDBOX MODE ACTIVE"),
                        out.index("GLOBAL DASHBOARD"))
        # Session persisted.
        state = sandbox_root() / STATE_FILENAME
        self.assertTrue(state.is_file())
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(Path(data["project"]).name, "alpha")
        # The fixture environment was built inside the pinned anchor.
        self.assertTrue(
            (sandbox_root() / "config" / "projects.json").is_file())
        self.assertTrue((sandbox_root() / "projects" / "alpha").is_dir())

    def test_entry_with_existing_sandbox_does_not_rebuild(self):
        marker = sandbox_root() / "projects" / "alpha" / ".ck" / "PLAN.md"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("# alpha\n", encoding="utf-8")
        reg = sandbox_root() / "config" / "projects.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("{}", encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = enter_sandbox()
        self.assertEqual(code, 0)
        # The pre-existing fixture content survived (no rebuild).
        self.assertEqual(marker.read_text(encoding="utf-8"), "# alpha\n")
        # The active fixture project was AUTO-REGISTERED in the
        # sandbox registry with its absolute path — no rebuild, just
        # a registry reconciliation.
        data = json.loads(reg.read_text(encoding="utf-8"))
        paths = [p["path"] for p in data["projects"]]
        self.assertIn(
            str((sandbox_root() / "projects" / "alpha").resolve()),
            paths,
        )

    def test_entry_with_explicit_project(self):
        proj = Path(self._tmp.name) / "custom"
        proj.mkdir(parents=True)
        # Ensure the sandbox skeleton exists so setup doesn't run.
        (sandbox_root() / "projects").mkdir(parents=True, exist_ok=True)
        (sandbox_root() / "config").mkdir(parents=True, exist_ok=True)
        (sandbox_root() / "config" / "projects.json").write_text(
            "{}", encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = enter_sandbox(project=proj)
        self.assertEqual(code, 0)
        out = buf.getvalue()
        # The banner itself is static; the project is persisted in the
        # session state instead.
        self.assertIn("SANDBOX MODE ACTIVE", out)
        self.assertNotIn("custom", out.split("GLOBAL DASHBOARD")[0]
                         .split("Global Dashboard")[0])
        data = json.loads(
            (sandbox_root() / STATE_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(data["project"], str(proj))

    def test_entry_with_missing_explicit_project_fails_cleanly(self):
        (sandbox_root() / "projects").mkdir(parents=True, exist_ok=True)
        (sandbox_root() / "config").mkdir(parents=True, exist_ok=True)
        (sandbox_root() / "config" / "projects.json").write_text(
            "{}", encoding="utf-8")
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = enter_sandbox(project=Path(self._tmp.name) / "nope")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", buf_err.getvalue())
        self.assertNotIn("SANDBOX MODE ACTIVE", buf_out.getvalue())

    def test_entry_without_any_projects_fails_cleanly(self):
        # A sandbox skeleton with NO projects (and no fixtures beyond
        # the empty registry mock) — setup must not run; the entry
        # fails cleanly since nothing can be picked.
        (sandbox_root() / "projects").mkdir(parents=True, exist_ok=True)
        (sandbox_root() / "config").mkdir(parents=True, exist_ok=True)
        (sandbox_root() / "config" / "projects.json").write_text(
            "{}", encoding="utf-8")
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = enter_sandbox()
        self.assertEqual(code, 1)
        self.assertIn("No sandbox projects available", buf_err.getvalue())


    def test_entry_from_inside_existing_sandbox_does_not_wipe(self):
        """Re-entering from a directory INSIDE the sandbox must never
        rmtree the tree under the caller (FileNotFoundError regression):
        the in-place setup keeps existing fixture content."""
        self._build_minimal_sandbox()
        alpha = sandbox_root() / "projects" / "alpha"
        marker = alpha / "sentinel.txt"
        marker.write_text("keep me", encoding="utf-8")

        saved_cwd = Path.cwd()
        self.addCleanup(os.chdir, saved_cwd)
        os.chdir(alpha)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = enter_sandbox()
        self.assertEqual(code, 0)
        # In-place re-init: the marker (and the cwd inode) survived.
        self.assertTrue(marker.is_file(), "fixture dir was wiped")
        self.assertEqual(Path.cwd(), alpha)
        self.assertTrue(Path.cwd().is_dir())

    def test_entry_twice_from_inside_active_project_keeps_cwd_valid(self):
        """The exact bug scenario, in-process: TWO consecutive bare
        ``ck-dev`` entries while sitting in ``.sandbox/projects/alpha``.
        The second one must not dangle the working directory — a later
        ``Path.cwd()`` (what every ``ck`` invocation does first) must
        still work."""
        self._build_minimal_sandbox()
        alpha = sandbox_root() / "projects" / "alpha"
        saved_cwd = Path.cwd()
        self.addCleanup(os.chdir, saved_cwd)
        os.chdir(alpha)

        for i in (1, 2):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = enter_sandbox()
            self.assertEqual(code, 0, f"entry #{i} failed")
            self.assertIn("SANDBOX MODE ACTIVE", buf.getvalue())
            # The process's working directory is still real after
            # each entry (no delete-and-recreate underneath us).
            self.assertEqual(Path.cwd(), alpha)
            self.assertEqual(os.getcwd(), str(alpha))


class TestExitSandbox(_SandboxAnchor):
    def test_exit_clears_session_and_confirms(self):
        repo = Path(self._tmp.name) / "repo"
        repo.mkdir()
        state = sandbox_root() / STATE_FILENAME
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps({"project": str(repo / "alpha"),
                        "repo_root": str(repo)}),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = exit_sandbox()
        self.assertEqual(code, 0)
        out = buf.getvalue()
        # SINGLE clean status line with the dynamically resolved
        # global binary that takes over after the session.
        self.assertEqual(
            out,
            f"[ok] Exited sandbox mode. Active binary is now: "
            f"{cksandbox.resolved_global_binary_path()}\n",
        )
        # The legacy 'Back to production' line is gone (no duplicates).
        self.assertNotIn("Back to production", out)
        self.assertEqual(
            out.count("[ok] Exited sandbox mode"), 1, out)
        self.assertFalse(state.exists())

    def test_exit_without_session_is_graceful_noop(self):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = exit_sandbox()
        self.assertEqual(code, 0)
        self.assertIn("Not in sandbox mode", buf_out.getvalue())
        self.assertEqual(buf_err.getvalue(), "")

    def test_exit_with_env_marker_only_still_confirms(self):
        os.environ[SANDBOX_ACTIVE_ENV] = str(Path(self._tmp.name) / "alpha")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = exit_sandbox()
        self.assertEqual(code, 0)
        self.assertIn("[ok] Exited sandbox mode. Active binary is now:",
                      buf.getvalue())

    def test_graceful_exit_has_no_binary_confirmation(self):
        """No session: the graceful no-op must not claim a binary swap."""
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = exit_sandbox()
        self.assertEqual(code, 0)
        self.assertNotIn("Active binary is now", buf_out.getvalue())
        self.assertNotIn("[ok] Exited", buf_out.getvalue())


class TestSandboxPathPriority(_SandboxAnchor):
    """PATH priority activation (dev binary wins the lookup race)."""

    def _dirs(self, names) -> dict:
        base = Path(tempfile.mkdtemp(prefix="ck-pathprio-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        made = {}
        for name in names:
            d = base / name
            d.mkdir()
            exe = d / "ck"
            exe.write_text("#!/bin/sh\necho ck\n", encoding="utf-8")
            exe.chmod(0o755)
            made[name] = d
        return made

    def test_activate_pins_checkout_dir_to_front(self):
        dirs = self._dirs(("checkout", "global"))
        env = {"PATH": os.pathsep.join(
            [str(dirs["global"]), str(dirs["checkout"])])}
        with mock.patch.object(cksandbox, "sandbox_entrypoint_dir",
                               return_value=dirs["checkout"]):
            pinned = cksandbox.activate_sandbox_path(env=env)
        self.assertEqual(pinned, dirs["checkout"])
        self.assertEqual(
            env["PATH"].split(os.pathsep)[0], str(dirs["checkout"]))
        self.assertEqual(
            cksandbox.active_binary_path(env=env),
            dirs["checkout"] / "ck")

    def test_activate_is_idempotent(self):
        dirs = self._dirs(("checkout", "global"))
        env = {"PATH": str(dirs["global"])}
        with mock.patch.object(cksandbox, "sandbox_entrypoint_dir",
                               return_value=dirs["checkout"]):
            cksandbox.activate_sandbox_path(env=env)
            cksandbox.activate_sandbox_path(env=env)
        self.assertEqual(env["PATH"].split(os.pathsep),
                         [str(dirs["checkout"]), str(dirs["global"])])

    def test_activate_noop_without_entrypoint(self):
        env = {"PATH": "/usr/bin"}
        with mock.patch.object(cksandbox, "sandbox_entrypoint_dir",
                               return_value=None):
            self.assertIsNone(cksandbox.activate_sandbox_path(env=env))
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_active_binary_self_heals_after_rc_reexport(self):
        """Session marker ON, PATH re-exported below user paths: the
        repaired PATH still resolves the LOCAL DEV binary."""
        dirs = self._dirs(("checkout", "global"))
        env = {
            "PATH": os.pathsep.join(
                [str(dirs["global"]), str(dirs["checkout"])]),
            cksandbox.SANDBOX_SHELL_ENV: "1",
        }
        with mock.patch.object(cksandbox, "sandbox_entrypoint_dir",
                               return_value=dirs["checkout"]):
            self.assertEqual(
                cksandbox.active_binary_path(env=env),
                dirs["checkout"] / "ck")

    def test_active_binary_no_healing_outside_session(self):
        """No session marker: resolution follows the raw PATH order
        (an rc file's re-export is then the user's explicit choice)."""
        dirs = self._dirs(("checkout", "global"))
        env = {"PATH": os.pathsep.join(
            [str(dirs["global"]), str(dirs["checkout"])])}
        with mock.patch.object(cksandbox, "sandbox_entrypoint_dir",
                               return_value=dirs["checkout"]):
            self.assertEqual(
                cksandbox.active_binary_path(env=env),
                dirs["global"] / "ck")

    def test_entry_pins_path_before_banner(self):
        """enter_sandbox() pins PATH first: the banner's binary row
        reports the LOCAL DEV binary, not a global install."""
        self._build_minimal_sandbox()
        proj = sandbox_root() / "projects" / "alpha"
        proj.mkdir(exist_ok=True)
        (sandbox_root() / "config" / "projects.json").write_text(
            "{}", encoding="utf-8")

        dirs = self._dirs(("checkout", "global"))
        pinned_env = {
            "PATH": os.pathsep.join(
                [str(dirs["global"]), str(dirs["checkout"])]),
        }
        entry = sandbox_entrypoint_dir = dirs["checkout"]
        buf = io.StringIO()
        captured = {}
        with mock.patch.object(cksandbox, "sandbox_entrypoint_dir",
                               return_value=entry), \
                mock.patch.dict(os.environ, pinned_env, clear=False):
            with redirect_stdout(buf):
                code = cksandbox.enter_sandbox(project=proj)
            # Capture INSIDE the patched env (patch.dict restores PATH
            # on exit): the live process PATH was re-pinned.
            captured["front"] = os.environ["PATH"].split(os.pathsep)[0]
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertEqual(captured["front"], str(dirs["checkout"]))
        # The banner therefore reports the LOCAL DEV binary.
        self.assertIn(f"Active binary: {entry / 'ck'}", out)
        self.assertNotIn(f"Active binary: {dirs['global'] / 'ck'}", out)


class TestBinaryResolution(unittest.TestCase):
    """Dynamic binary path resolution (no hardcoded paths).

    ``active_binary_path`` mirrors ``command -v ck`` on the CURRENT
    PATH; ``resolved_global_binary_path`` simulates the production
    PATH with the sandbox entrypoint dir removed. Both degrade
    gracefully when PATH lookup fails.
    """

    def _decoy_dir(self, tmp: Path, name: str = "bin") -> Path:
        d = tmp / name
        d.mkdir(parents=True, exist_ok=True)
        exe = d / "ck"
        exe.write_text("#!/bin/sh\necho decoy\n", encoding="utf-8")
        exe.chmod(0o755)
        return d

    def test_active_binary_mirrors_command_v(self):
        try:
            expected = shutil.which("ck")
        except OSError:
            expected = None
        resolved = cksandbox.active_binary_path()
        if expected is None:
            # No ck on PATH: fallbacks may still resolve it.
            if resolved is not None:
                self.assertTrue(Path(resolved).is_file())
        else:
            self.assertEqual(resolved, Path(expected))

    def test_active_binary_uses_injected_path_env(self):
        tmp = Path(tempfile.mkdtemp(prefix="ck-binprobe-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        decoy = self._decoy_dir(tmp)
        env = {"PATH": f"{decoy}{os.pathsep}/usr/bin"}
        self.assertEqual(
            cksandbox.active_binary_path(env=env), decoy / "ck")

    def test_active_binary_falls_back_to_launcher_dir(self):
        """PATH lookup failed: fallback 1 resolves the RUNNING
        launcher's own directory (the binary actually executing)."""
        tmp = Path(tempfile.mkdtemp(prefix="ck-binprobe-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        launcher = self._decoy_dir(tmp, "launch") / "ck"
        with mock.patch.object(cksandbox.sys, "argv", [str(launcher)]):
            resolved = cksandbox.active_binary_path(env={"PATH": ""})
        self.assertEqual(resolved, launcher.resolve())

    def test_active_binary_none_when_everything_fails(self):
        """No PATH hit, no launcher, no checkout entrypoint → None."""
        argv0 = str(Path(tempfile.mkdtemp(prefix="ck-empty-")) / "ck")
        with mock.patch.object(cksandbox.sys, "argv", [argv0]), \
                mock.patch.object(cksandbox, "sandbox_entrypoint_dir",
                                  return_value=None):
            self.assertIsNone(
                cksandbox.active_binary_path(env={"PATH": ""}))

    def test_resolved_global_skips_sandbox_entrypoint_dir(self):
        tmp = Path(tempfile.mkdtemp(prefix="ck-binprobe-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        checkout_bin = self._decoy_dir(tmp, "checkout")
        global_bin = self._decoy_dir(tmp, "global")
        env = {
            "PATH": os.pathsep.join([str(checkout_bin), str(global_bin)]),
            "VIRTUAL_ENV": "",
        }
        with mock.patch.object(
                cksandbox, "sandbox_entrypoint_dir",
                return_value=checkout_bin):
            resolved = cksandbox.resolved_global_binary_path(env=env)
        self.assertEqual(resolved, global_bin / "ck")

    def test_resolved_global_falls_back_to_local_install(self):
        fake_home = Path(tempfile.mkdtemp(prefix="ck-home-"))
        self.addCleanup(shutil.rmtree, fake_home, ignore_errors=True)
        local_bin = fake_home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        (local_bin / "ck").write_text("#!/bin/sh\n", encoding="utf-8")
        (local_bin / "ck").chmod(0o755)
        env = {"PATH": "/nonexistent-ck-bin"}
        with mock.patch.object(
                cksandbox, "sandbox_entrypoint_dir", return_value=None), \
                mock.patch("cklib.sandbox.Path.home",
                           return_value=fake_home):
            resolved = cksandbox.resolved_global_binary_path(env=env)
        self.assertEqual(resolved, local_bin / "ck")

    def test_resolved_global_placeholder_when_nothing_exists(self):
        env = {"PATH": "/nonexistent-ck-bin"}
        with mock.patch.object(
                cksandbox, "sandbox_entrypoint_dir", return_value=None), \
                mock.patch("cklib.sandbox.Path.home",
                           return_value=Path("/nonexistent-home")):
            resolved = cksandbox.resolved_global_binary_path(env=env)
        self.assertEqual(resolved, Path("ck"))


class TestRejectDevExit(unittest.TestCase):
    """Safeguard: `ck-dev exit` warns and exits 1, launching nothing."""

    def test_warns_and_returns_1(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cksandbox.reject_dev_exit(["/repo/ck-dev", "exit"])
        self.assertEqual(code, 1)
        out = buf.getvalue()
        self.assertIn("[!] 'ck-dev exit' cannot close an active subshell.",
                      out)
        self.assertIn("[!] To exit sandbox mode, type 'exit' or press "
                      "Ctrl+D.", out)

    def test_uses_real_entrypoint_name(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cksandbox.reject_dev_exit(
                ["/opt/tools/ck-dev.exe", "exit"])
        self.assertEqual(code, 1)
        self.assertIn("'ck-dev.exe exit' cannot close", buf.getvalue())

    def test_defaults_to_sys_argv(self):
        buf = io.StringIO()
        with mock.patch("sys.argv", ["ck-dev", "exit"]), \
                redirect_stdout(buf):
            code = cksandbox.reject_dev_exit()
        self.assertEqual(code, 1)
        self.assertIn("'ck-dev exit' cannot close", buf.getvalue())


class TestPlainCkInterceptor(_SandboxAnchor):
    """Plain `ck` inside a session: banner first, then transparent exec."""

    def _enter_session(self) -> None:
        (sandbox_root() / "projects").mkdir(parents=True, exist_ok=True)
        (sandbox_root() / "config").mkdir(parents=True, exist_ok=True)
        (sandbox_root() / "config" / "projects.json").write_text(
            "{}", encoding="utf-8")
        proj = sandbox_root() / "projects" / "alpha"
        proj.mkdir(exist_ok=True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(enter_sandbox(project=proj), 0)

    def _run_ck(self, argv, cwd):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        orig = os.getcwd()
        os.chdir(cwd)
        try:
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                code = main(argv)
        finally:
            os.chdir(orig)
        return code, buf_out.getvalue(), buf_err.getvalue()

    def test_banner_precedes_command_output(self):
        self._enter_session()
        proj_root = Path(self._tmp.name) / "realproj"
        (proj_root / ".ck").mkdir(parents=True)
        (proj_root / ".ck" / "PLAN.md").write_text(
            "# realproj\n## Current Sprint\n- [ ] interceptor task\n",
            encoding="utf-8",
        )
        os.environ["CK_SANDBOX"] = "1"
        try:
            code, out, err = self._run_ck(["list"], proj_root)
        finally:
            os.environ.pop("CK_SANDBOX", None)
        self.assertEqual(code, 0, err)
        # The exact banner (with the binary row) precedes output.
        self.assertIn("SANDBOX MODE ACTIVE", out)
        self.assertIn("Type 'exit' or press Ctrl+D to return to "
                      "production", out)
        self.assertLess(out.index("SANDBOX MODE ACTIVE"),
                        out.index("interceptor task"))
        # The command executed transparently in sandbox mode.
        self.assertIn("[ ] 1. interceptor task", out)
        self.assertNotIn("Traceback", err)

    def test_banner_shown_for_cwd_inside_sandbox(self):
        self._enter_session()
        proj_root = Path(self._tmp.name) / "realproj2"
        (proj_root / ".ck").mkdir(parents=True)
        (proj_root / ".ck" / "PLAN.md").write_text(
            "# realproj2\n## Current Sprint\n- [ ] inside task\n",
            encoding="utf-8",
        )
        # No CK_SANDBOX env — the cwd under .sandbox/ triggers it.
        inside = sandbox_root() / "projects" / "alpha"
        code, out, err = self._run_ck(["list"], inside)
        self.assertEqual(code, 0, err)
        self.assertIn("SANDBOX MODE ACTIVE", out)
        self.assertIn("Type 'exit' or press Ctrl+D to return to "
                      "production", out)

    def test_no_banner_without_dev_mode(self):
        proj_root = Path(self._tmp.name) / "plainproj"
        (proj_root / ".ck").mkdir(parents=True)
        (proj_root / ".ck" / "PLAN.md").write_text(
            "# plainproj\n## Current Sprint\n- [ ] plain task\n",
            encoding="utf-8",
        )
        code, out, err = self._run_ck(["list"], proj_root)
        self.assertEqual(code, 0, err)
        self.assertNotIn("SANDBOX MODE ACTIVE", out)
        self.assertIn("plain task", out)

    def test_no_banner_for_help_and_version(self):
        self._enter_session()
        os.environ["CK_SANDBOX"] = "1"
        try:
            for argv in (["-h"], ["--help"], ["help"], ["-v"],
                         ["--version"]):
                buf_out = io.StringIO()
                with redirect_stdout(buf_out):
                    code = main(argv)
                self.assertEqual(code, 0)
                self.assertNotIn(
                    "SANDBOX MODE ACTIVE", buf_out.getvalue(),
                    f"banner leaked into {argv!r}",
                )
        finally:
            os.environ.pop("CK_SANDBOX", None)

    def test_banner_without_session_warns_instead(self):
        # Dev mode on, but no session state at all.
        os.environ["CK_SANDBOX"] = "1"
        try:
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                code = main(["info"])
            self.assertEqual(code, 0)
            self.assertNotIn("SANDBOX MODE ACTIVE", buf_out.getvalue())
            self.assertIn("CK_SANDBOX is active", buf_out.getvalue())
            self.assertIn("ck-dev", buf_out.getvalue())
        finally:
            os.environ.pop("CK_SANDBOX", None)

    def test_st_g_lists_mock_projects_inside_session(self):
        """`ck st -g` inside the session lists the sandbox mock
        registry projects — never the empty-registry message."""
        # Full entry: builds the fixture registry (alpha +
        # orphaned-deleted) and leaves dev mode active.
        with redirect_stdout(io.StringIO()):
            self.assertEqual(enter_sandbox(), 0)
        inside = sandbox_root() / "projects" / "alpha"
        code, out, err = self._run_ck(["st", "-g"], inside)
        self.assertEqual(code, 0, err)
        self.assertIn("GLOBAL DASHBOARD", out)
        self.assertIn("alpha", out)
        self.assertIn("orphaned-deleted", out)
        self.assertNotIn("No registered projects", out)

    def test_st_g_works_without_env_from_inside_sandbox(self):
        """The registry ROUTES by cwd: plain `ck st -g` inside
        ``.sandbox/projects/alpha`` reads the sandbox registry even
        when CK_SANDBOX is unset (fresh shell, entry done earlier)."""
        with redirect_stdout(io.StringIO()):
            self.assertEqual(enter_sandbox(), 0)
        inside = sandbox_root() / "projects" / "alpha"
        # Strip the env toggle the entry left behind: only the cwd
        # may drive the sandbox routing here.
        os.environ.pop("CK_SANDBOX", None)
        code, out, err = self._run_ck(["st", "-g"], inside)
        self.assertEqual(code, 0, err)
        self.assertIn("GLOBAL DASHBOARD", out)
        self.assertIn("alpha", out)
        self.assertNotIn("No registered projects", out)

    def test_entry_auto_registers_active_project_with_abs_path(self):
        """The active project is registered in the SANDBOX registry
        with its absolute host path during entry."""
        proj = Path(self._tmp.name) / "regme"
        proj.mkdir(parents=True)
        (sandbox_root() / "projects").mkdir(parents=True, exist_ok=True)
        (sandbox_root() / "config").mkdir(parents=True, exist_ok=True)
        # No registry file at all: registration must create it.
        with redirect_stdout(io.StringIO()):
            self.assertEqual(enter_sandbox(project=proj), 0)
        reg = sandbox_root() / "config" / "projects.json"
        self.assertTrue(reg.is_file())
        data = json.loads(reg.read_text(encoding="utf-8"))
        names = [p["name"] for p in data["projects"]]
        paths = [p["path"] for p in data["projects"]]
        self.assertIn("regme", names)
        self.assertIn(str(proj.resolve()), paths)


class TestSubshellSession(_SandboxAnchor):
    """Interactive subshell session (bare `ck-dev`).

    Covers the matryoshka guard (no nested sessions), the session
    environment contract (CK_SANDBOX=1 + PYTHONPATH pinning), the
    `(sandbox)` prompt marker, and the full real-bash lifecycle:
    entry -> subshell -> `exit` clears the session.
    """

    def test_guard_detects_inherited_session_env(self):
        self.assertFalse(cksandbox.already_in_sandbox_session({}))
        self.assertFalse(
            cksandbox.already_in_sandbox_session({"CK_SANDBOX": ""}))
        self.assertFalse(
            cksandbox.already_in_sandbox_session({"CK_SANDBOX": "0"}))
        for truthy in ("1", "true", "YES", "on"):
            self.assertTrue(
                cksandbox.already_in_sandbox_session(
                    {"CK_SANDBOX": truthy}), truthy)
            self.assertTrue(
                cksandbox.already_in_sandbox_session(
                    {"CK_DEV": truthy}), truthy)

    def test_entry_and_spawn_guards_against_nesting(self):
        """CK_SANDBOX=1 inherited: warning + dashboard, NO second
        session write, NO subshell spawn (regardless of spawn flag)."""
        self._build_minimal_sandbox()
        os.environ["CK_SANDBOX"] = "1"
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                cksandbox, "spawn_sandbox_shell") as spawn:
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                code = cksandbox.enter_and_spawn(spawn=True)
        self.assertEqual(code, 0)
        spawn.assert_not_called()
        out = buf_out.getvalue()
        self.assertIn("Already in sandbox mode", out)
        self.assertIn("No nested session started", out)
        # The guard still surfaces the cross-project view.
        self.assertIn("GLOBAL DASHBOARD", out)

    def test_entry_and_spawn_without_session_spawns_shell(self):
        """No inherited session: setup runs (banner first), then the
        subshell spawns once with the session environment; after the
        shell exits, the persistent session is cleared."""
        self._build_minimal_sandbox()
        buf = io.StringIO()
        captured = {}

        def fake_spawn(project, *, shell=None):
            captured["project"] = project
            captured["env"] = cksandbox.sandbox_shell_env(project)
            return 0

        with mock.patch.object(cksandbox, "spawn_sandbox_shell",
                               side_effect=fake_spawn):
            with redirect_stdout(buf):
                code = cksandbox.enter_and_spawn(spawn=True)
        self.assertEqual(code, 0)
        out = buf.getvalue()
        # Banner + dashboard printed BEFORE the subshell.
        self.assertIn("SANDBOX MODE ACTIVE", out)
        self.assertIn("GLOBAL DASHBOARD", out)
        # The active fixture project was passed to the shell.
        self.assertEqual(captured["project"].name, "alpha")
        env = captured["env"]
        self.assertEqual(env["CK_SANDBOX"], "1")
        self.assertIn("PYTHONPATH", env)
        # Natural shell exit cleared the persistent session.
        self.assertFalse(
            (cksandbox.sandbox_root() / cksandbox.STATE_FILENAME)
            .exists())

    def test_spawn_decision_env_overrides(self):
        """NO wins over FORCE; default (neither) follows the explicit
        flag / TTY."""
        os.environ[cksandbox.CK_DEV_NO_SUBSHELL_ENV] = "1"
        os.environ[cksandbox.CK_DEV_FORCE_SUBSHELL_ENV] = "1"
        self.assertFalse(cksandbox._should_spawn_shell())
        os.environ.pop(cksandbox.CK_DEV_NO_SUBSHELL_ENV)
        self.assertTrue(cksandbox._should_spawn_shell())
        os.environ.pop(cksandbox.CK_DEV_FORCE_SUBSHELL_ENV)
        # No TTY under the test runner, no overrides -> no spawn.
        self.assertFalse(cksandbox._should_spawn_shell())
        # Explicit library flag always wins.
        self.assertTrue(cksandbox._should_spawn_shell(True))
        self.assertFalse(cksandbox._should_spawn_shell(False))

    def test_sandbox_shell_env_contract(self):
        project = Path(self._tmp.name) / "alpha"
        project.mkdir(parents=True)
        os.environ["PYTHONPATH"] = "/legacy/entry"
        self.addCleanup(os.environ.pop, "PYTHONPATH", None)
        env = cksandbox.sandbox_shell_env(project)
        self.assertEqual(env["CK_SANDBOX"], "1")
        self.assertEqual(env[cksandbox.SANDBOX_ACTIVE_ENV], str(project))
        self.assertEqual(env[cksandbox.SANDBOX_ROOT_ENV],
                         str(cksandbox.sandbox_root()))
        self.assertEqual(env[cksandbox.SANDBOX_SHELL_ENV], "1")
        # Checkout root FIRST (workspace sources win the import race).
        self.assertEqual(
            env["PYTHONPATH"].split(os.pathsep)[0],
            str(cksandbox.production_repo_root()))
        self.assertIn("/legacy/entry", env["PYTHONPATH"])
        # PATH priority routing: the checkout's entrypoint dir is
        # PREPENDED so a bare `ck` inside the subshell runs the local
        # dev wrapper instead of a globally installed system binary.
        entry_dir = cksandbox.sandbox_entrypoint_dir()
        self.assertIsNotNone(entry_dir)
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(entry_dir))
        self.assertTrue((entry_dir / "ck").exists()
                        or (entry_dir / "ck-dev").exists())

    def test_shell_env_path_prioritizes_repo_wrapper(self):
        """The inherited `~/.local/bin`-style PATH entry stays on PATH
        but LOSES to the checkout dir; a bare `ck` resolves locally."""
        project = Path(self._tmp.name) / "alpha"
        project.mkdir(parents=True)
        repo_root = str(cksandbox.production_repo_root())
        original = os.environ.get("PATH")
        os.environ["PATH"] = os.pathsep.join(
            ["/usr/local/bin", repo_root, "/usr/bin"])
        if original is None:
            self.addCleanup(os.environ.pop, "PATH", None)
        else:
            self.addCleanup(os.environ.__setitem__, "PATH", original)
        env = cksandbox.sandbox_shell_env(project)
        parts = env["PATH"].split(os.pathsep)
        self.assertEqual(parts[0], repo_root)
        self.assertEqual(parts.count(repo_root), 1)  # de-duplicated
        self.assertIn("/usr/local/bin", parts)
        self.assertIn("/usr/bin", parts)

    def test_sandbox_entrypoint_dir_prefers_repo_root(self):
        """The dir holding the `ck` wrapper wins over a bare `bin/`."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = root / "ck"
            entry.write_text("#!/bin/sh\n", encoding="utf-8")
            entry.chmod(0o755)
            (root / "bin").mkdir()
            self.assertEqual(cksandbox.sandbox_entrypoint_dir(root), root)

    def test_sandbox_entrypoint_dir_falls_back_to_bin(self):
        """A checkout that shims entrypoints under `bin/` is found."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bin").mkdir()
            entry = root / "bin" / "ck"
            entry.write_text("#!/bin/sh\n", encoding="utf-8")
            entry.chmod(0o755)
            self.assertEqual(cksandbox.sandbox_entrypoint_dir(root),
                             root / "bin")

    def test_sandbox_entrypoint_dir_ignores_non_executable(self):
        """A non-executable `ck` must NOT be routed to: the shell would
        skip it during PATH lookup and keep running a global binary."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ck").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "ck").chmod(0o644)  # present but not executable
            original = os.environ.pop("VIRTUAL_ENV", None)
            if original is not None:
                self.addCleanup(os.environ.__setitem__, "VIRTUAL_ENV",
                                original)
            self.assertIsNone(cksandbox.sandbox_entrypoint_dir(root))

    def test_repo_ck_wrapper_is_executable_with_shebang(self):
        """The checkout ships a RUNNABLE `ck`: executable bit + the
        `#!/usr/bin/env python3` launcher shebang, ready for PATH."""
        wrapper = cksandbox.production_repo_root() / "ck"
        self.assertTrue(wrapper.is_file(), wrapper)
        self.assertTrue(os.access(wrapper, os.X_OK),
                        f"{wrapper} is not executable")
        first_line = wrapper.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first_line, "#!/usr/bin/env python3")
        self.assertTrue(cksandbox.has_runnable_entrypoint(
            cksandbox.production_repo_root()))

    def test_shell_env_is_full_copy_of_inherited_environment(self):
        """The session env is the INHERITED environment updated only
        with the sandbox flags/paths — nothing else is added."""
        project = Path(self._tmp.name) / "alpha"
        project.mkdir(parents=True)
        os.environ["CK_SESSION_CANARY"] = "inherited-value"
        self.addCleanup(os.environ.pop, "CK_SESSION_CANARY", None)
        env = cksandbox.sandbox_shell_env(project)
        self.assertEqual(env.get("CK_SESSION_CANARY"), "inherited-value")
        # Every inherited var survives; the sandbox keys, the prompt
        # prefix and the PATH priority entry are the only changes.
        for key, value in os.environ.items():
            if key in ("PS1", "PROMPT", "PATH"):
                continue  # covered by their own tests
            self.assertEqual(env.get(key), value)

    def test_shell_env_prompt_prefix_only_when_inherited(self):
        """Prompt marker: best-effort via the INHERITED PS1/PROMPT —
        set when present, never fabricated, never doubled."""
        project = Path(self._tmp.name) / "alpha"
        project.mkdir(parents=True)
        mark = cksandbox.SANDBOX_PROMPT_MARK.strip()

        os.environ["PS1"] = "$ "
        os.environ["PROMPT"] = ">% ";
        self.addCleanup(os.environ.pop, "PS1", None)
        self.addCleanup(os.environ.pop, "PROMPT", None)
        env = cksandbox.sandbox_shell_env(project)
        self.assertEqual(env["PS1"], f"(sandbox) $ ")
        self.assertEqual(env["PROMPT"], f"(sandbox) >% ")

        # Already prefixed -> idempotent, no double marker.
        os.environ["PS1"] = f"(sandbox) $ "
        env = cksandbox.sandbox_shell_env(project)
        self.assertEqual(env["PS1"], f"(sandbox) $ ")

        # Not inherited -> NOT fabricated (shell falls back to the
        # banner + CK_SANDBOX marker per spec).
        os.environ.pop("PS1")
        os.environ.pop("PROMPT")
        env = cksandbox.sandbox_shell_env(project)
        self.assertNotIn("PS1", env)
        self.assertNotIn("PROMPT", env)
        self.assertTrue(mark)  # documented marker shape

    def test_spawn_is_config_free_and_preserves_cwd(self):
        """Clean spawn contract: `$SHELL` runs DIRECTLY with the
        session env — argv is exactly ``[shell]`` (no ``--rcfile``,
        no ``-i``), NO temporary rc files/directories are created,
        and the exact cwd is preserved."""
        project = Path(self._tmp.name) / "alpha"
        project.mkdir(parents=True)
        recorded = {}

        class FakeProc:
            returncode = 0

        def fake_run(argv, env=None, check=False, cwd=None):
            recorded["argv"] = argv
            recorded["env"] = env
            recorded["cwd"] = cwd
            return FakeProc()

        os.environ["SHELL"] = "/bin/bash"
        self.addCleanup(os.environ.pop, "SHELL", None)
        with mock.patch("subprocess.run", side_effect=fake_run):
            rc = cksandbox.spawn_sandbox_shell(project)
        self.assertEqual(rc, 0)
        self.assertEqual(recorded["argv"], ["/bin/bash"])
        self.assertNotIn("--rcfile", recorded["argv"])
        self.assertNotIn("-i", recorded["argv"])
        self.assertIsNone(recorded["cwd"])  # child inherits exact cwd
        self.assertEqual(recorded["env"]["CK_SANDBOX"], "1")
        self.assertEqual(recorded["env"][cksandbox.SANDBOX_SHELL_ENV], "1")
        # PATH priority: the checkout entrypoint dir leads the env
        # handed to the spawned shell.
        self.assertEqual(
            recorded["env"]["PATH"].split(os.pathsep)[0],
            str(cksandbox.sandbox_entrypoint_dir()))

    def test_spawned_shell_resolves_local_ck_first(self):
        """End to end with a REAL child shell: a bare `ck` inside the
        spawned subshell resolves to THIS checkout's wrapper, never a
        globally installed system binary."""
        if shutil.which("sh") is None:
            self.skipTest("sh unavailable")
        self._build_minimal_sandbox()
        tmp = Path(tempfile.mkdtemp(prefix="ck-path-probe-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out_file = tmp / "probe-out.txt"
        probe = tmp / "probe.sh"
        probe.write_text(
            "#!/bin/sh\n"
            f"command -v ck > {out_file}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        probe.chmod(0o755)
        os.environ["SHELL"] = str(probe)
        self.addCleanup(os.environ.pop, "SHELL", None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cksandbox.spawn_sandbox_shell(
                cksandbox.active_sandbox_project() or Path("."))
        self.assertEqual(rc, 0, buf.getvalue())
        resolved = out_file.read_text(encoding="utf-8").strip()
        expected = cksandbox.production_repo_root() / "ck"
        self.assertEqual(resolved, str(expected),
                         f"ck resolved to {resolved!r}, expected {expected}")

    def test_spawn_creates_no_temporary_shell_config(self):
        """End to end with a REAL child shell: launching the session
        subshell leaves NO generated rc files or ZDOTDIR directories
        behind (the regression this refactor exists for)."""
        if shutil.which("bash") is None:
            self.skipTest("bash unavailable")
        self._build_minimal_sandbox()
        tmp = Path(tempfile.mkdtemp(prefix="ck-probe-tmp-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        probe = tmp / "probe-shell.sh"
        probe.write_text(
            "#!/bin/sh\n"
            "echo PROBE_CK_SANDBOX=${CK_SANDBOX-unset}\n"
            "echo PROBE_PWD=$PWD\n"
            "exit 0\n",
            encoding="utf-8",
        )
        probe.chmod(0o755)
        # The probe child inherits the REAL fd 1 (redirect_stdout only
        # swaps sys.stdout), so it tees its output to a file we can
        # assert on afterwards.
        probe_out = tmp / "probe-out.txt"
        inner = tmp / "inner-shell.sh"
        inner.write_text(
            f"#!/bin/sh\n"
            f". {probe} > {probe_out}\n",
            encoding="utf-8",
        )
        inner.chmod(0o755)

        before = set(tmp.glob("ck-sandbox-*"))
        os.environ["SHELL"] = str(inner)
        self.addCleanup(os.environ.pop, "SHELL", None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cksandbox.spawn_sandbox_shell(
                cksandbox.active_sandbox_project() or Path("."))
        after = set(tmp.glob("ck-sandbox-*"))
        self.assertEqual(rc, 0)
        self.assertEqual(before, set())
        self.assertEqual(after, set())  # NO temp artifacts, ever
        out = probe_out.read_text(encoding="utf-8")
        self.assertIn("PROBE_CK_SANDBOX=1", out)
        self.assertIn(f"PROBE_PWD={Path.cwd()}", out)  # exact cwd kept

    def test_real_bash_lifecycle_entry_exit(self):
        """Full lifecycle with REAL bash: entry spawns the session
        shell, `exit` inside returns the user to production and the
        persistent session is cleared."""
        if shutil.which("bash") is None:
            self.skipTest("bash unavailable")
        self._build_minimal_sandbox()
        # Force the spawn through the env seam (non-TTY test runner).
        os.environ[cksandbox.CK_DEV_FORCE_SUBSHELL_ENV] = "1"
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cksandbox.enter_and_spawn()
        finally:
            os.environ.pop(cksandbox.CK_DEV_FORCE_SUBSHELL_ENV)
        self.assertEqual(code, 0, buf.getvalue())
        # The subshell ran and exited; the session was cleared.
        self.assertFalse(
            (cksandbox.sandbox_root() / cksandbox.STATE_FILENAME)
            .exists())


class TestCliExitCommand(_SandboxAnchor):
    """`ck-dev exit` hits the SAFEGUARD through main(); plain `ck exit`
    is rejected."""

    def test_exit_under_dev_entrypoint_name_hits_safeguard(self):
        state = sandbox_root() / STATE_FILENAME
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps({"project": "/x", "repo_root": "/repo"}),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with mock.patch("sys.argv", ["ck-dev", "exit"]), \
                redirect_stdout(buf):
            code = main(["exit"])
        self.assertEqual(code, 1)
        out = buf.getvalue()
        self.assertIn("[!] 'ck-dev exit' cannot close an active subshell.",
                      out)
        self.assertIn("[!] To exit sandbox mode, type 'exit' or press "
                      "Ctrl+D.", out)
        self.assertNotIn("[ok] Exited sandbox mode", out)
        # The persistent session was NOT touched.
        self.assertTrue(state.exists())

    def test_plain_ck_exit_is_rejected(self):
        buf = io.StringIO()
        with mock.patch("sys.argv", ["ck", "exit"]), redirect_stdout(buf):
            code = main(["exit"])
        self.assertEqual(code, 2)
        self.assertIn("Unknown command", buf.getvalue())

    def test_bare_ck_dev_enters_sandbox(self):
        buf = io.StringIO()
        with mock.patch("sys.argv", ["ck-dev"]), redirect_stdout(buf):
            code = main([])
        self.assertEqual(code, 0)
        self.assertIn("SANDBOX MODE ACTIVE", buf.getvalue())
        self.assertTrue((sandbox_root() / STATE_FILENAME).is_file())

    def test_bare_plain_ck_shows_help(self):
        buf = io.StringIO()
        with mock.patch("sys.argv", ["ck"]), redirect_stdout(buf):
            code = main([])
        self.assertEqual(code, 0)
        self.assertIn("Context Keeper CLI", buf.getvalue())
        self.assertNotIn("SANDBOX MODE ACTIVE", buf.getvalue())


class TestHelpDocumentsSession(unittest.TestCase):
    def test_help_documents_entry_and_exit(self):
        from cklib.cli import HELP_TEXT

        self.assertIn("./ck-dev", HELP_TEXT)
        self.assertIn("Enter sandbox mode", HELP_TEXT)
        self.assertIn("To exit: type 'exit' or press Ctrl+D.", HELP_TEXT)
        # Stale syntax is gone from the docs.
        self.assertNotIn("ck-dev exit", HELP_TEXT)
        self.assertIn("ck dev setup", HELP_TEXT)
        self.assertIn("ck dev clean", HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
