#!/usr/bin/env python3
"""
Context Keeper CLI v0.0.9 - Инструмент для управления контекстом проекта и Git-интеграции.
"""

import os
import sys
import json
import shutil
import subprocess
import re
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path


# --- КОНФИГУРАЦИЯ ---
VERSION = "0.0.9"
CK_DIR_NAME = ".ck"
HISTORY_LIMIT = 5  # Количество ЗАПИСЕЙ (блоков ###) до архивации
DRY_RUN = False  # True → push имитируется, False → push на удалённый репозиторий


INSTALL_PATH = "/usr/local/bin/ck"
GLOBAL_CONFIG_DIR = Path.home() / ".config" / "ck"
GLOBAL_CONFIG_FILE = Path.home() / ".ckrc"
REMOTE_URL = "https://raw.githubusercontent.com/vinyardrip/context-keeper/main/ck"

HELP_TEXT = f"""
Context Keeper CLI [v{VERSION}]
Использование: ck <команда>

Команды:
st             Статус проекта, Git и инструментов.
st --all       Полный вывод: статус + все задачи из PLAN.md.
save           Сохранить прогресс + Локальный Commit и имитация Push.
edit           Правка ПЛАНА (PLAN.md).
log            Просмотр ЖУРНАЛА (HISTORY.md).
add <текст>    Добавить задачу в PLAN.md.
init           Инициализация проекта и проверка окружения.
install        Установка ck в систему (/usr/local/bin).
uninstall      Удаление ck из системы.
update         Проверить и установить обновление с GitHub.

Опции:
-v, --version  Версия.
-h, --help     Справка.
"""

# ========================================================================== #
#  ВСТРОЕННЫЕ ШАБЛОНЫ (Self-contained)                                       #
#  DEFAULT_PLAN      — шаблон .ck/PLAN.md при ck init                       #
#  DEFAULT_PROMPT    — шаблон .ck/prompt.md при ck init                     #
#  DEFAULT_GITIGNORE — шаблон .ck/.gitignore при ck init                    #
#  DEFAULT_README    — шаблон .ck/README.md при ck init                       #
# ========================================================================== #

DEFAULT_PLAN = """# {project_name}

## Current Sprint
- [] Описать первую задачу

## Completed
"""

DEFAULT_PROMPT = """# Context Keeper: AI System Instructions \U0001f916

You are a Senior Engineer assistant for the **Context Keeper (ck)** CLI utility.
Your goal is to generate or update the `PLAN.md` file based on the user's technical requirements.

## \u26a0\ufe0f STRICT FORMATTING RULES (DO NOT DEVIATE):

1. **Active Task Syntax**: Use strictly `- []` (Dash, Space, Empty Brackets).
   - \u2705 CORRECT: `- [] Task description`
   - \u274c WRONG: `-[] Task` (no space after dash)
   - \u274c WRONG: `- [ ] Task` (space inside brackets)

2. **Completed Task Syntax**: Use strictly `- [x]`.
   - \u2705 CORRECT: `- [x] Finished task`

3. **Flat Structure**: Do NOT use nested lists, tabs, or indentation. Every task must be a top-level list item.

4. **Task Selection Logic**: The `ck` utility identifies the **very first** occurrence of `- []` from the top of the file as the "Current Active Task". Ensure the most urgent task is always the first `- []` entry.

5. **Character Limit**: Keep task descriptions concise (under 80 characters).

## \U0001f6e0\ufe0f Your Workflow:
1. Break down complex features into small, atomic steps.
2. Output the Markdown content for `PLAN.md` using the exact syntax above.
3. Provide ONLY the Markdown block unless explicitly asked for a discussion.

## \U0001f4cb EXAMPLE OF A VALID PLAN.md:

# Project Name

## Current Sprint
- [] Add language support (RU/EN) during init
- [] Translate script system messages
- [] Implement history rotation logic

## Completed
- [x] Configure GitHub CLI integration
- [x] Create initial .ck directory structure
"""

DEFAULT_GITIGNORE = """# Context Keeper - Auto-generated
# Exclude backups and dynamic state from Git tracking

# History archives (rotated backups)
*.bak

# Dynamic state (regenerated automatically)
state.json

# Local config overrides
config.local.json
"""

# fmt: off
DEFAULT_README = (
    "# {project_name}\n"
    "\n"
    "<div align=\"center\">\n"
    "\n"
    "**Minimalist Unix-way \"external memory\" for developers**  \n"
    "**\u041c\u0438\u043d\u0438\u043c\u0430\u043b\u0438\u0441\u0442\u0438\u0447\u043d\u0430\u044f \u00ab\u0432\u043d\u0435\u0448\u043d\u044f\u044f \u043f\u0430\u043c\u044f\u0442\u044c\u00bb \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a\u0430 \u0432 \u0441\u0442\u0438\u043b\u0435 Unix**\n"
    "\n"
    "[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)\n"
    "[![Python](https://img.shields.io/badge/python-3.8+-3776ab.svg)](https://www.python.org/)\n"
    "[![Unix](https://img.shields.io/badge/unix-way-green.svg)](https://en.wikipedia.org/wiki/Unix_philosophy)\n"
    "\n"
    "[English](#-english) \u00b7 [\u0420\u0443\u0441\u0441\u043a\u0438\u0439](#-\u0440\u0443\u0441\u0441\u043a\u0438\u0439)\n"
    "\n"
    "</div>\n"
    "\n"
    "\n"
    "<a name=\"english--eng\"></a>\n"
    "## \U0001f1ec\U0001f1e7 English\n"
    "\n"
    "CLI bridge between your brain, AI agents, and Git. Persistently saves the current task context and project history in plain Markdown files.\n"
    "\n"
    "### \U0001f4cb Requirements\n"
    "\n"
    "- **Python 3.8+** (Required)\n"
    "- **Git** (VCS integration)\n"
    "- **fzf** (Fuzzy Finder) \u2014 for interactive menus\n"
    "- **jq** (JSON Processor) \u2014 for state management\n"
    "- **gh** or **glab** \u2014 for remote repository synchronization\n"
    "\n"
    "### \U0001f4e6 Installation\n"
    "\n"
    "```bash\n"
    "# One-liner install (download, make executable, move to PATH)\n"
    "curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/ck -o ck && chmod +x ck && sudo mv ck /usr/local/bin/ck\n"
    "\n"
    "# Verify\n"
    "ck -v\n"
    "```\n"
    "\n"
    "Alternatively, download manually and use the built-in installer:\n"
    "\n"
    "```bash\n"
    "curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/ck -o ck\n"
    "chmod +x ck\n"
    "./ck install\n"
    "```\n"
    "\n"
    "### \U0001f5c2 Project Structure\n"
    "\n"
    "Once initialized, the tool creates a `.ck/` directory in your project root:\n"
    "\n"
    "```\n"
    ".ck/\n"
    "\u251c\u2500\u2500 state.json        # Metadata (project name, branch, tools status)\n"
    "\u251c\u2500\u2500 PLAN.md           # Active task list (Source of Truth)\n"
    "\u251c\u2500\u2500 HISTORY.md        # Chronological log of completed tasks\n"
    "\u251c\u2500\u2500 prompt.md         # AI System Instructions (Template for PLAN.md)\n"
    "\u251c\u2500\u2500 .gitignore        # Internal backup/state exclusion rules\n"
    "\u2514\u2500\u2500 HISTORY_*.md.bak  # History archives (auto-created on rotation)\n"
    "```\n"
    "\n"
    "### \U0001f6e0\ufe0f Commands\n"
    "\n"
    "| Command              | Description |\n"
    "|----------------------|-------------|\n"
    "| `ck init`            | Initialize `.ck/`, detect project name, verify environment, create template files |\n"
    "| `ck edit`            | Open `PLAN.md` in your `$EDITOR`. Mark the active task with `- []` |\n"
    "| `ck save`            | Interactive task completion workflow |\n"
    "| `ck st`              | Status overview \u2014 current task and the last history entries |\n"
    "| `ck st --all`        | Full status \u2014 current task, history, and complete PLAN.md content |\n"
    "| `ck add <text>`      | Add a new task to PLAN.md directly from the terminal |\n"
    "| `ck log`             | View the full project history directly in the terminal/editor |\n"
    "| `ck install`         | Install ck system-wide to `/usr/local/bin` |\n"
    "| `ck uninstall`       | Remove ck from the system and optionally clean global configs |\n"
    "| `ck update`          | Check for updates on GitHub and auto-upgrade to the latest version |\n"
    "| `ck -h, --help`      | Full command reference and manual |\n"
    "| `ck -v, --version`   | Show current utility version |\n"
    "\n"
    "\n"
    "---\n"
    "\n"
    "<a name=\"\u0440\u0443\u0441\u0441\u043a\u0438\u0439--rus\"></a>\n"
    "## \U0001f1f7\U0001f1fa \u0420\u0443\u0441\u0441\u043a\u0438\u0439\n"
    "\n"
    "CLI-\u043c\u043e\u0441\u0442 \u043c\u0435\u0436\u0434\u0443 \u0432\u0430\u0448\u0438\u043c \u0440\u0430\u0437\u0443\u043c\u043e\u043c, \u0418\u0418-\u0430\u0433\u0435\u043d\u0442\u0430\u043c\u0438 \u0438 Git. \u0421\u043e\u0445\u0440\u0430\u043d\u044f\u0435\u0442 \u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442 \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u043a\u0438 \u0438 \u0438\u0441\u0442\u043e\u0440\u0438\u044e \u043f\u0440\u043e\u0435\u043a\u0442\u0430 \u0432 \u043f\u0440\u043e\u0441\u0442\u044b\u0445 Markdown-\u0444\u0430\u0439\u043b\u0430\u0445.\n"
    "\n"
    "### \U0001f4cb \u0422\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\u044f\n"
    "\n"
    "- **Python 3.8+** (\u041e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u043e)\n"
    "- **Git** (VCS \u0438\u043d\u0442\u0435\u0433\u0440\u0430\u0446\u0438\u044f)\n"
    "- **fzf** (Fuzzy Finder) \u2014 \u0434\u043b\u044f \u0438\u043d\u0442\u0435\u0440\u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u043c\u0435\u043d\u044e\n"
    "- **jq** (JSON Processor) \u2014 \u0434\u043b\u044f \u0440\u0430\u0431\u043e\u0442\u044b \u0441\u043e \u0441\u0442\u0435\u0439\u0442\u043e\u043c\n"
    "- **gh** \u0438\u043b\u0438 **glab** \u2014 \u0434\u043b\u044f \u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u0438 \u0441 \u0443\u0434\u0430\u043b\u0451\u043d\u043d\u044b\u043c\u0438 \u0440\u0435\u043f\u043e\u0437\u0438\u0442\u043e\u0440\u0438\u044f\u043c\u0438\n"
    "\n"
    "### \U0001f4e6 \u0423\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430\n"
    "\n"
    "```bash\n"
    "# \u0423\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430 \u043e\u0434\u043d\u043e\u0439 \u043a\u043e\u043c\u0430\u043d\u0434\u043e\u0439 (\u0441\u043a\u0430\u0447\u0430\u0442\u044c, \u0441\u0434\u0435\u043b\u0430\u0442\u044c \u0438\u0441\u043f\u043e\u043b\u043d\u044f\u0435\u043c\u044b\u043c, \u043f\u0435\u0440\u0435\u043c\u0435\u0441\u0442\u0438\u0442\u044c \u0432 PATH)\n"
    "curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/ck -o ck && chmod +x ck && sudo mv ck /usr/local/bin/ck\n"
    "\n"
    "# \u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430\n"
    "ck -v\n"
    "```\n"
    "\n"
    "\u0410\u043b\u044c\u0442\u0435\u0440\u043d\u0430\u0442\u0438\u0432\u043d\u043e \u2014 \u0440\u0443\u0447\u043d\u0430\u044f \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u0441 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u0435\u043c \u0432\u0441\u0442\u0440\u043e\u0435\u043d\u043d\u043e\u0433\u043e \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u0449\u0438\u043a\u0430:\n"
    "\n"
    "```bash\n"
    "curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/ck -o ck\n"
    "chmod +x ck\n"
    "./ck install\n"
    "```\n"
    "\n"
    "### \U0001f6e0\ufe0f \u041a\u043e\u043c\u0430\u043d\u0434\u044b\n"
    "\n"
    "| \u041a\u043e\u043c\u0430\u043d\u0434\u0430              | \u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 |\n"
    "|----------------------|----------|\n"
    "| `ck init`            | \u0418\u043d\u0438\u0446\u0438\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u044f `.ck/`, \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0435\u043d\u0438\u0435 \u0438\u043c\u0435\u043d\u0438 \u043f\u0440\u043e\u0435\u043a\u0442\u0430, \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u043e\u043a\u0440\u0443\u0436\u0435\u043d\u0438\u044f, \u0441\u043e\u0437\u0434\u0430\u043d\u0438\u0435 \u0448\u0430\u0431\u043b\u043e\u043d\u043e\u0432 |\n"
    "| `ck edit`            | \u041e\u0442\u043a\u0440\u044b\u0442\u044c `PLAN.md` \u0432 \u0440\u0435\u0434\u0430\u043a\u0442\u043e\u0440\u0435. \u0410\u043a\u0442\u0438\u0432\u043d\u0430\u044f \u0437\u0430\u0434\u0430\u0447\u0430 \u043f\u043e\u043c\u0435\u0447\u0430\u0435\u0442\u0441\u044f \u043a\u0430\u043a `- []` |\n"
    "| `ck save`            | \u0418\u043d\u0442\u0435\u0440\u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u0435 \u0437\u0430\u0434\u0430\u0447\u0438 |\n"
    "| `ck st`              | \u0421\u0442\u0430\u0442\u0443\u0441 \u043f\u0440\u043e\u0435\u043a\u0442\u0430 \u2014 \u0442\u0435\u043a\u0443\u0449\u0430\u044f \u0437\u0430\u0434\u0430\u0447\u0430 \u0438 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u0438\u0437 \u0438\u0441\u0442\u043e\u0440\u0438\u0438 |\n"
    "| `ck st --all`        | \u041f\u043e\u043b\u043d\u044b\u0439 \u0441\u0442\u0430\u0442\u0443\u0441 \u2014 \u0442\u0435\u043a\u0443\u0449\u0430\u044f \u0437\u0430\u0434\u0430\u0447\u0430, \u0438\u0441\u0442\u043e\u0440\u0438\u044f \u0438 \u0432\u0441\u0451 \u0441\u043e\u0434\u0435\u0440\u0436\u0438\u043c\u043e\u0435 PLAN.md |\n"
    "| `ck add <\u0442\u0435\u043a\u0441\u0442>`     | \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043d\u043e\u0432\u0443\u044e \u0437\u0430\u0434\u0430\u0447\u0443 \u0432 PLAN.md \u043d\u0430\u043f\u0440\u044f\u043c\u0443\u044e \u0438\u0437 \u0442\u0435\u0440\u043c\u0438\u043d\u0430\u043b\u0430 |\n"
    "| `ck log`             | \u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440 \u043f\u043e\u043b\u043d\u043e\u0439 \u0438\u0441\u0442\u043e\u0440\u0438\u0438 \u043f\u0440\u043e\u0435\u043a\u0442\u0430 \u043f\u0440\u044f\u043c\u043e \u0432 \u0442\u0435\u0440\u043c\u0438\u043d\u0430\u043b\u0435 |\n"
    "| `ck install`         | \u0423\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430 ck \u0432 \u0441\u0438\u0441\u0442\u0435\u043c\u0443 (/usr/local/bin) |\n"
    "| `ck uninstall`       | \u0423\u0434\u0430\u043b\u0435\u043d\u0438\u0435 ck \u0438\u0437 \u0441\u0438\u0441\u0442\u0435\u043c\u044b \u0441 \u043e\u043f\u0446\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u043e\u0439 \u043e\u0447\u0438\u0441\u0442\u043a\u043e\u0439 \u0433\u043b\u043e\u0431\u0430\u043b\u044c\u043d\u044b\u0445 \u043a\u043e\u043d\u0444\u0438\u0433\u043e\u0432 |\n"
    "| `ck update`          | \u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f \u043d\u0430 GitHub \u0438 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438 \u043e\u0431\u043d\u043e\u0432\u0438\u0442\u044c\u0441\u044f \u0434\u043e \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0439 \u0432\u0435\u0440\u0441\u0438\u0438 |\n"
    "| `ck -h, --help`      | \u041f\u043e\u043b\u043d\u0430\u044f \u0441\u043f\u0440\u0430\u0432\u043a\u0430 \u043f\u043e \u043a\u043e\u043c\u0430\u043d\u0434\u0430\u043c \u0438 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u0430\u043c |\n"
    "| `ck -v, --version`   | \u0412\u044b\u0432\u043e\u0434 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u0432\u0435\u0440\u0441\u0438\u0438 \u0443\u0442\u0438\u043b\u0438\u0442\u044b |\n"
)
# fmt: on


class ContextKeeper:
    """Основной класс для управления контекстом проекта."""

    def __init__(self):
        """Инициализация путей и состояния."""
        self.root = self._find_root()
        self.ck_path = self.root / CK_DIR_NAME
        self.state_file = self.ck_path / "state.json"
        self.plan_file = self.ck_path / "PLAN.md"
        self.history_file = self.ck_path / "HISTORY.md"
        self._remote_declined = False

    def _find_root(self):
        """Поиск корневой директории проекта."""
        curr = Path.cwd()
        for parent in [curr] + list(curr.parents):
            if (parent / CK_DIR_NAME).is_dir():
                return parent
        return curr

    def _get_editor(self):
        """Определение редактора по умолчанию."""
        editor = os.environ.get("EDITOR")
        if editor:
            return editor
        for fallback in ["micro", "nano", "vi"]:
            if shutil.which(fallback):
                return fallback
        return "vi"

    def _exec(self, cmd_list, silent=False):
        """Выполнение системной команды с поддержкой dry-run."""
        cmd_str = " ".join(cmd_list)
        is_network_write = any(x in cmd_list for x in ["push", "create"])
        is_gh_glab = any(x in cmd_list for x in ["gh", "glab"])

        if DRY_RUN and (is_network_write or is_gh_glab):
            if "remote" not in cmd_list and "branch" not in cmd_list:
                if not silent:
                    print(f"\U0001f9ea [ЗАГЛУШКА-ОБЛАКО]: {cmd_str}")
                return "dry_run_cloud_success"

        try:
            result = subprocess.run(
                cmd_list, capture_output=True, text=True, check=True
            )
            return result.stdout.strip()
        except Exception:
            return None

    def _check_tools(self):
        """Проверка доступности инструментов и версии Python."""

        # Проверка версии Python (возвращает True, если 3.8 или выше)
        python_version_ok = sys.version_info >= (3, 8)

        tools = {
            "python3.8+": {
                "status": python_version_ok,
                "desc": "Python Interpreter 3.8+",
            },
            "git": {"status": shutil.which("git") is not None, "desc": "Git VCS"},
            "gh": {"status": shutil.which("gh") is not None, "desc": "GitHub CLI"},
            "glab": {"status": shutil.which("glab") is not None, "desc": "GitLab CLI"},
            "fzf": {"status": shutil.which("fzf") is not None, "desc": "Fuzzy Finder"},
            "jq": {"status": shutil.which("jq") is not None, "desc": "JSON Processor"},
        }

        # Выведем предупреждение в консоль, если Python слишком старый
        if not python_version_ok:
            v = sys.version_info
            print(
                f"\u26a0\ufe0f  Warning: Python {v.major}.{v.minor} detected. Python 3.8+ is required for stable work."
            )

        return {name: info["status"] for name, info in tools.items()}

    def _is_git_repo(self):
        """Проверка, находимся ли мы в Git-репозитории."""
        res = self._exec(["git", "rev-parse", "--is-inside-work-tree"], silent=True)
        return res is not None

    def _get_recent_history(self, count=3):
        """Получение последних записей из истории."""
        if not self.history_file.exists():
            return []
        content = self.history_file.read_text()
        entries = re.split(r"\n(?=### )", content)
        if entries and not entries[0].strip().startswith("###"):
            entries.pop(0)
        return [e.strip() for e in entries[-count:]]

    def _rotate(self):
        """Архивация истории с сохранением последней записи для контекста."""
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        arch = self.ck_path / f"HISTORY_{ts}.md.bak"

        content = self.history_file.read_text()
        last_entry_start = content.rfind("### ")
        last_entry = content[last_entry_start:] if last_entry_start != -1 else ""

        self.history_file.rename(arch)
        header = f"# История {self.root.name}\nАрхив: {arch.name}\n\n"
        self.history_file.write_text(header + last_entry)
        print(
            f"\U0001f5c4 История достигла лимита ({HISTORY_LIMIT} записей) и была архивирована. Контекст сохранен."
        )

    # ------------------------------------------------------------------ #
    #                         COMMAND: init                               #
    # ------------------------------------------------------------------ #

    def _ensure_ck_dir(self):
        """Гарантированное создание .ck/ если не существует."""
        if not self.ck_path.exists():
            self.ck_path.mkdir(parents=True)

    def _write_if_missing(self, filepath, content, label):
        """Создать файл с содержимым, только если он ещё не существует."""
        if not filepath.exists():
            filepath.write_text(content, encoding="utf-8")
            print(f"\U0001f4dd Создан шаблон: {label}")
            return True
        print(f"\u2139\ufe0f Уже существует: {label}")
        return False

    def init(self):
        """Инициализация проекта Context Keeper.

        Создаёт полную структуру:
          .ck/state.json      — метаданные проекта (перезаписывается)
          .ck/PLAN.md         — шаблон плана задач
          .ck/prompt.md       — инструкция для ИИ
          .ck/.gitignore      — правила исключения бэкапов и state.json
          .ck/README.md       — справка по Context Keeper (команды, установка)
          .gitignore          — корневой gitignore (дополнение, не перезапись)
        """
        self._ensure_ck_dir()

        # --- state.json (всегда перезаписывается при init) ---
        tools_status = self._check_tools()
        state = {
            "version": VERSION,
            "project_name": self.root.name,
            "created_at": datetime.now().isoformat(),
            "last_update": datetime.now().isoformat(),
            "current_step": "Init",
            "tools": tools_status,
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

        # --- Корневой .gitignore (дополнение, не перезапись) ---
        gitignore = self.root / ".gitignore"
        ignore_entry = "\n# Context Keeper\n.ck/*.bak\n"
        if gitignore.exists():
            if ".ck/*.bak" not in gitignore.read_text():
                with open(gitignore, "a") as f:
                    f.write(ignore_entry)
        else:
            gitignore.write_text(ignore_entry)

        # --- Шаблоны файлов в .ck/ (только если не существуют) ---
        self._write_if_missing(
            self.ck_path / "PLAN.md",
            DEFAULT_PLAN.format(project_name=self.root.name),
            "PLAN.md",
        )
        self._write_if_missing(
            self.ck_path / "prompt.md",
            DEFAULT_PROMPT,
            "prompt.md",
        )
        self._write_if_missing(
            self.ck_path / ".gitignore",
            DEFAULT_GITIGNORE,
            ".ck/.gitignore",
        )

        # --- README.md внутри .ck/ (справка по ck, рядом с остальными шаблонами) ---
        self._write_if_missing(
            self.ck_path / "README.md",
            DEFAULT_README.format(project_name=self.root.name),
            "README.md",
        )

        print(f"\u2705 Context Keeper v{VERSION} инициализирован.")

    # ------------------------------------------------------------------ #
    #                       COMMAND: st / st --all                        #
    # ------------------------------------------------------------------ #

    def _get_current_task(self):
        """Получение текущей активной задачи из плана."""
        if not self.plan_file.exists():
            return "Нет активной задачи"
        content = self.plan_file.read_text()
        match = re.search(r"-\s*\[\s*\]\s*(.*)", content)
        if not match:
            match = re.search(r"-\s*\[\]\s*(.*)", content)
        return match.group(1).strip() if match else "Все задачи завершены"

    def status(self):
        """Отображение статуса проекта."""
        if not self.state_file.exists():
            return print("Проект не найден.")
        with open(self.state_file, "r") as f:
            s = json.load(f)

        print("\n" + "\u2550" * 45)
        print(f" \U0001f680 ПРОЕКТ: {s['project_name']} [v{VERSION}]")
        print(f" \U0001f3af ТЕКУЩИЙ ШАГ: {self._get_current_task()}")

        if self._is_git_repo():
            br = self._exec(["git", "branch", "--show-current"], silent=True)
            print(f" \U0001f33f ВЕТКА: {br or 'unknown'}")

        tools = [t for t, status in s.get("tools", {}).items() if status]
        if tools:
            print(f" \U0001f6e0\ufe0f  ИНСТРУМЕНТЫ: {', '.join(tools)}")
        print("\u2550" * 45)

        recent = self._get_recent_history(count=3)
        if recent:
            print("\n \U0001f4dc ПОСЛЕДНИЕ ЗАПИСИ:")
            for entry in recent:
                lines = entry.splitlines()
                if not lines:
                    continue

                print(f"  {lines[0].replace('### ', '\U0001f539 ')}")

                in_code_block = False
                for line in lines[1:]:
                    clean = line.strip()
                    if clean.startswith("```"):
                        in_code_block = not in_code_block
                        print(f"    {'\u250c' if in_code_block else '\u2514'}{'\u2500' * 40}")
                        continue

                    if in_code_block:
                        print(f"    \u2502 {clean}")
                    elif clean:
                        print(f"    {clean}")
                print("")
        print("\u2550" * 35 + " [Конец Статуса] " + "\u2550" * 5 + "\n")

    def status_all(self):
        """Полный вывод: статус + все задачи из PLAN.md."""
        self.status()

        if self.plan_file.exists():
            content = self.plan_file.read_text()
            print("\u2550" * 45)
            print(" \U0001f4cb ПОЛНЫЙ ПЛАН (PLAN.md)")
            print("\u2550" * 45)
            print(content)
        else:
            print("\u26a0\ufe0f PLAN.md не найден.\n")

    # ------------------------------------------------------------------ #
    #                         COMMAND: save                               #
    # ------------------------------------------------------------------ #

    def _handle_git_sync(self, task, comment):
        """Обработка Git-синхронизации."""
        if not self._is_git_repo():
            if input("\u2049\ufe0f Git не найден. Инициализировать? (y/N): ").lower() == "y":
                self._exec(["git", "init"])
            else:
                return

        remote = self._exec(["git", "remote"], silent=True)
        if not remote and not self._remote_declined:
            with open(self.state_file, "r") as f:
                state_data = json.load(f)
                tools = state_data.get("tools", {})
            if tools.get("gh"):
                if (
                    input(
                        "\U0001f310 Remote не найден. Создать на GitHub через gh? (y/N): "
                    ).lower()
                    == "y"
                ):
                    vis = (
                        "--public"
                        if input("\U0001f513 Сделать публичным? (y/N): ").lower() == "y"
                        else "--private"
                    )
                    self._exec(
                        [
                            "gh",
                            "repo",
                            "create",
                            self.root.name,
                            vis,
                            "--source=.",
                            "--push",
                        ]
                    )
                else:
                    self._remote_declined = True

        print("\nТип изменений:")
        print(
            "1: feat  (новый функционал)\n2: fix   (исправление)\n3: chore (рутина)\n4: docs  (документация)\n5: custom (свой вариант)"
        )
        choice = input("Выберите (1-5, по умолчанию 1): ")

        if choice == "5":
            prefix = (
                input("Введите свой префикс (например, refactor): ").strip().lower()
            )
            if not prefix:
                prefix = "feat"
        else:
            prefix = {"2": "fix", "3": "chore", "4": "docs"}.get(choice, "feat")

        if input(f"\U0001f680 Выполнить коммит '{prefix}: ...' и Push? (y/N): ").lower() == "y":
            commit_msg = f"{prefix}: [{task}] {comment}"
            self._exec(["git", "add", "."])
            if self._exec(["git", "commit", "-m", commit_msg]):
                print("\U0001f4e4 Отправка в облако...")
                self._exec(["git", "push"])
                print("\u2705 Синхронизация завершена.")

    def save(self):
        """Сохранение прогресса и Git-коммит."""
        if not self.state_file.exists():
            return print("\u274c Сначала: ck init")

        task = self._get_current_task()
        print(f"\U0001f4cd Задача: {task}")
        comment = input("\U0001f4dd Описание: ").strip() or "update"

        log_content = ""
        if input("\U0001f4ce Добавить тех-лог? (y/N): ").lower() == "y":
            print("--- ВВОД (Ctrl+D для завершения) ---")
            try:
                log_content = sys.stdin.read().strip()
            except EOFError:
                pass

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n### {ts} | {task}\n- {comment}\n"
        if log_content:
            entry += f"\n```text\n{log_content}\n```\n"

        with open(self.history_file, "a") as f:
            f.write(entry)

        with open(self.state_file, "r") as f:
            state = json.load(f)
        state["last_update"] = datetime.now().isoformat()
        state["current_step"] = task
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

        print("\U0001f4be История сохранена.")
        self._handle_git_sync(task, comment)

        # ПРОВЕРКА ЛИМИТА ПО ЗАПИСЯМ (###) И РОТАЦИЯ
        if self.history_file.exists():
            if self.history_file.read_text().count("### ") > HISTORY_LIMIT:
                self._rotate()

    # ------------------------------------------------------------------ #
    #                         COMMAND: add                                #
    # ------------------------------------------------------------------ #

    def add_task(self, task_text):
        """Добавление задачи в PLAN.md.

        Если .ck/ не существует — создаёт директорию и PLAN.md
        с правильной структурой из DEFAULT_PLAN.
        """
        if not task_text.strip():
            return print("\u274c Укажите текст задачи: ck add <текст>")

        task_line = f"- [] {task_text.strip()}\n"

        # Гарантируем, что .ck/ существует
        self._ensure_ck_dir()

        if not self.plan_file.exists():
            # Создаём PLAN.md с полной структурой из шаблона,
            # но вставляем задачу вместо плейсхолдера
            plan_content = DEFAULT_PLAN.format(project_name=self.root.name)
            # Заменяем плейсхолдер первой задачи на реальную
            plan_content = re.sub(
                r"- \[\] Описать первую задачу\n", task_line, plan_content
            )
            self.plan_file.write_text(plan_content, encoding="utf-8")
            print(f"\u2705 Задача добавлена (PLAN.md создан):")
            print(f"   {task_line.strip()}")
            return

        content = self.plan_file.read_text()

        # Вставить перед секцией "## Completed", если она существует
        completed_match = re.search(r"^## [Cc]ompleted", content, re.MULTILINE)
        if completed_match:
            insert_pos = completed_match.start()
            new_content = content[:insert_pos] + task_line + content[insert_pos:]
        else:
            new_content = content.rstrip() + "\n" + task_line

        self.plan_file.write_text(new_content, encoding="utf-8")
        print(f"\u2705 Задача добавлена в PLAN.md:")
        print(f"   {task_line.strip()}")

    # ------------------------------------------------------------------ #
    #                       COMMAND: install                              #
    # ------------------------------------------------------------------ #

    def install(self):
        """Установка ck в систему (/usr/local/bin)."""
        script_path = Path(__file__).resolve()

        if not script_path.exists():
            print(f"\u274c Не удалось найти исходный скрипт: {script_path}")
            return

        if Path(INSTALL_PATH).exists():
            overwrite = input(
                f"\u26a0\ufe0f {INSTALL_PATH} уже существует. Перезаписать? (y/N): "
            )
            if overwrite.lower() != "y":
                print("\u274c Установка отменена.")
                return

        print(f"\U0001f4e6 Установка Context Keeper v{VERSION}...")

        try:
            shutil.copy2(str(script_path), INSTALL_PATH)
            print(f"\u2705 Установлено: {INSTALL_PATH}")
        except PermissionError:
            print("\U0001f512 Недостаточно прав. Запрашиваем sudo...")
            result = subprocess.run(
                ["sudo", "cp", str(script_path), INSTALL_PATH]
            )
            if result.returncode == 0:
                print(f"\u2705 Установлено: {INSTALL_PATH} (через sudo)")
            else:
                print("\u274c Ошибка установки. Проверьте права доступа.")
                return
        except Exception as e:
            print(f"\u274c Ошибка: {e}")
            return

        print("\U0001f389 Context Keeper готов к работе. Запустите: ck init")

    # ------------------------------------------------------------------ #
    #                      COMMAND: uninstall                             #
    # ------------------------------------------------------------------ #

    def uninstall(self):
        """Удаление ck из системы."""
        if not Path(INSTALL_PATH).exists():
            print("\u2139\ufe0f Context Keeper не найден в системе.")
            return

        confirm = input(f"\U0001f5d1 Удалить {INSTALL_PATH}? (y/N): ")
        if confirm.lower() != "y":
            print("\u274c Отменено.")
            return

        try:
            Path(INSTALL_PATH).unlink()
            print(f"\u2705 Бинарник удалён: {INSTALL_PATH}")
        except PermissionError:
            result = subprocess.run(["sudo", "rm", INSTALL_PATH])
            if result.returncode == 0:
                print(f"\u2705 Бинарник удалён: {INSTALL_PATH} (через sudo)")
            else:
                print("\u274c Ошибка удаления.")
                return
        except Exception as e:
            print(f"\u274c Ошибка: {e}")
            return

        # Проверка глобальных конфигов
        global_paths = [GLOBAL_CONFIG_DIR, GLOBAL_CONFIG_FILE]
        existing = [p for p in global_paths if p.exists()]

        if existing:
            print("\n\U0001f4e6 Найдены глобальные конфигурации:")
            for p in existing:
                print(f"   \u2022 {p}")

            if input("Удалить их? (y/N): ").lower() == "y":
                for p in existing:
                    try:
                        if p.is_dir():
                            shutil.rmtree(p)
                        else:
                            p.unlink()
                        print(f"   \U0001f5d1 Удалено: {p}")
                    except Exception as e:
                        print(f"   \u274c Ошибка удаления {p}: {e}")

        print("\n\U0001f44b Context Keeper удалён из системы.")

    # ------------------------------------------------------------------ #
    #                       COMMAND: update                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_version(ver_str):
        """Парсинг версии '0.0.9' → кортеж (0, 0, 9)."""
        try:
            return tuple(int(x) for x in ver_str.strip().split("."))
        except (ValueError, AttributeError):
            return (0, 0, 0)

    def update(self):
        """Проверить и установить обновление с GitHub.

        Скачивает свежий скрипт по REMOTE_URL, извлекает VERSION,
        сравнивает с текущей. Если версия новее — перезаписывает
        исполняемый файл (с sudo, если необходимо).
        """
        print(f"\U0001f50d Проверка обновлений... (текущая версия: {VERSION})")

        try:
            req = urllib.request.Request(REMOTE_URL, headers={"User-Agent": "ck-updater"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                remote_data = resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            print(f"\u274c Не удалось подключиться к GitHub: {e.reason}")
            return
        except Exception as e:
            print(f"\u274c Ошибка загрузки: {e}")
            return

        # Извлекаем VERSION из скачанного скрипта
        match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', remote_data, re.MULTILINE)
        if not match:
            print("\u274c Не удалось определить версию удалённого скрипта.")
            return

        remote_version = match.group(1)
        current_tuple = self._parse_version(VERSION)
        remote_tuple = self._parse_version(remote_version)

        print(f"   Удалённая версия: {remote_version}")

        if remote_tuple <= current_tuple:
            print("\u2705 У вас уже последняя версия.")
            return

        print(f"\U0001f680 Доступно обновление: {VERSION} \u2192 {remote_version}")

        confirm = input("Обновить? (y/N): ")
        if confirm.lower() != "y":
            print("\u274c Обновление отменено.")
            return

        # Определяем путь к текущему исполняемому файлу
        target = Path(__file__).resolve()

        try:
            target.write_text(remote_data, encoding="utf-8")
            target.chmod(target.stat().st_mode | 0o755)
            print(f"\u2705 Обновлено: {target}")
        except PermissionError:
            print("\U0001f512 Недостаточно прав. Запрашиваем sudo...")
            # Пишем во временный файл, затем sudo cp
            tmp_path = Path(f"/tmp/ck_update_{remote_version}")
            try:
                tmp_path.write_text(remote_data, encoding="utf-8")
                tmp_path.chmod(0o755)
                result = subprocess.run(
                    ["sudo", "cp", str(tmp_path), str(target)]
                )
                if result.returncode == 0:
                    tmp_path.unlink(missing_ok=True)
                    print(f"\u2705 Обновлено: {target} (через sudo)")
                else:
                    print("\u274c Ошибка обновления через sudo.")
                    return
            except Exception as e:
                print(f"\u274c Ошибка: {e}")
                return
        except Exception as e:
            print(f"\u274c Ошибка записи: {e}")
            return

        print(f"\U0001f389 Context Keeper обновлён до v{remote_version}.")


def main():
    """Точка входа CLI."""
    ck = ContextKeeper()
    args = sys.argv[1:]
    if not args or "-h" in args:
        print(HELP_TEXT)
        return
    if "-v" in args:
        print(f"ck version {VERSION}")
        return

    cmd = args[0]
    if cmd == "init":
        ck.init()
    elif cmd == "save":
        ck.save()
    elif cmd == "st":
        if "--all" in args:
            ck.status_all()
        else:
            ck.status()
    elif cmd == "edit":
        subprocess.run([ck._get_editor(), ck.plan_file])
    elif cmd == "log":
        subprocess.run([ck._get_editor(), ck.history_file])
    elif cmd == "add":
        if len(args) < 2:
            print("\u274c Укажите текст задачи: ck add <текст>")
        else:
            ck.add_task(" ".join(args[1:]))
    elif cmd == "install":
        ck.install()
    elif cmd == "uninstall":
        ck.uninstall()
    elif cmd == "update":
        ck.update()
    else:
        print("Неизвестная команда. ck -h для справки.")


if __name__ == "__main__":
    main()
