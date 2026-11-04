"""Documentation consistency: version bump and roadmap hygiene.

Guards the v0.12.0 documentation update:

- every version source agrees (package, pyproject, README, ROADMAP);
- completed features (Shell Completion) never reappear in the
  README backlog sections;
- the English and Russian roadmap sections stay in sync (same number
  of pending / completed entries);
- the strict space-routing rule and the dev-session ``ck info`` note
  are documented in both languages (v0.9.3);
- append-only process notes (``ck note <ID>``, ``--list``,
  ``--clear``, array storage and legacy migration) are documented in
  both languages (v0.10.0);
- the unified note timestamp format and HISTORY multi-note rule are
  documented in both languages (v0.10.1);
- the grouped-help quick reference appears near the Commands table in
  both languages (v0.11.0);
- `ck -h` color behavior and the pipe/NO_COLOR rules are documented in
  both languages (v0.11.1);
- the daily journal `today` (init/migrate, rollover, restrictions) is
  documented in both languages and in the Commands table (v0.12.0).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from cklib import config as ckconfig

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
ROADMAP = ROOT / "ROADMAP.md"
PYPROJECT = ROOT / "pyproject.toml"


def _section(text: str, marker: str) -> str:
    """Return the body of the LAST ``###`` section introduced by ``marker``.

    ``rindex`` avoids matching prose headings such as
    ``### Completed tasks are never reordered`` earlier in the README.
    """
    start = text.rindex(marker) + len(marker)
    rest = text[start:]
    ends = [pos for pos in (rest.find("\n### "), rest.find("\n---"))
            if pos != -1]
    return rest[:min(ends)] if ends else rest


def _checklist(section: str, marker: str) -> list[str]:
    return [line for line in section.splitlines()
            if line.strip().startswith(f"- [{marker}]")]


class TestDocumentationConsistency(unittest.TestCase):
    def setUp(self):
        self.readme = README.read_text(encoding="utf-8")
        self.roadmap = ROADMAP.read_text(encoding="utf-8")
        self.version = ckconfig.VERSION

    def test_version_sources_agree(self):
        pyproject = PYPROJECT.read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), self.version)
        self.assertIn(f"version-{self.version}-blue", self.readme)
        self.assertIn(f"ck version {self.version}", self.readme)
        self.assertIn(f"Current version: v{self.version}", self.roadmap)

    def test_shell_completion_is_not_in_any_backlog_section(self):
        english = _section(self.readme, "### Ideas & Discussion")
        russian = _section(self.readme, "### Идеи и обсуждения")
        for section in (english, russian):
            self.assertNotIn("Shell Completion", section)
            self.assertNotIn("автодополнение команд", section)

    def test_shell_completion_is_listed_as_completed(self):
        english = _section(self.readme, "### Completed")
        russian = _section(self.readme, "### Завершённые")
        self.assertIn("Shell Completion", english)
        self.assertIn("Shell Completion", russian)

    def test_strict_routing_and_dev_info_are_documented_bilingually(self):
        """v0.9.3: both notes must exist in the EN and RU sections."""
        self.assertGreaterEqual(
            self.readme.count("routes only to spaces"), 2)
        self.assertGreaterEqual(
            self.readme.count("Why strict, not magical"), 2)
        self.assertGreaterEqual(
            self.readme.count("`ck info` in a dev session"), 2)

    def test_append_only_notes_are_documented_bilingually(self):
        """v0.10.0: both Process Notes sections describe the new CLI."""
        english = _section(
            self.readme, "## Process Notes & Unfocused / Paused Context")
        russian = _section(
            self.readme, "## Процессные заметки и Unfocused / Paused Context")
        for section in (english, russian):
            self.assertIn("ck note <ID> <text>", section)
            self.assertIn("--list", section)
            self.assertIn("--clear", section)
            self.assertIn("ISO-8601", section)
            self.assertIn("state.json", section)

    def test_note_timestamp_rule_is_documented_bilingually(self):
        """v0.10.1: timestamp format and HISTORY multi-note rule."""
        english = _section(
            self.readme, "## Process Notes & Unfocused / Paused Context")
        russian = _section(
            self.readme, "## Процессные заметки и Unfocused / Paused Context")
        for section in (english, russian):
            self.assertIn("HH:MM UTC", section)
            self.assertIn("YYYY-MM-DD HH:MM UTC", section)
            self.assertIn("* Note: <text>", section)
            self.assertIn("active_task.note", section)
        self.assertIn("**Note timestamps.**", english)
        self.assertIn("**Метки времени заметок.**", russian)

    def test_quick_reference_documented_bilingually(self):
        """v0.11.0: grouped-help quick ref near the Commands tables."""
        for marker, needle in (
            ("## 🛠 Commands", "**Quick reference:**"),
            ("## 🛠 Команды", "**Краткая справка:**"),
        ):
            start = self.readme.rindex(marker)
            window = self.readme[start:start + 500]
            self.assertIn(needle, window, marker)
            self.assertIn("ck -h", window, marker)

    def test_help_color_policy_documented_bilingually(self):
        """v0.11.1: color scheme plus NO_COLOR and pipe guarantees."""
        self.assertIn(
            "`ck -h` uses the same color scheme as status badges",
            self.readme,
        )
        self.assertIn("Piped output is always plain.", self.readme)
        self.assertIn(
            "`ck -h` использует ту же цветовую схему", self.readme)
        self.assertIn(
            "При перенаправлении вывод всегда без ANSI.", self.readme)

    def test_daily_journal_documented_bilingually(self):
        """v0.12.0: daily journal init/migrate/rollover/rules."""
        english = _section(self.readme, "### Daily Journal (`today`)")
        russian = _section(self.readme, "### Ежедневный журнал (`today`)")
        for section in (english, russian):
            self.assertIn("ck today init", section)
            self.assertIn("ck today migrate", section)
            self.assertIn("today_date", section)
            self.assertIn("HISTORY.md", section)
            self.assertIn("DAILY_SPACES", section)
            self.assertIn("`ck today save`", section)
        self.assertIn("local midnight", english)
        self.assertIn("локальная полночь", russian)
        self.assertIn("| ck today init |", self.readme)
        self.assertIn("| ck today migrate", self.readme)

    def test_bilingual_roadmap_checklists_have_parity(self):
        pairs = (
            ("### Active / Pending Features", "### Активные / запланированные задачи"),
            ("### Ideas & Discussion", "### Идеи и обсуждения"),
            ("### Completed", "### Завершённые"),
        )
        for en_marker, ru_marker in pairs:
            en = _section(self.readme, en_marker)
            ru = _section(self.readme, ru_marker)
            en_open = _checklist(en, " ")
            ru_open = _checklist(ru, " ")
            en_done = _checklist(en, "x")
            ru_done = _checklist(ru, "x")
            self.assertEqual(len(en_open), len(ru_open), en_marker)
            self.assertEqual(len(en_done), len(ru_done), en_marker)


if __name__ == "__main__":
    unittest.main()
