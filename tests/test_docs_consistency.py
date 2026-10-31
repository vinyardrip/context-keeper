"""Documentation consistency: version bump and roadmap hygiene.

Guards the v0.9.2 documentation cleanup:

- every version source agrees (package, pyproject, README, ROADMAP);
- completed features (Shell Completion) never reappear in the
  README backlog sections;
- the English and Russian roadmap sections stay in sync (same number
  of pending / completed entries).
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
