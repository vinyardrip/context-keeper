"""Tests for ``ck completion`` (Bash / Zsh shell completion).

Coverage:

- ``ck completion bash`` / ``ck completion zsh`` print a script to
  STDOUT (never a traceback) that the respective shell accepts
  syntactically (``bash -n`` / ``zsh -n``).
- The registry helper (``ck completion --list-projects``) emits the
  registered project names, one per line, newest first, with the
  folder name as a fallback for entries lacking a display name.
- A missing, empty, unreadable, corrupt or lock-contended registry
  degrades to an empty result with exit code 0 and no traceback — and
  a missing registry is not materialized (no ``~/.config/ck/`` or
  lock file is created by a completion probe).
- The generated Bash script really parses names out of the registry
  mock and completes them for ``ck <TAB>``, ``ck st <TAB>`` and
  ``ck list <TAB>``. ``ck st`` accepts the project as its first
  positional only: after a project name or a flag it switches to
  valid options instead of repeating project names.
- The Zsh script wires ``_ck_projects`` into the ``st`` positional and
  applies the same first-positional rule (Bash and Zsh symmetry).
- ``ck completion --list-spaces`` follows the persisted ``spaces_order``
  and includes the virtual ``project`` row plus built-in/custom spaces.
  Both shells complete those names for ``ck space
  move|swap|rename|delete``, filtering protected names (``project``,
  built-ins) and the already-selected first operand of ``swap``.
- Both generated scripts invoke the dynamic helpers through
  ``"${CK_BIN:-ck}"`` (never a bare ``command ck``); with ``CK_BIN``
  set and no ``ck`` on ``PATH``, project and space completion still
  works in Bash and Zsh.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cklib import completion
from cklib import config as ckconfig
from cklib import registry as ckregistry
from cklib import spaces
from cklib.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
ZSH = shutil.which("zsh")


class IsolatedRegistry(unittest.TestCase):
    """Pin the global registry into a per-test temporary HOME."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

        self._orig = (
            ckconfig.GLOBAL_CONFIG_DIR,
            ckconfig.GLOBAL_REGISTRY_FILE,
            ckconfig.GLOBAL_STATE_FILE,
            ckconfig.LEGACY_GLOBAL_CONFIG_FILE,
            ckregistry.GLOBAL_CONFIG_DIR,
            ckregistry.GLOBAL_REGISTRY_FILE,
            ckregistry.LEGACY_GLOBAL_CONFIG_FILE,
        )
        new_dir = self.home / ".config" / "ck"
        new_file = new_dir / "projects.json"
        new_legacy = self.home / ".ckrc"
        ckconfig.GLOBAL_CONFIG_DIR = new_dir
        ckconfig.GLOBAL_REGISTRY_FILE = new_file
        ckconfig.GLOBAL_STATE_FILE = new_dir / "state.json"
        ckconfig.LEGACY_GLOBAL_CONFIG_FILE = new_legacy
        ckregistry.GLOBAL_CONFIG_DIR = new_dir
        ckregistry.GLOBAL_REGISTRY_FILE = new_file
        ckregistry.LEGACY_GLOBAL_CONFIG_FILE = new_legacy

        def _restore() -> None:
            (ckconfig.GLOBAL_CONFIG_DIR,
             ckconfig.GLOBAL_REGISTRY_FILE,
             ckconfig.GLOBAL_STATE_FILE,
             ckconfig.LEGACY_GLOBAL_CONFIG_FILE,
             ckregistry.GLOBAL_CONFIG_DIR,
             ckregistry.GLOBAL_REGISTRY_FILE,
             ckregistry.LEGACY_GLOBAL_CONFIG_FILE) = self._orig
        self.addCleanup(_restore)

    @property
    def registry_file(self) -> Path:
        return ckconfig.GLOBAL_REGISTRY_FILE

    @property
    def state_file(self) -> Path:
        return ckconfig.GLOBAL_STATE_FILE

    @property
    def spaces_dir(self) -> Path:
        return ckconfig.GLOBAL_CONFIG_DIR / "spaces"

    def write_registry(self, projects: list) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        self.registry_file.write_text(
            json.dumps({"projects": projects}), encoding="utf-8")

    def write_spaces(self, names: list, order=None) -> None:
        """Create real space files and, optionally, a persisted order."""
        self.spaces_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            (self.spaces_dir / f"{name}.md").write_text(
                f"# {name}\n\n## Current Sprint\n\n## Completed\n",
                encoding="utf-8",
            )
        if order is not None:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({"spaces_order": order}, indent=2),
                encoding="utf-8",
            )


def _run_main(argv):
    """Run the CLI in-process, capturing stdout/stderr and the code."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def _entry(name: str, path: str, last_seen: str) -> dict:
    return {
        "name": name,
        "path": path,
        "registered_at": "2026-01-01T00:00:00+00:00",
        "last_seen": last_seen,
    }


# ---------------------------------------------------------------------------
# Script output and syntax
# ---------------------------------------------------------------------------


class TestCompletionScripts(unittest.TestCase):

    def test_bash_script_goes_to_stdout(self):
        rc, out, err = _run_main(["completion", "bash"])
        self.assertEqual(rc, 0, err)
        self.assertIn("complete -F _ck_completion ck", out)
        self.assertIn('"${CK_BIN:-ck}" completion --list-projects', out)
        self.assertIn('"${CK_BIN:-ck}" completion --list-spaces', out)
        self.assertNotIn("command ck", out)
        for cmd in ("st", "list", "dashboard", "add", "done", "completion"):
            self.assertIn(cmd, out)
        self.assertNotIn("Traceback", out)

    def test_zsh_script_goes_to_stdout(self):
        rc, out, err = _run_main(["completion", "zsh"])
        self.assertEqual(rc, 0, err)
        self.assertIn("#compdef ck", out)
        self.assertIn("compdef _ck ck", out)
        self.assertIn("_ck_projects", out)
        self.assertIn("_ck_spaces", out)
        self.assertIn('"${CK_BIN:-ck}" completion --list-projects', out)
        self.assertIn('"${CK_BIN:-ck}" completion --list-spaces', out)
        self.assertNotIn("command ck", out)
        self.assertNotIn("Traceback", out)

    @unittest.skipUnless(BASH, "bash is not installed")
    def test_bash_script_is_valid_syntax(self):
        rc, out, _ = _run_main(["completion", "bash"])
        self.assertEqual(rc, 0)
        with tempfile.NamedTemporaryFile(
                "w", suffix=".bash", delete=False) as fh:
            fh.write(out)
            path = fh.name
        self.addCleanup(os.unlink, path)
        result = subprocess.run([BASH, "-n", path],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(ZSH, "zsh is not installed")
    def test_zsh_script_is_valid_syntax(self):
        rc, out, _ = _run_main(["completion", "zsh"])
        self.assertEqual(rc, 0)
        with tempfile.NamedTemporaryFile(
                "w", suffix=".zsh", delete=False) as fh:
            fh.write(out)
            path = fh.name
        self.addCleanup(os.unlink, path)
        result = subprocess.run([ZSH, "-n", path],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_completion_stays_navigation_focused(self):
        """Supported shells and commands are a curated set — no
        internal dev/install commands spammed at the user."""
        self.assertEqual(completion.SUPPORTED_SHELLS, ("bash", "zsh"))
        offered = {name for name, _ in completion.COMPLETION_COMMANDS}
        self.assertIn("st", offered)
        self.assertIn("list", offered)
        self.assertIn("completion", offered)
        self.assertNotIn("ck-clean", offered)
        self.assertNotIn("uninstall", offered)

    def test_script_generation_rejects_unsupported_shell(self):
        with self.assertRaises(ValueError):
            completion.script_for("fish")

    def test_missing_shell_prints_usage(self):
        rc, out, _ = _run_main(["completion"])
        self.assertEqual(rc, 2)
        self.assertIn("Usage: ck completion <bash|zsh>", out)

    def test_unknown_shell_is_a_clean_error(self):
        rc, out, _ = _run_main(["completion", "fish"])
        self.assertEqual(rc, 2)
        self.assertIn("Unsupported shell", out)
        self.assertNotIn("Traceback", out)

    def test_unknown_flag_is_a_clean_error(self):
        rc, out, _ = _run_main(["completion", "--shout"])
        self.assertEqual(rc, 2)
        self.assertIn("Unknown flag", out)


# ---------------------------------------------------------------------------
# Registry -> project names
# ---------------------------------------------------------------------------


class TestProjectNameListing(IsolatedRegistry):

    def test_registered_names_newest_first(self):
        self.write_registry([
            _entry("alpha", "/tmp/alpha", "2026-10-01T00:00:00+00:00"),
            _entry("beta", "/tmp/beta", "2026-10-02T00:00:00+00:00"),
        ])
        rc, out, _ = _run_main(["completion", "--list-projects"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["beta", "alpha"])

    def test_names_are_deduplicated_and_namespace_clean(self):
        self.write_registry([
            _entry("alpha", "/tmp/alpha-one", "2026-10-02T00:00:00+00:00"),
            _entry("alpha", "/tmp/alpha-two", "2026-10-01T00:00:00+00:00"),
            _entry("", "/tmp/gamma", "2026-09-01T00:00:00+00:00"),
        ])
        rc, out, _ = _run_main(["completion", "--list-projects"])
        self.assertEqual(rc, 0)
        # Deduplicated; an empty display name falls back to the folder.
        self.assertEqual(out.splitlines(), ["alpha", "gamma"])

    def test_empty_registry_yields_nothing(self):
        self.write_registry([])
        rc, out, _ = _run_main(["completion", "--list-projects"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_missing_registry_is_silent_and_unmaterialized(self):
        self.assertFalse(self.registry_file.exists())
        rc, out, err = _run_main(["completion", "--list-projects"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        # A completion probe must not create ~/.config/ck/ or a lock.
        self.assertFalse(self.registry_file.exists())
        self.assertFalse(self.registry_file.parent.exists())

    def test_corrupt_registry_degrades_without_traceback(self):
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        self.registry_file.write_text("not json", encoding="utf-8")
        rc, out, err = _run_main(["completion", "--list-projects"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", out)
        self.assertNotIn("Traceback", err)

    def test_unreadable_registry_path_degrades(self):
        # A directory where the registry file is expected: every read
        # raises OSError, which completion must swallow.
        self.registry_file.mkdir(parents=True)
        rc, out, err = _run_main(["completion", "--list-projects"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)

    def test_lock_contention_degrades(self):
        """A fail-closed lock timeout is reported as "no names"."""
        from cklib.config import LockTimeoutError

        self.write_registry([
            _entry("alpha", "/tmp/alpha", "2026-10-01T00:00:00+00:00")])
        from unittest import mock
        with mock.patch.object(ckregistry, "file_lock",
                               side_effect=LockTimeoutError("busy")):
            rc, out, err = _run_main(["completion", "--list-projects"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)

    def test_list_project_names_never_raises(self):
        """The module-level helper (used by the scripts) is total."""
        from unittest import mock
        with mock.patch.object(ckregistry, "list_projects",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(completion.list_project_names(), [])


# ---------------------------------------------------------------------------
# Spaces sequence -> space names
# ---------------------------------------------------------------------------


class TestSpaceNameListing(IsolatedRegistry):

    def test_default_order_surfaces_project_builtins_and_custom(self):
        self.write_spaces(["work"])
        rc, out, err = _run_main(["completion", "--list-spaces"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            out.splitlines(), ["project", "local", "remote", "work"])

    def test_persisted_spaces_order_is_respected(self):
        self.write_spaces(
            ["work", "office"],
            order=["project", "office", "local", "remote", "work"],
        )
        rc, out, err = _run_main(["completion", "--list-spaces"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            out.splitlines(),
            ["project", "office", "local", "remote", "work"],
        )

    def test_missing_state_is_silent_and_unmaterialized(self):
        self.assertFalse(self.state_file.exists())
        self.assertFalse(self.spaces_dir.exists())
        rc, out, err = _run_main(["completion", "--list-spaces"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines(), ["project", "local", "remote"])
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.state_file.exists())
        self.assertFalse(self.spaces_dir.exists())

    def test_list_space_names_never_raises(self):
        from unittest import mock
        with mock.patch.object(spaces, "space_order",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(completion.list_space_names(), [])


# ---------------------------------------------------------------------------
# End-to-end shell behaviour (Bash)
# ---------------------------------------------------------------------------


class _ShellHarness(IsolatedRegistry):
    """Builds a temporary ``ck`` shim + completion script for a shell."""

    def _env(self, extra: dict | None = None) -> dict:
        bin_dir = self.home / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "ck"
        shim.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{REPO_ROOT / "ck"}" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        env = {
            key: value for key, value in os.environ.items()
            if key not in ("CK_BIN", "CK_SANDBOX", "CK_DEV",
                           "CK_SANDBOX_ACTIVE", "CK_SANDBOX_SHELL")
        }
        env.update({
            "HOME": str(self.home),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "NO_COLOR": "1",
            "CK_SANDBOX_ROOT": str(self.home / ".sandbox"),
        })
        if extra:
            env.update(extra)
        return env

    def _script(self, shell: str) -> Path:
        rc, out, _ = _run_main(["completion", shell])
        self.assertEqual(rc, 0)
        path = self.home / f"ck.{shell}"
        path.write_text(out, encoding="utf-8")
        return path


class TestBashCompletionBehaviour(_ShellHarness):

    def _complete(self, words: list, env_extra: dict | None = None) -> list:
        driver = self.home / "drive.bash"
        driver.write_text(
            'source "$1"\n'
            'shift\n'
            'COMP_WORDS=("$@")\n'
            'COMP_CWORD=$(( ${#COMP_WORDS[@]} - 1 ))\n'
            '_ck_completion\n'
            'printf "%s\\n" "${COMPREPLY[@]}"\n',
            encoding="utf-8",
        )
        script = self._script("bash")
        result = subprocess.run(
            [BASH, str(driver), str(script), *words],
            capture_output=True, text=True, env=self._env(env_extra),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout.splitlines()

    @unittest.skipUnless(BASH, "bash is not installed")
    def test_completes_commands_and_project_names(self):
        self.write_registry([
            _entry("alpha", "/tmp/alpha", "2026-10-01T00:00:00+00:00"),
            _entry("beta", "/tmp/beta", "2026-10-02T00:00:00+00:00"),
        ])
        replies = self._complete(["ck", ""])
        self.assertIn("st", replies)
        self.assertIn("list", replies)
        self.assertIn("completion", replies)
        self.assertIn("alpha", replies)
        self.assertIn("beta", replies)

    @unittest.skipUnless(BASH, "bash is not installed")
    def test_completes_project_target_prefix(self):
        self.write_registry([
            _entry("alpha", "/tmp/alpha", "2026-10-01T00:00:00+00:00"),
            _entry("beta", "/tmp/beta", "2026-10-02T00:00:00+00:00"),
        ])
        self.assertEqual(self._complete(["ck", "st", "al"]), ["alpha"])
        self.assertEqual(
            sorted(self._complete(["ck", "list", ""])), ["alpha", "beta"])

    @unittest.skipUnless(BASH, "bash is not installed")
    def test_st_projects_then_flags_after_first_positional(self):
        self.write_registry([
            _entry("alpha", "/tmp/alpha", "2026-10-01T00:00:00+00:00"),
            _entry("beta", "/tmp/beta", "2026-10-02T00:00:00+00:00"),
        ])
        # `ck st <TAB>` is project completion, exactly like `ck list`.
        self.assertEqual(
            sorted(self._complete(["ck", "st", ""])), ["alpha", "beta"])
        self.assertEqual(
            sorted(self._complete(["ck", "list", ""])), ["alpha", "beta"])

        # A project name already consumed the only positional: flags.
        after_project = self._complete(["ck", "st", "alpha", ""])
        self.assertIn("-l", after_project)
        self.assertIn("--all", after_project)
        self.assertNotIn("alpha", after_project)
        self.assertNotIn("beta", after_project)

        # A flag also consumes the positional slot: keep offering flags,
        # never duplicate project names.
        after_flag = self._complete(["ck", "st", "-l", ""])
        self.assertIn("-g", after_flag)
        self.assertNotIn("alpha", after_flag)
        self.assertNotIn("beta", after_flag)

    @unittest.skipUnless(BASH, "bash is not installed")
    def test_space_names_with_spaces_survive(self):
        self.write_registry([
            _entry("beta project", "/tmp/beta", "2026-10-02T00:00:00+00:00"),
        ])
        self.assertEqual(
            self._complete(["ck", "st", "beta"]), ["beta project"])

    @unittest.skipUnless(BASH, "bash is not installed")
    def test_space_management_completes_ordered_space_names(self):
        self.write_spaces(
            ["work", "office"],
            order=["project", "office", "local", "remote", "work"],
        )
        ordered = ["project", "office", "local", "remote", "work"]

        self.assertEqual(
            self._complete(["ck", "space", "move", ""]), ordered)
        self.assertEqual(
            self._complete(["ck", "space", "swap", ""]), ordered)
        # The virtual project cannot be renamed; built-ins remain valid
        # old names for the current command semantics.
        self.assertEqual(
            self._complete(["ck", "space", "rename", ""]),
            ["office", "local", "remote", "work"])
        # Only custom spaces can be deleted: project + built-ins are
        # protected by `ck space delete` pre-flight validation.
        self.assertEqual(
            self._complete(["ck", "space", "delete", ""]),
            ["office", "work"])

        # swap's second operand excludes the first one; prefix filtering
        # still applies.
        self.assertEqual(
            self._complete(["ck", "space", "swap", "office", ""]),
            ["project", "local", "remote", "work"])
        self.assertEqual(
            self._complete(["ck", "space", "delete", "o"]), ["office"])

    @unittest.skipUnless(BASH, "bash is not installed")
    def test_ck_bin_resolves_helper_when_path_lacks_ck(self):
        """The script calls "${CK_BIN:-ck}", not a naked PATH lookup.

        PATH is reduced to an empty directory, so completion can only
        succeed when CK_BIN points at the dev launcher.
        """
        self.write_registry([
            _entry("alpha", "/tmp/alpha", "2026-10-01T00:00:00+00:00"),
            _entry("beta", "/tmp/beta", "2026-10-02T00:00:00+00:00"),
        ])
        empty_path = self.home / "empty-path"
        empty_path.mkdir()
        self.assertEqual(
            sorted(self._complete(
                ["ck", "st", ""],
                {"CK_BIN": str(self.home / "bin" / "ck"),
                 "PATH": str(empty_path)},
            )),
            ["alpha", "beta"],
        )

    @unittest.skipUnless(BASH, "bash is not installed")
    def test_missing_registry_still_completes_commands(self):
        self.assertFalse(self.registry_file.exists())
        replies = self._complete(["ck", ""])
        self.assertIn("dashboard", replies)


# ---------------------------------------------------------------------------
# End-to-end shell behaviour (Zsh)
# ---------------------------------------------------------------------------


class TestZshCompletionBehaviour(_ShellHarness):

    def _st_arguments(self, *words) -> list:
        """Run the generated `_ck` for `words`, returning `_arguments`
        specs. The mock isolates the positional/option wiring from a
        full compsys session."""
        driver = self.home / "st-specs.zsh"
        driver.write_text(
            '_arguments() {\n'
            '    local arg\n'
            '    for arg in "$@"; do\n'
            '        print -r -- "$arg"\n'
            '    done\n'
            '}\n'
            'source "$1"\n'
            'shift\n'
            'words=("$@")\n'
            'CURRENT=${#words[@]}\n'
            '_ck\n',
            encoding="utf-8",
        )
        script = self._script("zsh")
        result = subprocess.run(
            [ZSH, str(driver), str(script), *words],
            capture_output=True, text=True, env=self._env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout.splitlines()

    @unittest.skipUnless(ZSH, "zsh is not installed")
    def test_st_projects_then_flags_after_first_positional(self):
        self.write_registry([
            _entry("alpha", "/tmp/alpha", "2026-10-01T00:00:00+00:00"),
            _entry("beta", "/tmp/beta", "2026-10-02T00:00:00+00:00"),
        ])
        project_spec = "1:registered project:_ck_projects"

        fresh = self._st_arguments("ck", "st", "")
        self.assertIn(project_spec, fresh)
        self.assertTrue(any("print the task list" in line for line in fresh))

        after_project = self._st_arguments("ck", "st", "alpha", "")
        self.assertNotIn(project_spec, after_project)
        self.assertTrue(
            any("print the task list" in line for line in after_project))

        after_flag = self._st_arguments("ck", "st", "-l", "")
        self.assertNotIn(project_spec, after_flag)
        self.assertTrue(
            any("show the global dashboard" in line for line in after_flag))

    def _space_completion(self, *words, env_extra=None) -> list:
        """Run `_ck` for a `ck space …` line with mocked compsys."""
        driver = self.home / "space-specs.zsh"
        driver.write_text(
            'compadd() {\n'
            '    local -a arr\n'
            '    while (( $# )); do\n'
            '        case $1 in\n'
            '            -a) arr=("${(@P)2}"); shift 2 ;;\n'
            '            *) shift ;;\n'
            '        esac\n'
            '    done\n'
            '    print -l -- "${arr[@]}"\n'
            '}\n'
            '_wanted() { shift 3; "$@" }\n'
            '_describe() { :; }\n'
            'source "$1"\n'
            'shift\n'
            'words=("$@")\n'
            'CURRENT=${#words[@]}\n'
            '_ck\n',
            encoding="utf-8",
        )
        script = self._script("zsh")
        result = subprocess.run(
            [ZSH, str(driver), str(script), *words],
            capture_output=True, text=True, env=self._env(env_extra),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout.splitlines()

    @unittest.skipUnless(ZSH, "zsh is not installed")
    def test_ck_bin_resolves_helper_when_path_lacks_ck(self):
        self.write_spaces(
            ["work", "office"],
            order=["project", "office", "local", "remote", "work"],
        )
        empty_path = self.home / "empty-path"
        empty_path.mkdir()
        self.assertEqual(
            self._space_completion(
                "ck", "space", "move", "",
                env_extra={"CK_BIN": str(self.home / "bin" / "ck"),
                           "PATH": str(empty_path)},
            ),
            ["project", "office", "local", "remote", "work"],
        )

    @unittest.skipUnless(ZSH, "zsh is not installed")
    def test_space_management_completes_ordered_space_names(self):
        self.write_spaces(
            ["work", "office"],
            order=["project", "office", "local", "remote", "work"],
        )
        ordered = ["project", "office", "local", "remote", "work"]

        self.assertEqual(
            self._space_completion("ck", "space", "move", ""), ordered)
        self.assertEqual(
            self._space_completion("ck", "space", "swap", ""), ordered)
        self.assertEqual(
            self._space_completion("ck", "space", "rename", ""),
            ["office", "local", "remote", "work"])
        self.assertEqual(
            self._space_completion("ck", "space", "delete", ""),
            ["office", "work"])
        self.assertEqual(
            self._space_completion("ck", "space", "swap", "office", ""),
            ["project", "local", "remote", "work"])

    @unittest.skipUnless(ZSH, "zsh is not installed")
    def test_projects_are_parsed_from_registry_mock(self):
        self.write_registry([
            _entry("alpha", "/tmp/alpha", "2026-10-01T00:00:00+00:00"),
            _entry("beta project", "/tmp/beta", "2026-10-02T00:00:00+00:00"),
        ])
        driver = self.home / "drive.zsh"
        # Minimal compsys stand-ins: route `_wanted ... compadd -a arr`
        # to the real array and print its elements.
        driver.write_text(
            'compadd() {\n'
            '    local -a arr\n'
            '    while (( $# )); do\n'
            '        case $1 in\n'
            '            -a) arr=("${(@P)2}"); shift 2 ;;\n'
            '            *) shift ;;\n'
            '        esac\n'
            '    done\n'
            '    print -l -- "${arr[@]}"\n'
            '}\n'
            '_wanted() { shift 3; "$@" }\n'
            'source "$1"\n'
            '_ck_projects\n',
            encoding="utf-8",
        )
        script = self._script("zsh")
        result = subprocess.run(
            [ZSH, str(driver), str(script)],
            capture_output=True, text=True, env=self._env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(), ["beta project", "alpha"])
        self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(ZSH, "zsh is not installed")
    def test_missing_registry_is_quiet(self):
        self.assertFalse(self.registry_file.exists())
        driver = self.home / "quiet.zsh"
        driver.write_text(
            '_wanted() { shift 3; "$@" }\n'
            'compadd() { print -l -- "called" }\n'
            'source "$1"\n'
            '_ck_projects\n',
            encoding="utf-8",
        )
        script = self._script("zsh")
        result = subprocess.run(
            [ZSH, str(driver), str(script)],
            capture_output=True, text=True, env=self._env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
