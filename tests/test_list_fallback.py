"""No-context ``ck list`` fallback lists registered projects (v0.14.0).

Outside every project with no nested discovery matches, the notice
used to dead-end on "(none registered here)" even when OTHER projects
were registered globally. Now they render under "Registered
projects:" — every name printed is addressable via
``ck list <project_name>`` from any directory.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tests  # noqa: F401  (filesystem isolation safety net)

from cklib import config as ckconfig
from cklib import registry
from cklib.cli import main


def _run_main(argv, root=None):
    cwd = os.getcwd()
    if root is not None:
        os.chdir(root)
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = main(argv)
    finally:
        os.chdir(cwd)
    return code, buf_out.getvalue(), buf_err.getvalue()


class TestListFallbackRegistryNames(unittest.TestCase):
    def setUp(self):
        self.old_config = ckconfig.GLOBAL_CONFIG_DIR
        self.root = tempfile.mkdtemp(prefix="ck_list_fallback_")
        self.old_cwd = os.getcwd()
        # A dangling cwd OUTSIDE every project: the bare temp dir has
        # no .ck/, no parent plan, and no nested projects.
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.old_cwd)
        ckconfig.GLOBAL_CONFIG_DIR = self.old_config

    def test_nothing_registered_dead_end_is_unchanged(self):
        code, out, _err = _run_main(["list"])
        self.assertEqual(code, 0)
        self.assertIn("(none registered here)", out)
        self.assertNotIn("Registered projects:", out)

    def test_registered_projects_render_as_menu(self):
        elsewhere = Path(tempfile.mkdtemp(prefix="ck_registered_proj_"))
        (elsewhere / ".ck").mkdir()
        (elsewhere / ".ck" / "PLAN.md").write_text(
            "## Current Sprint\n- [] one\n", encoding="utf-8")
        registry.register_project(elsewhere, name="alpha")

        code, out, _err = _run_main(["list"])

        self.assertEqual(code, 0)
        self.assertIn("(none registered here)", out)
        self.assertIn("Registered projects:", out)
        self.assertIn("  - alpha", out)
        self.assertIn("ck list <project_name>", out)

    def test_capped_list_points_at_dashboard(self):
        for i in range(11):
            elsewhere = Path(
                tempfile.mkdtemp(prefix=f"ck_capped_{i:02d}_"))
            (elsewhere / ".ck").mkdir()
            (elsewhere / ".ck" / "PLAN.md").write_text(
                "## Current Sprint\n- [] one\n", encoding="utf-8")
            registry.register_project(elsewhere, name=f"proj{i:02d}")

        code, out, _err = _run_main(["list"])

        self.assertEqual(code, 0)
        self.assertIn("ck dashboard", out)


if __name__ == "__main__":
    unittest.main()
