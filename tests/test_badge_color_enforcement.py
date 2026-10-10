"""End-to-end proof that every status badge is color-styled.

Unit tests can call ``ui.notice`` directly and prove the palette is
right, but they cannot prove the *commands* actually route their
notices through it. These tests run the real CLI and inspect the RAW
stdout bytes, asserting that:

- every emitted status badge carries its exact ANSI sequence;
- the sequence is present **even though stdout is a plain pipe /
  StringIO** (no TTY) — the regression that "it looks fine in my
  terminal" would hide;
- ``NO_COLOR`` still produces plain text, and that is the only way to
  get it.

``strip_ansi`` is used ONLY to prove that stripping yields the exact
plain badge text — never to make a failing assertion pass.
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
from cklib import ui
from cklib.cli import main

# The exact sequences the spec requires.
STYLED = {
    "[!]": "\033[1;33m[!]\033[0m",
    "[i]": "\033[36m[i]\033[0m",
    "[ok]": "\033[1;32m[ok]\033[0m",
    "[err]": "\033[1;31m[err]\033[0m",
}


class _ColorHarness(unittest.TestCase):
    """Isolated HOME + a real project, with colours FORCED ON."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "proj"
        (self.project / ".ck").mkdir(parents=True)
        (self.project / ".ck" / "PLAN.md").write_text(
            "# p\n\n## Current Sprint\n- [ ] alpha\n- [ ] beta\n",
            encoding="utf-8")

        saved = {k: os.environ.get(k) for k in
                 ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE",
                  "CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                  "CK_SANDBOX_SHELL", "CK_SANDBOX_ROOT")}
        os.environ["HOME"] = str(self.home)
        os.environ["CK_SANDBOX_ROOT"] = str(self.root / ".sandbox")
        for key in ("CK_SANDBOX", "CK_DEV", "CK_SANDBOX_ACTIVE",
                    "CK_SANDBOX_SHELL"):
            os.environ.pop(key, None)
        # Colors ON: explicitly clear every opt-out.
        for key in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE"):
            os.environ.pop(key, None)

        # GLOBAL REGISTRY ISOLATION: the registry paths are bound at
        # IMPORT time (``from .config import GLOBAL_REGISTRY_FILE``),
        # so reassigning HOME — or patching ``ckconfig`` alone — is NOT
        # enough. Without patching the CONSUMING modules the dashboard
        # would read the developer's real registry.
        from cklib import registry as ckregistry

        fake_cfg = self.home / ".config" / "ck"
        patcher = mock.patch.multiple(
            ckregistry,
            GLOBAL_CONFIG_DIR=fake_cfg,
            GLOBAL_REGISTRY_FILE=fake_cfg / "projects.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        cfg_patcher = mock.patch.multiple(
            ckconfig,
            GLOBAL_CONFIG_DIR=fake_cfg,
            GLOBAL_REGISTRY_FILE=fake_cfg / "projects.json",
            LEGACY_GLOBAL_CONFIG_FILE=self.home / ".ckrc",
        )
        cfg_patcher.start()
        self.addCleanup(cfg_patcher.stop)

        def _restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(_restore)

    def run_cli(self, argv) -> tuple[int, str]:
        """Run the CLI capturing RAW stdout (escape sequences intact).

        stdout is a StringIO — definitively NOT a TTY — which is the
        whole point: badges must still be colored.
        """
        buf = io.StringIO()
        with mock.patch("pathlib.Path.cwd", return_value=self.project), \
                redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()


class TestBadgesAreColoredThroughAPipe(_ColorHarness):
    """The core guarantee."""

    def _assert_badges_styled(self, out: str, expected: list[str]) -> None:
        plain = ui.strip_ansi(out)
        for token in expected:
            with self.subTest(token=token):
                self.assertIn(STYLED[token], out,
                              f"{token} is NOT styled in a pipe:\n{out!r}")
                # Stripping must yield exactly the documented token.
                self.assertIn(token, plain)

    def test_ok_badge_on_add_is_bold_green(self):
        code, out = self.run_cli(["add", "new task"])
        self.assertEqual(code, 0, out)
        self._assert_badges_styled(out, ["[ok]"])

    def test_warn_badge_in_status_is_bold_yellow(self):
        """``ck st`` on an unfocused plan emits a bold-yellow ``[!]``."""
        code, out = self.run_cli(["st"])
        self.assertEqual(code, 0, out)
        self.assertIn(STYLED["[!]"], out, out)

    def test_err_badge_on_unknown_task_is_bold_red(self):
        code, out = self.run_cli(["swap", "1", "9"])
        self.assertEqual(code, 2, out)
        # Unknown id is a plain ERROR line -> still bold red.
        self.assertIn(ui.BOLD_RED, out, out)

    def test_dashboard_hint_is_cyan(self):
        """THE requirement: the ``[i]`` hint under the dashboard."""
        from cklib import registry as ckregistry

        code, out = self.run_cli(["dashboard"])
        self.assertEqual(code, 0, out)
        self.assertIn(STYLED["[i]"], out, out)

        # With a project registered the dashboard gains the footer tip.
        ckregistry.register_project(self.project, name="alpha")
        code, out = self.run_cli(["dashboard"])
        self.assertEqual(code, 0, out)
        self.assertIn(STYLED["[i]"] + " Missing a project?", out, out)

    def test_warn_badge_is_bold_yellow(self):
        """A ``[!]`` warning renders bold yellow, not plain yellow."""
        code, out = self.run_cli(["st"])
        self.assertEqual(code, 0, out)
        self.assertIn(STYLED["[!]"], out, out)

    def test_space_list_hint_is_cyan(self):
        code, out = self.run_cli(["space", "list"])
        self.assertEqual(code, 0, out)
        self.assertIn(STYLED["[i]"], out, out)

    def test_reorder_badge_is_bold_red_on_refusal(self):
        code, out = self.run_cli(["done", "1"])
        self.assertEqual(code, 0, out)
        code, out = self.run_cli(["move", "1", "1"])
        self.assertEqual(code, 1, out)
        self.assertIn(STYLED["[err]"] + " Cannot move completed task #1.",
                      out, out)

    def test_no_badge_is_ever_plaintext_in_a_pipe(self):
        """Sweep several commands: no status badge may appear UNSTYLED."""
        commands = (["add", "t"], ["st"], ["dashboard"], ["list"],
                    ["done", "1"], ["move", "2", "1"], ["notes"],
                    ["info"], ["space", "list"])
        for argv in commands:
            _code, out = self.run_cli(argv)
            plain = ui.strip_ansi(out)
            for token in ("[!]", "[i]", "[ok]", "[err]"):
                # Every occurrence in the PLAIN text must correspond to
                # a styled occurrence in the raw output.
                count_plain = plain.count(token)
                if count_plain == 0:
                    continue
                with self.subTest(argv=argv, token=token):
                    self.assertGreaterEqual(
                        out.count(STYLED[token]), count_plain,
                        f"{argv}: {count_plain} plain {token!r} in "
                        f"{plain!r}")


class TestNoColorIsTheOnlyOptOut(_ColorHarness):
    def test_no_color_removes_every_sequence(self):
        os.environ["NO_COLOR"] = "1"
        _code, out = self.run_cli(["add", "plain task"])
        self.assertNotIn("\033[", out)
        self.assertIn("[ok] Added task", out)

    def test_force_color_zero_does_not_disable(self):
        os.environ["FORCE_COLOR"] = "0"
        _code, out = self.run_cli(["add", "still colored"])
        self.assertIn(STYLED["[ok]"], out, out)

    def test_clicolor_force_zero_does_not_disable(self):
        os.environ["CLICOLOR_FORCE"] = "0"
        _code, out = self.run_cli(["add", "still colored"])
        self.assertIn(STYLED["[ok]"], out, out)

    def test_empty_no_color_is_not_an_opt_out(self):
        os.environ["NO_COLOR"] = ""
        _code, out = self.run_cli(["add", "still colored"])
        self.assertIn(STYLED["[ok]"], out, out)


class TestNoPlaintextBadgeSources(_ColorHarness):
    """Static audit: no module emits a bare badge literal."""

    def test_no_bare_badge_prints_remain(self):
        """Every ``print``/``printer`` of a badge goes through notice()."""
        core = Path(__file__).resolve().parent.parent / "cklib"
        offenders: list[str] = []
        for module in sorted(core.glob("*.py")):
            for number, line in enumerate(
                    module.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if not (stripped.startswith("print(")
                        or stripped.startswith("printer(")):
                    continue
                if "notice(" in stripped:
                    continue
                if any(f'"{t}' in stripped or f"f\"{t}" in stripped
                       for t in ("[ok]", "[!]", "[i]", "[err]")):
                    offenders.append(f"{module.name}:{number}: {stripped}")
        self.assertEqual(offenders, [],
                         "bare badge prints bypass notice():\n"
                         + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()