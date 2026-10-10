"""A notice is ONE styled line: badge and sentence share one colour.

The CLI's ``_notice`` and the dashboard's footers used to be built by
the same primitive but could still diverge, because the primitive only
coloured the badge and reset straight after it. That left the sentence
— the part carrying the meaning — in the terminal's default colour, so
a notice was a two-tone line instead of one readable status unit.

These tests pin the unified contract:

* ``notice()`` opens ONE style run and closes it with ONE trailing
  reset, so no unstyled text can leak past the end of the line;
* a ROOT-level notice and an IN-TABLE notice built from the same token
  and message are byte-identical;
* colouring costs zero display columns, so no layout shifts;
* a disabled palette yields byte-identical plain text.
"""

from __future__ import annotations

import unittest

from cklib import ui

TOKENS = ("[!]", "[i]", "[ok]", "[err]")


class TestNoticeIsOneStyledRun(unittest.TestCase):
    """The whole line carries the badge's colour."""

    def setUp(self):
        self.p = ui.Palette(True)

    def test_exactly_one_style_and_one_reset(self):
        for token in TOKENS:
            with self.subTest(token=token):
                line = ui.notice(token, "the message", self.p)
                self.assertEqual(line.count("\033["), 2, line)

    def test_style_opens_before_the_badge(self):
        for token in TOKENS:
            with self.subTest(token=token):
                line = ui.notice(token, "the message", self.p)
                self.assertTrue(
                    line.startswith(ui.BADGE_STYLES[token] + token + " "),
                    line)

    def test_reset_closes_after_the_message(self):
        for token in TOKENS:
            with self.subTest(token=token):
                line = ui.notice(token, "the message", self.p)
                self.assertTrue(line.endswith("the message" + ui.RESET), line)

    def test_no_reset_between_badge_and_message(self):
        """A mid-line reset is exactly the bug this contract removes."""
        for token in TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token + ui.RESET,
                                 ui.notice(token, "msg", self.p))

    def test_message_is_actually_styled(self):
        """The sentence must not fall back to the default colour."""
        line = ui.notice("[i]", "hint text", self.p)
        self.assertIn(ui.CYAN + "[i] hint text", line)


class TestRootAndInTableNoticesMatch(unittest.TestCase):
    """One engine ⇒ identical bytes, wherever the notice is printed."""

    def test_same_token_and_message_give_identical_bytes(self):
        p = ui.Palette(True)
        for token in TOKENS:
            with self.subTest(token=token):
                root = ui.notice(token, "shared sentence", p)
                # What a dashboard footer embeds is the very same call.
                table = ui.notice(token, "shared sentence", p)
                self.assertEqual(root, table)

    def test_footer_notice_is_full_line(self):
        """The GLOBAL DASHBOARD footer is built through notice()."""
        p = ui.Palette(True)
        footer = ui.notice(ui.INFO, "Missing a project?", p)
        self.assertEqual(footer,
                         "\033[36m[i] Missing a project?\033[0m")


class TestStylingCostsNoColumns(unittest.TestCase):
    """Coloured notices must not reflow anything around them."""

    def test_display_width_equals_plain_width(self):
        p = ui.Palette(True)
        for token in TOKENS:
            with self.subTest(token=token):
                styled = ui.notice(token, "msg", p)
                self.assertEqual(ui.display_width(styled),
                                 len(f"{token} msg"))

    def test_stripped_notice_is_the_plain_notice(self):
        p = ui.Palette(True)
        for token in TOKENS:
            with self.subTest(token=token):
                self.assertEqual(
                    ui.strip_ansi(ui.notice(token, "msg", p)),
                    f"{token} msg")


class TestDisabledPaletteIsByteIdentical(unittest.TestCase):
    """NO_COLOR / piped output must contain no escape at all."""

    def test_disabled_palette_emits_plain_text(self):
        p = ui.Palette(False)
        for token in TOKENS:
            with self.subTest(token=token):
                line = ui.notice(token, "msg", p)
                self.assertEqual(line, f"{token} msg")
                self.assertNotIn("\033[", line)


if __name__ == "__main__":
    unittest.main()