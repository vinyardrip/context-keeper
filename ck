#!/usr/bin/env python3
"""
Context Keeper CLI - Инструмент для управления контекстом проекта и Git-интеграции.
"""

import os
import sys
import json
import shutil
import subprocess
import re
from datetime import datetime
from pathlib import Path


# --- КОНФИГУРАЦИЯ ---
VERSION = "0.0.7-git-pro"
CK_DIR_NAME = ".ck"
HISTORY_LIMIT = 5  # Количество ЗАПИСЕЙ (блоков ###) до архивации
DRY_RUN = True  # True: Локальный commit РАБОТАЕТ, сетевой Push ИМИТИРУЕТСЯ.

HELP_TEXT = f"""
Context Keeper CLI [v{VERSION}]
Использование: ck <команда>

Команды:
st             Статус проекта, Git и инструментов.
save           Сохранить прогресс + Локальный Commit и имитация Push.
edit           Правка ПЛАНА (PLAN.md).
log            Просмотр ЖУРНАЛА (HISTORY.md).
init           Инициализация проекта и проверка окружения.

Опции:
-v, --version  Версия.
-h, --help     Справка.
"""


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
                    print(f"🧪 [ЗАГЛУШКА-ОБЛАКО]: {cmd_str}")
                return "dry_run_cloud_success"

        try:
            result = subprocess.run(
                cmd_list, capture_output=True, text=True, check=True
            )
            return result.stdout.strip()
        except Exception:
            return None

    def _check_tools(self):
        """Проверка доступности инструментов."""
        tools = {
            "git": {"status": shutil.which("git") is not None, "desc": "Git VCS"},
            "gh": {"status": shutil.which("gh") is not None, "desc": "GitHub CLI"},
            "glab": {"status": shutil.which("glab") is not None, "desc": "GitLab CLI"},
            "fzf": {"status": shutil.which("fzf") is not None, "desc": "Fuzzy Finder"},
            "jq": {"status": shutil.which("jq") is not None, "desc": "JSON Processor"},
        }
        return {name: info["status"] for name, info in tools.items()}

    def _is_git_repo(self):
        """Проверка, находимся ли мы в Git-репозитории."""
        res = self._exec(
            ["git", "rev-parse", "--is-inside-work-tree"], silent=True
        )
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
        last_entry_start = content.rfind('### ')
        last_entry = content[last_entry_start:] if last_entry_start != -1 else ""

        self.history_file.rename(arch)
        header = f"# История {self.root.name}\nАрхив: {arch.name}\n\n"
        self.history_file.write_text(header + last_entry)
        print(f"🗄 История достигла лимита ({HISTORY_LIMIT} записей) и была архивирована. Контекст сохранен.")

    def init(self):
        """Инициализация проекта Context Keeper."""
        if not self.ck_path.exists():
            self.ck_path.mkdir(parents=True)

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

        gitignore = self.root / ".gitignore"
        ignore_entry = "\n# Context Keeper\n.ck/*.bak\n"
        if gitignore.exists():
            if ".ck/*.bak" not in gitignore.read_text():
                with open(gitignore, "a") as f:
                    f.write(ignore_entry)
        else:
            gitignore.write_text(ignore_entry)

        print(f"✅ Context Keeper v{VERSION} инициализирован.")

    def _get_current_task(self):
        """Получение текущей активной задачи из плана."""
        if not self.plan_file.exists():
            return "Нет активной задачи"
        content = self.plan_file.read_text()
        match = re.search(r"-\s*\[\s*\]\s*(.*)", content)
        if not match:
            match = re.search(r"-\s*\[\]\s*(.*)", content)
        return match.group(1).strip() if match else "Все задачи завершены"

    def _handle_git_sync(self, task, comment):
        """Обработка Git-синхронизации."""
        if not self._is_git_repo():
            if input("⁉️ Git не найден. Инициализировать? (y/N): ").lower() == "y":
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
                    input("🌐 Remote не найден. Создать на GitHub через gh? (y/N): ").lower()
                    == "y"
                ):
                    vis = (
                        "--public"
                        if input("🔓 Сделать публичным? (y/N): ").lower() == "y"
                        else "--private"
                    )
                    self._exec(
                        ["gh", "repo", "create", self.root.name, vis, "--source=.", "--push"]
                    )
                else:
                    self._remote_declined = True

        print("\nТип изменений:")
        print("1: feat  (новый функционал)\n2: fix   (исправление)\n3: chore (рутина)\n4: docs  (документация)")
        choice = input("Выберите (1-4, по умолчанию 1): ")
        prefix = {"2": "fix", "3": "chore", "4": "docs"}.get(choice, "feat")

        if input(f"🚀 Выполнить коммит '{prefix}: ...' и Push? (y/N): ").lower() == "y":
            commit_msg = f"{prefix}: [{task}] {comment}"
            self._exec(["git", "add", "."])
            if self._exec(["git", "commit", "-m", commit_msg]):
                print("📤 Отправка в облако...")
                self._exec(["git", "push"])
                print("✅ Синхронизация завершена.")

    def save(self):
        """Сохранение прогресса и Git-коммит."""
        if not self.state_file.exists():
            return print("❌ Сначала: ck init")

        task = self._get_current_task()
        print(f"📍 Задача: {task}")
        comment = input("📝 Описание: ").strip() or "update"

        log_content = ""
        if input("📎 Добавить тех-лог? (y/N): ").lower() == "y":
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

        print("💾 История сохранена.")
        self._handle_git_sync(task, comment)

        # ПРОВЕРКА ЛИМИТА ПО ЗАПИСЯМ (###) И РОТАЦИЯ
        if self.history_file.exists():
            if self.history_file.read_text().count('### ') > HISTORY_LIMIT:
                self._rotate()

    def status(self):
        """Отображение статуса проекта."""
        if not self.state_file.exists():
            return print("Проект не найден.")
        with open(self.state_file, "r") as f:
            s = json.load(f)

        print("\n" + "═" * 45)
        print(f" 🚀 ПРОЕКТ: {s['project_name']} [v{VERSION}]")
        print(f" 🎯 ТЕКУЩИЙ ШАГ: {self._get_current_task()}")

        if self._is_git_repo():
            br = self._exec(
                ["git", "branch", "--show-current"], silent=True
            )
            print(f" 🌿 ВЕТКА: {br or 'unknown'}")

        tools = [t for t, status in s.get("tools", {}).items() if status]
        if tools:
            print(f" 🛠  ИНСТРУМЕНТЫ: {', '.join(tools)}")
        print("═" * 45)

        recent = self._get_recent_history(count=3)
        if recent:
            print("\n 📜 ПОСЛЕДНИЕ ЗАПИСИ:")
            for entry in recent:
                lines = entry.splitlines()
                if not lines:
                    continue

                print(f"  {lines[0].replace('### ', '🔹 ')}")

                in_code_block = False
                for line in lines[1:]:
                    clean = line.strip()
                    if clean.startswith("```"):
                        in_code_block = not in_code_block
                        print(f"    {'┌' if in_code_block else '└'}{'─' * 40}")
                        continue

                    if in_code_block:
                        print(f"    │ {clean}")
                    elif clean:
                        print(f"    {clean}")
                print("")
        print("═" * 35 + " [Конец Статуса] " + "═" * 5 + "\n")


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
        ck.status()
    elif cmd == "edit":
        subprocess.run([ck._get_editor(), ck.plan_file])
    elif cmd == "log":
        subprocess.run([ck._get_editor(), ck.history_file])
    else:
        print("Неизвестная команда.")


if __name__ == "__main__":
    main()