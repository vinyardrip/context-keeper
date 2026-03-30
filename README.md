Here's the corrected `README.md` with proper Markdown formatting, fixed code blocks, tables, and consistent structure.

# Context Keeper (ck) 🧠

<div align="center">

**Minimalist Unix-way "external memory" for developers**
**Минималистичная «внешняя память» разработчика в стиле Unix**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8+-3776ab.svg)](https://www.python.org/)
[![Unix](https://img.shields.io/badge/unix-way-green.svg)](https://en.wikipedia.org/wiki/Unix_philosophy)

[English](#-english) · [Русский](#-русский)

</div>


<a name="english--eng"></a>
## 🇬🇧 English

CLI bridge between your brain, AI agents, and Git. Persistently saves the current task context and project history in plain Markdown files.

### Table of Contents
- [🧊 Concept](#-concept)
- [✨ Features](#-features)
- [📋 Requirements](#-requirements)
- [📦 Installation](#-installation)
- [📂 Project Structure](#-project-structure)
- [🛠 Commands](#-commands)
- [🔧 Core Mechanics](#-core-mechanics)
- [🗺 Roadmap](#-roadmap)

### 🧊 Concept

Context Keeper (`ck`) acts as a bridge between your brain, AI agents, and Git, persistently saving the current task context and project history in plain Markdown files.

### ✨ Features

- **Plain Text Storage**: All data stored in human-readable Markdown files
- **Git Integration**: Native support for `gh` and `glab` CLI tools
- **Auto-Archiving**: Automatic rotation of history files when reaching the limit
- **Strict Parsing**: Enforced formatting standards to prevent "dirty" markup
- **AI-Ready**: Optimized context preservation for AI agent workflows

### 📋 Requirements

- **Python 3.8+** (Required)
- **Git** (VCS integration)
- **fzf** (Fuzzy Finder) — for interactive menus
- **jq** (JSON Processor) — for state management
- **gh** or **glab** — for remote repository synchronization

### 📦 Installation

```bash
# Make executable
chmod +x ck

# Optional: Global installation
sudo cp ck /usr/local/bin/
# OR (coming soon)
ck --install
```

### 📂 Project Structure

Once initialized, the tool creates a `.ck/` directory in your root:

```
.ck/
├── state.json        # Metadata (project name, branch, tools status)
├── PLAN.md           # Active task list (Source of Truth)
├── HISTORY.md        # Chronological log of completed tasks
├── prompt.md         # AI System Instructions (Template for generating PLAN.md)
└── HISTORY_*.md.bak  # History archives (created automatically on rotation)
```

### 🛠 Commands

| Command       | Description |
|---------------|-------------|
| `ck init`     | Initialize `.ck/`, detect project name, and verify environment |
| `ck edit`     | Open `PLAN.md` in your `$EDITOR`. Mark the active task with `-[]` |
| `ck save`     | Interactive task completion workflow |
| `ck st`       | Status overview — current task and the last history entries |
| `ck log`      | View the full project history directly in the terminal/editor |
| `ck -h, --help` | Full command reference and manual |
| `ck -v, --version` | Show current utility version |

#### `ck save` Workflow

1. Select type (`feat`, `fix`, `chore`, `docs`) via `fzf`
2. Enter commit message + optional technical log (saved to history, but excluded from Git commit)
3. Choose to push via `gh`/`glab` or stay local

### 🔧 Core Mechanics

**Strict Parsing**
Active tasks must use `-[]` (no space). This prevents "dirty" formatting during manual edits and ensures precise regex detection.

**Auto-Archive**
When `HISTORY.md` reaches `HISTORY_LIMIT`, it rotates to a `.bak` file, preserving the last entry for context.

**VCS Integration**
If no remote exists, `ck` offers to create a repository on GitHub automatically (public or private).

### 🗺 Roadmap

- [ ] **Global Config**: Define project hubs (`~/projects/*`) and customize history limits/output count
- [ ] **Archive Management**: Set a limit for the number of `.bak` files to keep (auto-deletion of old archives)
- [ ] **Context Injection**: Automatically update `prompt.md` and templates into `.ck/` upon init
- [ ] **Self-installer**: `ck --install` command for global setup in `/usr/local/bin`
- [ ] **Snapshot Mode**: Create temporary recovery points for specific files before letting AI agents perform "risky" edits (developer's safety net)
- [ ] **Global Observer**: Unified dashboard aggregating status across all projects in workspace hubs
- [ ] **Update System**: `ck update` command to pull the latest version of the utility

---

<a name="русский--rus"></a>
## 🇷🇺 Русский

CLI-мост между вашим разумом, ИИ-агентами и Git. Сохраняет контекст разработки и историю проекта в простых Markdown-файлах.

### Оглавление
- [🧊 Концепция](#-концепция)
- [✨ Возможности](#-возможности)
- [📋 Требования](#-требования)
- [📦 Установка](#-установка)
- [📂 Структура проекта](#-структура-проекта)
- [🛠 Команды](#-команды)
- [Сценарий работы `ck save`](#сценарий-работы-ck-save)
- [🔧 Механика работы](#-механика-работы)
- [🗺 Roadmap](#-roadmap-1)

### 🧊 Концепция

Context Keeper (`ck`) служит мостом между вашим разумом, ИИ-агентами и Git, сохраняя контекст разработки и историю проекта в простых Markdown-файлах.

### ✨ Возможности

- **Хранение в тексте**: Все данные сохраняются в читаемых Markdown-файлах
- **Интеграция с Git**: Поддержка CLI-инструментов `gh` и `glab`
- **Авто-архивация**: Автоматическая ротация файлов истории при достижении лимита
- **Строгий парсинг**: Строгие стандарты форматирования для защиты от «грязной» разметки
- **Готовность к ИИ**: Оптимизировано для сохранения контекста при работе с AI-агентами

### 📋 Требования

- **Python 3.8+** (Обязательно)
- **Git** (VCS интеграция)
- **fzf** (Fuzzy Finder) — для интерактивных меню
- **jq** (JSON Processor) — для работы со стейтом
- **gh** или **glab** — для синхронизации с удаленными репозиториями

### 📦 Установка

```bash
# Сделать исполняемым
chmod +x ck

# Опционально: глобальная установка
sudo cp ck /usr/local/bin/
# ИЛИ (в разработке)
ck --install
```

### 📂 Структура проекта

После инициализации утилита создает директорию `.ck/` в корне проекта:

```
.ck/
├── state.json        # Метаданные (имя проекта, ветка, статус утилит)
├── PLAN.md           # Активный список задач (Source of Truth)
├── HISTORY.md        # Хронологический лог завершенных задач
├── prompt.md         # Инструкция для ИИ (Шаблон для генерации PLAN.md)
└── HISTORY_*.md.bak  # Архивы истории (создаются автоматически при ротации)
```

### 🛠 Команды

| Команда       | Описание |
|---------------|----------|
| `ck init`     | Инициализация `.ck/`, определение имени проекта и проверка окружения |
| `ck edit`     | Открыть `PLAN.md` в редакторе. Активная задача помечается как `-[]` |
| `ck save`     | Интерактивное завершение задачи |
| `ck st`       | Статус проекта — текущая задача и последние записи из истории |
| `ck log`      | Просмотр полной истории проекта прямо в терминале |
| `ck -h, --help` | Полная справка по командам и аргументам |
| `ck -v, --version` | Вывод текущей версии утилиты |

#### Сценарий работы `ck save`

1. Выбор типа (`feat`, `fix`, `chore`, `docs`) через `fzf`
2. Ввод сообщения коммита + опциональный тех-лог (уйдет в историю, но не попадет в Git-коммит)
3. Выбор push через `gh`/`glab` или оставить локально

### 🔧 Механика работы

**Строгий парсинг**
Активные задачи должны иметь формат `-[]` (без пробела). Это защита от «грязной» разметки при ручной правке.

**Авто-архивация**
При достижении лимита `HISTORY_LIMIT` файл истории переименовывается в `.bak`, а в новом файле оставляется последняя запись для сохранения контекста.

**Интеграция с VCS**
Если у проекта нет удаленного репозитория, `ck` предложит создать его на GitHub через `gh` (публичный или приватный).

### 🗺 Roadmap

- [ ] **Глобальный конфиг**: Настройка путей к хабам проектов (`~/projects/*`) и кастомизация лимитов вывода
- [ ] **Управление архивами**: Лимит на количество хранимых `.bak` файлов (авто-удаление старых копий)
- [ ] **Инъекция контекста**: Авто-обновление локального `prompt.md` и шаблонов в папке `.ck/` при инициализации
- [ ] **Установщик**: Команда `ck --install` для быстрой глобальной установки в `/usr/local/bin`
- [ ] **Snapshot Mode**: Создание временных точек восстановления файлов перед «рискованными» правками ИИ-агентов (страховка разработчика)
- [ ] **Global Observer**: Сводный дашборд по всем проектам во всех рабочих хабах
- [ ] **Обновление**: Команда `ck update` для подтягивания свежей версии утилиты
