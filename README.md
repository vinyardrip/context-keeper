# Context Keeper (ck) 🧠
Minimalist Unix-way "external memory" for developers
Минималистичная «внешняя память» разработчика в стиле Unix

[![version](https://img.shields.io/badge/version-0.0.9-blue)]()
[![python](https://img.shields.io/badge/python-3.8%2B-blue)]()
[![platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS-lightgrey)]()
[![license](https://img.shields.io/badge/license-MIT-green)]()

---

**English · Русский**

---

# 🇬🇧 English

CLI bridge between your brain, AI agents, and Git. Persistently saves the current task context and project history in plain Markdown files.

## Table of Contents
- 🧊 Concept
- ✨ Features
- 📋 Requirements
- 📦 Installation
- 📂 Project Structure
- 🛠 Commands
- ck save Workflow
- 🔧 Core Mechanics
- 🗺 Roadmap

---

## 🧊 Concept
Context Keeper (ck) acts as a bridge between your brain, AI agents, and Git, persistently saving the current task context and project history in plain Markdown files. It follows the Unix philosophy: do one thing well, use plain text, and compose with other tools.

---

## ✨ Features
- Plain Text Storage: All data stored in human-readable Markdown files — no databases, no lock-in
- Git Integration: Native support for gh and glab CLI tools for remote repository management
- Auto-Archiving: Automatic rotation of history files when reaching the configurable HISTORY_LIMIT
- Strict Parsing: Enforced formatting standards (- [] / - [x]) to prevent "dirty" markup
- AI-Ready: Optimized context preservation for AI workflows
- Self-Contained: Templates embedded in script
- Self-Installer: ck install / ck uninstall
- Self-Updater: ck update
- CLI Task Management: ck add <text>
- Full Plan View: ck st --all

---

## 📋 Requirements
- Python 3.8+
- Git
- fzf
- jq
- gh or glab

---

## 📦 Installation

### Option 1 — One-liner (recommended)
```bash
curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/ck -o ck && chmod +x ck && sudo mv ck /usr/local/bin/ck
```

### Option 2 — Built-in installer
```bash
curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/ck -o ck
chmod +x ck
./ck install
```

### Verify
```bash
ck -v    # → ck version 0.0.9
```

---

## 📂 Project Structure
```bash
.ck/
├── state.json
├── PLAN.md
├── HISTORY.md
├── prompt.md
├── .gitignore
└── HISTORY_*.md.bak
```

---

## 🛠 Commands

| COMMAND | DESCRIPTION |
|--------|------------|
| ck init | Initialize project |
| ck edit | Open PLAN.md |
| ck save | Task completion workflow |
| ck st | Status overview |
| ck st --all | Full status |
| ck add | Add task |
| ck log | View history |
| ck install | Install |
| ck uninstall | Remove |
| ck update | Update |
| ck -h | Help |
| ck -v | Version |

---

## ck save Workflow
- Display current active task
- Enter commit description + optional log
- Select type (feat, fix, chore, docs, custom)
- Optional push
- Auto-archive when limit reached

---

## 🔧 Core Mechanics

### Strict Parsing
Active tasks must use:
```
- []
```

### Auto-Archive
When HISTORY.md reaches limit → rotates to .bak

### VCS Integration
Supports gh and glab

### Self-Contained Templates
All templates embedded in binary

---

## 🗺 Roadmap
-  **Core Analytics**: Progress tracking (%), task counters, and skip detection logic
- **Smart Rendering**: "Triad" view (Next/Current/Previous) with 120-char log truncation
- **Object Parsing**: Entity-based block processing (Task + Log + ID) instead of flat lines
- **Snapshot Mode**: Preventive state backups and data integrity checks ("safety first" mode)
- **Context Injection**: Mechanism to inject project context into external AI prompts and tools
- **Archive Management**: Advanced history control and search across HISTORY.md.bak files
- **Global Config & Observer**: Centralized settings (~/.ckrc) and cross-project dashboard
- **Interactive Workflow**: ID-based task completion and real-time plan updates during `ck save`
- **Instant Brain Dump**: `ck add <text>` with automatic `- []` formatting and positioning
- **CLI Improvements**: Bulk management (`ck done <range>`), and `ck st --all` view
- **Self-System**: Robust self-installer, uninstaller, and automatic update mechanism

---

<a name="русский--rus"></a>

# 🇷🇺 Русский

CLI-мост между вашим разумом, ИИ-агентами и Git.

---

## Оглавление
- 🧊 Концепция
- ✨ Возможности
- 📋 Требования
- 📦 Установка
- 📂 Структура проекта
- 🛠 Команды
- Сценарий работы ck save
- 🔧 Механика работы
- 🗺 Roadmap

---

## 🧊 Концепция
Context Keeper (ck) служит мостом между вашим разумом, ИИ-агентами и Git.

---

## ✨ Возможности
- Хранение в тексте
- Интеграция с Git
- Авто-архивация
- Строгий парсинг
- Поддержка AI

---

## 📋 Требования
- Python 3.8+
- Git
- fzf
- jq
- gh или glab

---

## 📦 Установка
```bash
curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/ck -o ck
chmod +x ck
./ck install
```

---

## 📂 Структура проекта
```bash
.ck/
├── state.json
├── PLAN.md
├── HISTORY.md
├── prompt.md
└── HISTORY_*.md.bak
```

---

## 🛠 Команды
(полный список аналогичен английской версии выше)

---

## Сценарий работы ck save
- Отображение задачи
- Ввод коммита
- Выбор типа
- Push
- Авто-архивация

---

## 🔧 Механика работы
- Строгий формат: `- []`
- Ротация истории
- Интеграция с Git

---

## 🗺 Roadmap
- **Аналитика и Статистика**: Расчет прогресса (%), счетчик задач и детектор пропусков
- **Умный вывод (Триада)**: Отображение (Будущее/Текущее/Прошлое) с обрезкой логов (120 симв.)
- **Объектный парсинг**: Обработка блоков (Задача + Лог + ID) вместо простых строк
- **Snapshot Mode**: Превентивные бэкапы состояния и проверка целостности («режим страховки»)
- **Context Injection**: Механизм внедрения контекста в сторонние инструменты и ИИ-запросы
- **Archive Management**: Расширенное управление архивами и поиск по HISTORY.md.bak
- **Global Config & Observer**: Централизованные настройки (~/.ckrc) и панель мониторинга проектов
- **Interactive Workflow**: Выбор ID выполненной задачи и правка плана прямо во время `ck save`
- **Мгновенный Brain Dump**: Команда `ck add <текст>` с авто-разметкой `- []` и позиционированием
- **CLI Improvements**: Массовое управление (`ck done <range>`) и режим просмотра `ck st --all`
- **Self-System**: Надежный установщик, деинсталлятор и система автоматического обновления
