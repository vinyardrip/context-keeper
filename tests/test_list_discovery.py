"""Tests for ``ck list`` project discovery and explicit targeting.

``ck list`` used to resolve its plan through the same hierarchy as
``ck st``: LOCAL -> UPWARD -> GLOBAL. The GLOBAL tier silently picked
the most-recently-active REGISTERED project, so running ``ck list``
in a folder that owns no project printed an arbitrary project's tasks
with no indication of where they came from — a silent, plausible-
looking wrong answer.

The behaviour pinned here:

- ``ck list <project_name>`` targets a REGISTERED project from any
  working directory;
- an unknown name is an explicit error (exit 1), never an empty
  listing;
- with no argument inside a project, the historical listing stands;
- with no argument OUTSIDE every project, the command either names
  the single nested project it found or lists the choices — it never
  picks one silently.

Isolation: global config (registry + spaces) is pinned to a per-test
tmp HOME and the dev-mode toggles are forced OFF, so these tests are
byte-stable under pytest and plain ``python -m unittest``.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cklib.cli import main
from cklib.core import ContextKeeper


class _ListHarness(unittest.TestCase):
    """Isolated HOME + project tree with a `run` helper."""

    _ENV_KEYS = ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                 "CK_SANDBOX_SHELL", "CK_SANDBOX_ROOT")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()

        saved = {k: os.environ.get(k) for k in self._ENV_KEYS}
        os.environ["HOME"] = str(self.home)
        os.environ["CK_SANDBOX_ROOT"] = str(self.root / ".sandbox")
        for key in ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                    "CK_SANDBOX_SHELL"):
            os.environ.pop(key, None)

        def _restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(_restore)

    # -- helpers --------------------------------------------------------- #

    def run_cli(self, argv, cwd: Path) -> tuple[int, str]:
        """Invoke ``ck <argv>`` with ``cwd`` as the working directory."""
        buf = io.StringIO()
        with mock.patch("pathlib.Path.cwd", return_value=cwd), \
                redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()

    def make_project(self, name: str, tasks: list[str], *,
                     register: bool = True,
                     parent: Path | None = None) -> Path:
        """Create a project with real tasks, optionally registered.

        ``parent`` places the project in a sub-directory (used by the
        nested-discovery tests); it defaults to the harness root.
        ``done`` indexes mark tasks completed.
        """
        base = parent if parent is not None else self.root
        path = base / name
        (path / ".ck").mkdir(parents=True)
        body = ["# " + name, "", "## Current Sprint"]
        body += [f"- [ ] {t}" for t in tasks]
        (path / ".ck" / "PLAN.md").write_text("\n".join(body) + "\n",
                                              encoding="utf-8")
        if register:
            from cklib import registry
            registry.register_project(path, name=name)
        return path


class TestListInsideProject(_ListHarness):
    """The in-project path keeps its historical output exactly."""

    def test_bare_list_inside_project_is_unchanged(self):
        project = self.make_project("alpha", ["first task", "second task"])
        code, out = self.run_cli(["list"], project)

        self.assertEqual(code, 0, out)
        self.assertIn("## Current Sprint", out)
        self.assertIn("[ ] 1. first task", out)
        self.assertIn("[ ] 2. second task", out)

    def test_inside_project_no_project_label_is_added(self):
        """The ``(name)`` suffix belongs to DISCOVERY only — inside a
        project the user already knows where they are."""
        project = self.make_project("alpha", ["only task"])
        code, out = self.run_cli(["list"], project)

        self.assertEqual(code, 0, out)
        self.assertIn("## Current Sprint\n", out)
        self.assertNotIn("(alpha)", out)

    def test_empty_project_still_reports_no_tasks(self):
        path = self.root / "empty"
        (path / ".ck").mkdir(parents=True)
        (path / ".ck" / "PLAN.md").write_text("", encoding="utf-8")
        code, out = self.run_cli(["list"], path)

        self.assertEqual(code, 0, out)
        self.assertIn("No tasks", out)


class TestListOutsideProject(_ListHarness):
    """No silent arbitrary fallback."""

    def test_multiple_nested_projects_are_listed_not_guessed(self):
        self.make_project("alpha", ["alpha task"])
        self.make_project("test_isolation_sub", ["isolation task"])
        parent = self.root

        code, out = self.run_cli(["list"], parent)

        self.assertEqual(code, 0, out)
        self.assertIn("[!] Current directory is not a context-keeper "
                      "project.", out)
        self.assertIn("Available nested projects:", out)
        self.assertIn("  - alpha", out)
        self.assertIn("  - test_isolation_sub", out)
        self.assertIn("ck list <project_name>", out)
        # Crucially: neither project's tasks were dumped.
        self.assertNotIn("alpha task", out)
        self.assertNotIn("isolation task", out)

    def test_zero_nested_projects_still_reports_the_notice(self):
        (self.root / "plain").mkdir()
        code, out = self.run_cli(["list"], self.root / "plain")

        self.assertEqual(code, 0, out)
        self.assertIn("not a context-keeper project", out)
        self.assertIn("(none registered here)", out)
        self.assertIn("ck list <project_name>", out)

    def test_exactly_one_nested_project_is_listed_with_its_label(self):
        solo = self.root / "solo"
        solo.mkdir()
        self.make_project("lonely", ["solo task", "second solo task"],
                          parent=solo)

        code, out = self.run_cli(["list"], solo)

        self.assertEqual(code, 0, out)
        # The header names the project explicitly.
        self.assertIn("## Current Sprint (lonely)", out)
        self.assertIn("[ ] 1. solo task", out)
        self.assertIn("[ ] 2. second solo task", out)
        self.assertNotIn("Available nested projects", out)

    def test_project_label_appears_on_the_first_header_only(self):
        solo = self.root / "solo"
        solo.mkdir()
        project = solo / "lonely"
        (project / ".ck").mkdir(parents=True)
        (project / ".ck" / "PLAN.md").write_text(
            "# lonely\n\n## Current Sprint\n- [ ] open task\n\n"
            "## Completed\n- [x] finished task\n",
            encoding="utf-8")
        from cklib import registry
        registry.register_project(project, name="lonely")

        code, out = self.run_cli(["list"], solo)

        self.assertEqual(code, 0, out)
        self.assertIn("## Current Sprint (lonely)", out)
        self.assertIn("## Completed\n", out)
        self.assertEqual(out.count("(lonely)"), 1, out)

    def test_unregistered_nested_project_is_discovered_by_scan(self):
        """An unregistered folder that physically carries a PLAN.md is
        still OFFERED: the filesystem scan is the fallback that makes
        `ck list` useful in an empty-registry context."""
        self.make_project("alpha", ["alpha task"], register=False)
        code, out = self.run_cli(["list"], self.root)

        self.assertEqual(code, 0, out)
        # Exactly one nested project -> listed directly with its name.
        self.assertIn("## Current Sprint (alpha)", out)
        self.assertIn("[ ] 1. alpha task", out)

    def test_unregistered_name_is_targetable(self):
        """Every name the scan offers must be addressable."""
        self.make_project("alpha", ["alpha task"], register=False)
        code, out = self.run_cli(["list", "alpha"], self.root)

        self.assertEqual(code, 0, out)
        self.assertIn("[ ] 1. alpha task", out)

    def test_name_outside_the_scan_tree_is_still_not_found(self):
        """The scan fallback does NOT widen matching to the whole disk:
        an unregistered project is addressable only when it actually
        nests under the invocation directory."""
        self.make_project("far_away", ["far task"], register=False)
        # A sibling directory that does NOT contain far_away.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()

        code, out = self.run_cli(["list", "far_away"], elsewhere)

        self.assertEqual(code, 1)
        self.assertIn("ERROR: Project 'far_away' not found.", out)

    def test_registered_entry_does_not_mask_unregistered_siblings(self):
        """The registry and the FILESYSTEM scan are MERGED, not
        sequential tiers.

        One registered folder among several physical projects used to
        look like "exactly one nested project" (the scan only ran
        when the registry knew nothing), so the registered one was
        auto-listed while its unregistered siblings stayed invisible
        — a silent pick with a plausible-looking alibi.
        """
        self.make_project("zeta_registered", ["registered task"])
        self.make_project("alpha_unregistered", ["unregistered task"],
                          register=False)

        code, out = self.run_cli(["list"], self.root)

        self.assertEqual(code, 0, out)
        self.assertIn("Available nested projects:", out)
        # BOTH tiers are offered, sorted by name.
        self.assertIn("  - alpha_unregistered", out)
        self.assertIn("  - zeta_registered", out)
        self.assertLess(out.index("  - alpha_unregistered"),
                        out.index("  - zeta_registered"))
        # Neither project's tasks were dumped.
        self.assertNotIn("registered task", out)
        self.assertNotIn("unregistered task", out)

    def test_every_mixed_tier_name_stays_addressable(self):
        """Each name the merged notice prints resolves — registry
        name, folder name and scan-only folder alike."""
        self.make_project("zeta_registered", ["registered task"])
        self.make_project("alpha_unregistered", ["unregistered task"],
                          register=False)

        for name, task in (("zeta_registered", "registered task"),
                           ("alpha_unregistered", "unregistered task")):
            with self.subTest(name=name):
                code, out = self.run_cli(["list", name], self.root)
                self.assertEqual(code, 0, out)
                self.assertIn(task, out)


class TestListExplicitProject(_ListHarness):
    """``ck list <project_name>`` — explicit targeting."""

    def test_explicit_name_lists_target_project_from_parent(self):
        self.make_project("alpha", ["alpha task"])
        self.make_project("beta", ["beta task"])

        code, out = self.run_cli(["list", "alpha"], self.root)

        self.assertEqual(code, 0, out)
        self.assertIn("[ ] 1. alpha task", out)
        self.assertNotIn("beta task", out)

    def test_explicit_name_works_from_an_unrelated_directory(self):
        self.make_project("alpha", ["alpha task"])
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()

        code, out = self.run_cli(["list", "alpha"], elsewhere)

        self.assertEqual(code, 0, out)
        self.assertIn("[ ] 1. alpha task", out)

    def test_explicit_name_wins_over_the_current_project(self):
        """Targeting is explicit: it beats the directory the user
        happens to be standing in."""
        self.make_project("alpha", ["alpha task"])
        beta = self.make_project("beta", ["beta task"])

        code, out = self.run_cli(["list", "alpha"], beta)

        self.assertEqual(code, 0, out)
        self.assertIn("[ ] 1. alpha task", out)
        self.assertNotIn("beta task", out)

    def test_folder_name_is_accepted_when_registry_name_differs(self):
        """``register --name`` may rename an entry; users type the
        folder name, so that must still resolve."""
        project = self.root / "typed_name"
        (project / ".ck").mkdir(parents=True)
        (project / ".ck" / "PLAN.md").write_text(
            "# p\n\n## Current Sprint\n- [ ] task by folder\n",
            encoding="utf-8")
        from cklib import registry
        registry.register_project(project, name="pretty_name")

        code, out = self.run_cli(["list", "typed_name"], self.root)

        self.assertEqual(code, 0, out)
        self.assertIn("[ ] 1. task by folder", out)

    def test_unknown_project_exits_1_with_error(self):
        self.make_project("alpha", ["alpha task"])
        code, out = self.run_cli(["list", "nope"], self.root)

        self.assertEqual(code, 1)
        self.assertIn("ERROR: Project 'nope' not found.", out)
        # An error is not a listing.
        self.assertNotIn("alpha task", out)

    def test_unknown_project_exits_1_even_inside_a_project(self):
        project = self.make_project("alpha", ["alpha task"])
        code, out = self.run_cli(["list", "nope"], project)

        self.assertEqual(code, 1)
        self.assertIn("ERROR: Project 'nope' not found.", out)
        self.assertNotIn("alpha task", out)

    def test_unknown_name_not_present_anywhere_is_not_found(self):
        """A name matching neither the registry nor the scanned tree is
        genuinely unknown."""
        self.make_project("alpha", ["alpha task"])
        code, out = self.run_cli(["list", "ghost"], self.root)

        self.assertEqual(code, 1)
        self.assertIn("ERROR: Project 'ghost' not found.", out)


class TestListCoreHelpers(_ListHarness):
    """Unit-level coverage of the resolution helpers."""

    def test_registered_entries_under_is_name_sorted(self):
        for name in ("zeta", "alpha", "mid"):
            self.make_project(name, ["t"])
        found = [e.name for e in
                 __import__("cklib.core", fromlist=["x"])
                 .registered_entries_under(self.root)]
        self.assertEqual(found, ["alpha", "mid", "zeta"])

    def test_registered_entries_under_ignores_ancestors(self):
        """A project ABOVE the directory is not "nested" below it."""
        outer = self.make_project("outer", ["t"])
        inner = outer / "inner"
        inner.mkdir()
        from cklib.core import registered_entries_under
        self.assertEqual(registered_entries_under(inner), [])

    def test_registered_project_by_name_returns_root(self):
        project = self.make_project("alpha", ["t"])
        from cklib.core import registered_project_by_name
        self.assertEqual(
            Path(registered_project_by_name("alpha")).resolve(),
            project.resolve())

    def test_registered_project_by_name_unknown_is_none(self):
        self.make_project("alpha", ["t"])
        from cklib.core import registered_project_by_name
        self.assertIsNone(registered_project_by_name("ghost"))

    def test_no_registry_degrades_to_empty(self):
        """No registry on disk must degrade, never raise."""
        from cklib.core import (registered_entries_under,
                                registered_project_by_name)
        self.assertEqual(registered_entries_under(self.root), [])
        self.assertIsNone(registered_project_by_name("alpha"))

    def test_nested_projects_uses_invocation_dir_not_walked_root(self):
        """Discovery anchors on the invocation directory, so a parent's
        project can never mask what the user actually asked about."""
        parent = self.make_project("alpha", ["t"])
        sub = parent / "sub"
        sub.mkdir()
        keeper = ContextKeeper()
        with mock.patch("pathlib.Path.cwd", return_value=sub):
            self.assertEqual(keeper.nested_projects(), [])


if __name__ == "__main__":
    unittest.main()