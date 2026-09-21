"""Tests for the centralized ANSI color system (cklib/ui.py).

Covers:

- ``color_enabled`` precedence: ``NO_COLOR`` (unoverrideable opt-out)
  > ``FORCE_COLOR`` / ``CLICOLOR_FORCE`` (force-on when piped) >
  TTY detection (``isatty``).
- :class:`Palette` code generation and the identity transform of a
  disabled palette (plain-text fallback).
- ``strip_ansi`` round-trip.
- ``ck st`` rendering: colored and plain output are
  content-identical after stripping escapes.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cklib import ui
from cklib.config import read_color_config
from cklib.core import ContextKeeper, _render_local_status
from cklib.parser import parse_plan


class _FakeTty(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def _env(**overrides) -> mock._patch_dict:
    """Patch os.environ to EXACTLY the given variables."""
    return mock.patch.dict(os.environ, overrides, clear=True)


# ---------------------------------------------------------------------------
# color_enabled: precedence rules
# ---------------------------------------------------------------------------


class TestColorEnabled(unittest.TestCase):
    def test_no_color_disables_even_when_forced(self):
        """NO_COLOR is the highest-precedence opt-out: nothing
        (not even FORCE_COLOR or a TTY) can re-enable colors."""
        with _env(NO_COLOR="1", FORCE_COLOR="1", CLICOLOR_FORCE="1"):
            self.assertFalse(ui.color_enabled(_FakeTty()))

    def test_no_color_empty_value_does_not_disable(self):
        """no-color.org: NO_COLOR must be present AND non-empty."""
        with _env(NO_COLOR=""):
            self.assertTrue(ui.color_enabled(_FakeTty()))

    def test_force_color_enables_when_piped(self):
        with _env(FORCE_COLOR="1"):
            self.assertTrue(ui.color_enabled(io.StringIO()))

    def test_force_color_zero_does_not_force(self):
        with _env(FORCE_COLOR="0"):
            self.assertFalse(ui.color_enabled(io.StringIO()))

    def test_clicolor_force_truthy_enables_when_piped(self):
        for value in ("1", "true", "YES", "on"):
            with _env(CLICOLOR_FORCE=value):
                self.assertTrue(
                    ui.color_enabled(io.StringIO()), f"CLICOLOR_FORCE={value}"
                )

    def test_clicolor_force_zero_falls_back_to_tty(self):
        with _env(CLICOLOR_FORCE="0"):
            self.assertFalse(ui.color_enabled(io.StringIO()))
            self.assertTrue(ui.color_enabled(_FakeTty()))

    def test_tty_enables_by_default(self):
        with _env():
            self.assertTrue(ui.color_enabled(_FakeTty()))

    def test_pipe_disables_by_default(self):
        with _env():
            self.assertFalse(ui.color_enabled(io.StringIO()))

    def test_no_isattr_stream_is_safe(self):
        """A stream without isatty (None-like) must not raise."""
        with _env():
            self.assertFalse(ui.color_enabled(object()))

    def test_default_stream_is_stdout(self):
        """Without an explicit stream, sys.stdout is probed."""
        with _env(), mock.patch("sys.stdout", _FakeTty()):
            self.assertTrue(ui.color_enabled())
        with _env(), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(ui.color_enabled())


# ---------------------------------------------------------------------------
# Palette: code generation & plain fallback
# ---------------------------------------------------------------------------


class TestPalette(unittest.TestCase):
    def test_disabled_palette_is_identity(self):
        p = ui.Palette(False)
        for fn in (p.bold, p.green, p.yellow, p.cyan, p.red, p.muted,
                   p.text, p.border, p.accent,
                   p.bold_yellow, p.bold_cyan, p.bold_green, p.bold_red):
            self.assertEqual(fn("text"), "text")

    def test_enabled_palette_generates_codes(self):
        p = ui.Palette(True)
        self.assertEqual(p.green("x"), f"{ui.GREEN}x{ui.RESET}")
        self.assertEqual(p.bold("x"), f"{ui.BOLD}x{ui.RESET}")
        self.assertEqual(p.yellow("x"), f"{ui.YELLOW}x{ui.RESET}")
        self.assertEqual(p.cyan("x"), f"{ui.CYAN}x{ui.RESET}")
        self.assertEqual(p.red("x"), f"{ui.RED}x{ui.RESET}")

    def test_enabled_palette_combines_codes(self):
        p = ui.Palette(True)
        self.assertEqual(
            p.bold_yellow("x"), f"{ui.BOLD}{ui.YELLOW}x{ui.RESET}"
        )
        self.assertEqual(
            p.bold_cyan("x"), f"{ui.BOLD}{ui.CYAN}x{ui.RESET}"
        )

    def test_slots_default_to_native_inheritance(self):
        """Without config, secondary text/borders/accents emit NO
        SGR sequence — the terminal's native (theme-tuned) text
        color shows through. No DIM, no hardcoded dark gray."""
        p = ui.Palette(True)
        self.assertEqual(p.text("x"), "x")
        self.assertEqual(p.muted("x"), "x")
        self.assertEqual(p.border("x"), "x")
        self.assertEqual(p.accent("x"), "x")

    def test_paint_without_codes_is_noop(self):
        self.assertEqual(ui.Palette(True).paint("x"), "x")

    def test_paint_empty_text_is_noop(self):
        self.assertEqual(ui.Palette(True).paint("", ui.BOLD), "")

    def test_get_palette_respects_stream(self):
        with _env():
            self.assertFalse(ui.get_palette(io.StringIO()).enabled)
            self.assertTrue(ui.get_palette(_FakeTty()).enabled)

    def test_strip_ansi_removes_all_sgr_sequences(self):
        p = ui.Palette(True)
        colored = (
            f"{p.bold('a')} {p.green('b')} {p.bold_cyan('c')} "
            "\033[1;32mcombined\033[0m plain"
        )
        self.assertEqual(ui.strip_ansi(colored), "a b c combined plain")


# ---------------------------------------------------------------------------
# Renderer integration: ck st coloring
# ---------------------------------------------------------------------------


class TestStatusRenderingColors(unittest.TestCase):
    _PLAN = "# P\n- [x] prev task\n- [>] focus task\n- [ ] next task\n"

    def _render(self, palette, plan=None):
        plan = plan if plan is not None else self._PLAN
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.ck_path.mkdir(parents=True, exist_ok=True)
            ck.plan_file.write_text(plan, encoding="utf-8")
            tl = parse_plan(ck.plan_file.read_text(encoding="utf-8"))
            return _render_local_status(ck, tl, palette=palette)

    def test_plain_output_contains_no_escapes(self):
        out = self._render(ui.Palette(False))
        self.assertNotIn("\033[", out)
        self.assertIn("[>] Focus:", out)
        self.assertIn("- [2] focus task", out)

    def test_colored_output_uses_semantic_colors(self):
        out = self._render(ui.Palette(True))
        self.assertIn("\033[", out)
        # Completed (done) section and its items are green.
        self.assertIn(f"{ui.GREEN}    << Done:{ui.RESET}", out)
        self.assertIn(
            f"{ui.GREEN}       - [1] prev task [x]{ui.RESET}", out)
        # Active focus item is bold yellow.
        self.assertIn(
            f"{ui.BOLD}{ui.YELLOW}[2] focus task{ui.RESET}", out
        )
        # Upcoming item inherits the terminal's NATIVE color
        # (previously low-contrast bright-black gray).
        self.assertIn("       - [3] next task [ ]", out)
        self.assertNotIn(
            f"\033[90m       - [3] next task [ ]{ui.RESET}", out)
        # Skipped (empty here) hint is native too.
        self.assertIn("    [!] Skipped: (none)", out)
        # Section header is bold.
        self.assertIn(f"{ui.BOLD} -> WORK CONTEXT:{ui.RESET}", out)

    def test_colored_skipped_line_is_yellow(self):
        plan = "# P\n- [x] done\n- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n"
        out = self._render(ui.Palette(True), plan=plan)
        # Skipped tasks render by name, capped, in standard yellow.
        self.assertIn(
            f"{ui.YELLOW}       - [4] c [ ]{ui.RESET}",
            out,
        )
        self.assertIn(
            f"{ui.YELLOW}       - [5] d [ ]{ui.RESET}",
            out,
        )

    def test_colored_output_has_no_low_contrast_tokens(self):
        """Zero DIM (2) and zero black/bright-black (30/90) codes —
        readable on dark and transparent themes without selection."""
        out = self._render(ui.Palette(True))
        for token in ("\033[2m", "\033[30m", "\033[90m"):
            self.assertNotIn(token, out)

    def test_borders_and_accents_inherit_native_color(self):
        out = self._render(ui.Palette(True))
        lines = out.splitlines()
        # Structural bars: unpainted even with colors ON.
        self.assertEqual(lines[0], "=" * 61)
        self.assertEqual(lines[-1], "=" * 61)
        # Version tag + header: nothing styled after the project
        # name — the line ends with the plain (accent-native) tag.
        from cklib.config import VERSION
        header = [l for l in lines if "project" in l][0]
        self.assertTrue(header.endswith(f"[v{VERSION}]"))

    def test_global_fallback_hint_is_accent_native(self):
        """The ``<- path`` annotation also inherits native color."""
        from cklib.config import VERSION
        from cklib.parser import parse_plan as _pp
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            ck = ContextKeeper(root=root)
            ck.ck_path.mkdir(parents=True, exist_ok=True)
            ck.plan_file.write_text(self._PLAN, encoding="utf-8")
            out = _render_local_status(
                ck, _pp(self._PLAN), palette=ui.Palette(True),
                source="global",
            )
        header = [l for l in out.splitlines() if "project" in l][0]
        self.assertTrue(header.endswith(f"<- {ck.root}"))

    def test_colored_output_strips_to_plain_output(self):
        """Plain-text fallback: strip_ansi(colored) == plain."""
        plain = self._render(ui.Palette(False))
        colored = self._render(ui.Palette(True))
        self.assertEqual(ui.strip_ansi(colored), plain)

    def test_zero_done_progress_is_native_not_green(self):
        plan = "# P\n- [ ] only open\n"
        out = self._render(ui.Palette(True), plan=plan)
        self.assertNotIn(f"{ui.GREEN}(0.0%){ui.RESET}", out)
        # 0% ratio inherits the native terminal color (no gray).
        self.assertIn("(0.0%)", out)
        self.assertNotIn("\033[90m(0.0%)", out)


# ---------------------------------------------------------------------------
# Pure-ASCII output guarantee (no emoji / box-drawing fallback glyphs)
# ---------------------------------------------------------------------------


class _IsolatedHome(unittest.TestCase):
    """Pin the global registry to a per-test tmp HOME."""

    def setUp(self):
        import tempfile
        from cklib import config as ckconfig
        from cklib import registry as ckregistry

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake_home = Path(self._tmp.name)

        self._mods = (ckconfig, ckregistry)
        self._orig = {
            (mod, name): getattr(mod, name)
            for mod in self._mods
            for name in (
                "GLOBAL_CONFIG_DIR", "GLOBAL_REGISTRY_FILE",
                "LEGACY_GLOBAL_CONFIG_FILE",
            )
        }
        new_dir = fake_home / ".config" / "ck"
        for mod in self._mods:
            mod.GLOBAL_CONFIG_DIR = new_dir
            mod.GLOBAL_REGISTRY_FILE = new_dir / "projects.json"
            mod.LEGACY_GLOBAL_CONFIG_FILE = fake_home / ".ckrc"

        def _restore():
            for (mod, name), value in self._orig.items():
                setattr(mod, name, value)
        self.addCleanup(_restore)


class TestAsciiOnlyOutput(_IsolatedHome):
    """Every read command must emit STRICT 7-bit ASCII text: no emoji,
    no box-drawing bars, no typographic dashes — zero broken fallback
    glyphs (▯) in terminals without Nerd Fonts."""

    PLAN = (
        "# P\n## Current Sprint\n"
        "- [x] prev task\n"
        "- [>] focus task\n"
        "- [ ] next task\n"
    )

    def _project(self) -> "ContextKeeper":
        root = Path(self._tmp.name) / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(self.PLAN, encoding="utf-8")
        return ck

    def test_plain_status_block_is_pure_ascii(self):
        ck = self._project()
        ck.set_note("ascii note")
        out = _render_local_status(
            ck, parse_plan(self.PLAN), palette=ui.Palette(False))
        self.assertTrue(out.isascii(), f"non-ASCII bytes: {out!r}")
        self.assertIn("* Note: ascii note", out)

    def test_colored_status_strips_to_pure_ascii(self):
        ck = self._project()
        out = _render_local_status(
            ck, parse_plan(self.PLAN), palette=ui.Palette(True))
        stripped = ui.strip_ansi(out)
        self.assertTrue(stripped.isascii())

    def test_global_fallback_header_annotation_is_ascii(self):
        ck = self._project()
        out = _render_local_status(
            ck, parse_plan(self.PLAN), palette=ui.Palette(False),
            source="global",
        )
        self.assertTrue(out.isascii())
        self.assertIn(f"<- {ck.root}", out)

    def test_no_project_state_is_ascii(self):
        out = ui.render_no_project(ui.Palette(False))
        self.assertTrue(out.isascii())
        self.assertIn("[!] No active project found.", out)

    def test_no_color_ck_st_end_to_end_is_ascii(self):
        """NO_COLOR=1 ck st: 100% plain ASCII, no escapes, no icons."""
        import os
        from cklib.cli import main

        ck = self._project()
        ck.set_note("test")
        buf = io.StringIO()
        orig_cwd = Path.cwd()
        env = {k: v for k, v in os.environ.items()
               if k not in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE",
                            "CK_SANDBOX", "CK_DEV")}
        env["NO_COLOR"] = "1"
        os.chdir(ck.root)
        try:
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("sys.stdout", buf):
                code = main(["st"])
        finally:
            os.chdir(orig_cwd)
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertNotIn("\033[", out)
        self.assertTrue(out.isascii(), f"non-ASCII bytes: {out!r}")
        self.assertIn("* Note: test", out)

    def test_ck_note_output_is_ascii_with_star_marker(self):
        """`ck note "test"` reports with the ASCII `* Note` marker."""
        import os
        from cklib.cli import main

        ck = self._project()
        buf = io.StringIO()
        orig_cwd = Path.cwd()
        env = {k: v for k, v in os.environ.items()
               if k not in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE",
                            "CK_SANDBOX", "CK_DEV")}
        os.chdir(ck.root)
        try:
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("sys.stdout", buf):
                code = main(["note", "test"])
        finally:
            os.chdir(orig_cwd)
        self.assertEqual(code, 0)
        self.assertIn("* Note saved for [2]: test", buf.getvalue())
        self.assertTrue(buf.getvalue().isascii())

    def test_dashboard_outputs_are_ascii(self):
        from cklib.core import _render_dashboard, _safe_parse_plan
        from cklib import registry as ckregistry
        from cklib.parser import parse_plan_file

        ck = self._project()
        ckregistry.register_project(ck.root, name="project")
        for verbose in (False, True):
            out = _render_dashboard(
                ck,
                list_projects=ckregistry.list_projects,
                parse_plan_file=parse_plan_file,
                verbose=verbose,
            )
            self.assertTrue(
                out.isascii(),
                f"non-ASCII bytes (verbose={verbose}): {out!r}",
            )


# ---------------------------------------------------------------------------
# Configurable palette: resolve_color + .ck.json "colors" overrides
# ---------------------------------------------------------------------------


class TestResolveColor(unittest.TestCase):
    def test_standard_names(self):
        self.assertEqual(ui.resolve_color("cyan"), "\033[36m")
        self.assertEqual(ui.resolve_color("green"), "\033[32m")
        self.assertEqual(ui.resolve_color(" Red "), "\033[31m")

    def test_bright_variants(self):
        self.assertEqual(ui.resolve_color("bright blue"), "\033[94m")
        self.assertEqual(
            ui.resolve_color("bright-black"), "\033[90m")

    def test_bold_prefix(self):
        self.assertEqual(ui.resolve_color("bold red"), "\033[1;31m")
        self.assertEqual(
            ui.resolve_color("Bold Blue"), "\033[1;34m")
        self.assertEqual(ui.resolve_color("bold"), "\033[1m")

    def test_raw_sgr_parameters(self):
        self.assertEqual(ui.resolve_color("94"), "\033[94m")
        self.assertEqual(ui.resolve_color("38;5;208"), "\033[38;5;208m")

    def test_native_tokens_return_none(self):
        for value in ("", "  ", "none", "default", "native", "reset",
                      "None"):
            self.assertIsNone(ui.resolve_color(value), repr(value))

    def test_invalid_values_return_none(self):
        for value in ("sparkles", "bold sparkles", "1;36;abc",
                      42, None, ["cyan"]):
            self.assertIsNone(ui.resolve_color(value), repr(value))


class TestPaletteColorOverrides(unittest.TestCase):
    def test_configured_slot_paints(self):
        p = ui.Palette(True, {"muted": "blue"})
        self.assertEqual(p.muted("x"), f"\033[34mx{ui.RESET}")

    def test_text_slot_cascades_to_other_slots(self):
        p = ui.Palette(True, {"text": "cyan"})
        for fn in (p.text, p.muted, p.border, p.accent):
            self.assertEqual(fn("x"), f"{ui.CYAN}x{ui.RESET}")

    def test_explicit_slot_overrides_text_cascade(self):
        p = ui.Palette(True, {"text": "cyan", "border": "bold red"})
        self.assertEqual(p.muted("x"), f"{ui.CYAN}x{ui.RESET}")
        self.assertEqual(p.border("x"), f"\033[1;31mx{ui.RESET}")

    def test_invalid_value_degrades_to_native(self):
        p = ui.Palette(True, {"muted": "sparkles"})
        self.assertEqual(p.muted("x"), "x")

    def test_native_value_degrades_to_native(self):
        p = ui.Palette(True, {"border": "none"})
        self.assertEqual(p.border("x"), "x")

    def test_disabled_palette_ignores_overrides(self):
        p = ui.Palette(False, {"muted": "blue", "accent": "red"})
        self.assertEqual(p.muted("x"), "x")
        self.assertEqual(p.accent("x"), "x")

    def test_non_dict_colors_are_ignored(self):
        p = ui.Palette(True, ["muted", "blue"])  # type: ignore[arg-type]
        self.assertEqual(p.muted("x"), "x")


class TestReadColorConfig(unittest.TestCase):
    """``.ck.json`` → ``{"colors": {...}}`` palette overrides."""

    def _root_with(self, config_text: str | None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "proj"
        (root / ".ck").mkdir(parents=True)
        if config_text is not None:
            (root / ".ck.json").write_text(config_text, encoding="utf-8")
        return root

    def test_missing_config_returns_empty(self):
        root = self._root_with(None)
        self.assertEqual(read_color_config(root), {})

    def test_reads_known_slots_only(self):
        root = self._root_with(json.dumps({
            "editor": "vim",
            "colors": {"muted": "blue", "border": "bold red",
                       "sparkle": "pink"},
        }))
        self.assertEqual(
            read_color_config(root),
            {"muted": "blue", "border": "bold red"},
        )

    def test_non_string_values_dropped(self):
        root = self._root_with(json.dumps(
            {"colors": {"muted": 42, "border": "blue"}}))
        self.assertEqual(read_color_config(root), {"border": "blue"})

    def test_missing_colors_key_returns_empty(self):
        root = self._root_with(json.dumps({"editor": "vim"}))
        self.assertEqual(read_color_config(root), {})

    def test_malformed_json_returns_empty(self):
        root = self._root_with("{ not json !!")
        self.assertEqual(read_color_config(root), {})

    def test_non_dict_colors_returns_empty(self):
        root = self._root_with(json.dumps({"colors": ["blue"]}))
        self.assertEqual(read_color_config(root), {})


class TestStatusAppliesColorConfig(unittest.TestCase):
    """Project config restyles ck st; unset slots stay native."""

    PLAN = (
        "# P\n## Current Sprint\n"
        "- [x] prev task\n"
        "- [>] focus task\n"
        "- [ ] next task\n"
    )

    def _project(self, config: dict | None = None) -> ContextKeeper:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "project"
        root.mkdir()
        ck = ContextKeeper(root=root)
        ck.ck_path.mkdir(parents=True, exist_ok=True)
        ck.plan_file.write_text(self.PLAN, encoding="utf-8")
        if config is not None:
            (root / ".ck.json").write_text(
                json.dumps(config), encoding="utf-8")
        return ck

    def _colored_status(self, ck: ContextKeeper) -> str:
        env = {k: v for k, v in os.environ.items()
               if k not in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE")}
        env["FORCE_COLOR"] = "1"
        with mock.patch.dict(os.environ, env, clear=True):
            return ck.status()

    def test_no_config_secondary_text_is_native(self):
        ck = self._project()
        out = self._colored_status(ck)
        self.assertIn("\033[", out)  # badges still colored
        for token in ("\033[2m", "\033[30m", "\033[90m"):
            self.assertNotIn(token, out)

    def test_configured_border_and_muted(self):
        ck = self._project({"colors": {
            "border": "cyan", "muted": "blue"}})
        out = self._colored_status(ck)
        self.assertIn(f"{ui.CYAN}{'=' * 61}{ui.RESET}", out)
        self.assertIn(f"\033[34m[%] Progress:", out)

    def test_configured_accent_paints_version_tag(self):
        from cklib.config import VERSION
        ck = self._project({"colors": {"accent": "magenta"}})
        out = self._colored_status(ck)
        self.assertIn(f"\033[35m[v{VERSION}]{ui.RESET}", out)

    def test_badge_colors_unchanged_by_config(self):
        ck = self._project({"colors": {"text": "red"}})
        ck.set_note("note text")
        out = self._colored_status(ck)
        # Standard badges keep their high-visibility colors.
        self.assertIn(
            f"{ui.GREEN}       - [1] prev task [x]{ui.RESET}", out)
        self.assertIn(
            f"{ui.BOLD}{ui.YELLOW}[2] focus task{ui.RESET}", out)
        self.assertIn(
            f"{ui.BOLD}{ui.CYAN}       * Note: note text{ui.RESET}", out)

    def test_text_cascade_colors_hints_and_borders(self):
        ck = self._project({"colors": {"text": "cyan"}})
        out = self._colored_status(ck)
        self.assertIn(f"{ui.CYAN}{'=' * 61}{ui.RESET}", out)
        self.assertIn(
            f"{ui.CYAN}       - [3] next task [ ]{ui.RESET}", out)

    def test_invalid_config_degrades_to_native(self):
        ck = self._project({"colors": {"muted": "sparkles"}})
        out = self._colored_status(ck)
        lines = out.splitlines()
        self.assertEqual(lines[0], "=" * 61)  # border unpainted
        self.assertNotIn("\033[90m", out)


class TestCliErrorColoring(unittest.TestCase):
    """ERROR wrappers print red when color is enabled, plain otherwise."""

    def test_error_plain_when_no_color(self):
        from cklib.cli import _print_error
        buf = io.StringIO()
        with _env(NO_COLOR="1"), mock.patch("sys.stdout", buf):
            _print_error("ERROR: something failed")
        self.assertEqual(buf.getvalue(), "ERROR: something failed\n")

    def test_error_red_when_forced(self):
        from cklib.cli import _print_error
        buf = io.StringIO()
        with _env(FORCE_COLOR="1"), mock.patch("sys.stdout", buf):
            _print_error("ERROR: something failed")
        self.assertEqual(
            buf.getvalue(),
            f"{ui.RED}ERROR: something failed{ui.RESET}\n",
        )


if __name__ == "__main__":
    unittest.main()
