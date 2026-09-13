# Context Keeper (ck) 🧠
Minimalist Unix-way "external memory" for developers
Минималистичная «внешняя память» разработчика в стиле Unix

[![version](https://img.shields.io/badge/version-0.1.1-blue)]()
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
- Git Integration: Local commits only — `ck` never pushes, clones, or touches remotes without an explicit `ck update` (fast-forward pull)
- Auto-Archiving: Automatic rotation of history files when reaching the configurable HISTORY_LIMIT
- Strict Parsing: Task statuses `- [ ]` / `- [>]` / `- [x]` (legacy `- []` input is accepted and canonicalized on write)
- AI-Ready: Optimized context preservation for AI workflows
- Self-Contained: Templates embedded in the package
- Self-Installer: ck install / ck uninstall (user-level symlink, no sudo)
- Self-Updater: ck update (git fetch + `pull --ff-only`, refuses on dirty work tree)
- CLI Task Management: ck add <text>, ck start <ID>, ck done <ID|range|list>
- Global Registry: Cross-project dashboard (ck dashboard) backed by `~/.config/ck/projects.json`
- Fail-Closed Locking: Cross-process file locking guards registry, PLAN.md, HISTORY.md and state.json writes
- Full Plan View: ck st --all

---

## 📋 Requirements
- Python 3.8+
- Git (optional — ck save gracefully skips commits without it)
- fzf, jq (optional integrations)

---

## 📦 Installation

### Option 1 — Clone (recommended)
```bash
git clone https://github.com/vinyardrip/context-keeper.git
cd context-keeper
./ck init      # optional: try it out
./install.sh   # symlinks ck into ~/.local/bin (no sudo)
```

### Option 2 — Built-in installer
```bash
git clone https://github.com/vinyardrip/context-keeper.git
cd context-keeper
chmod +x ck
./ck install
```

> Note: `ck` is a thin wrapper over the `cklib/` package — install from the repository (or via `pip install .`), not as a standalone single file.

### Verify
```bash
ck -v    # → ck version 0.1.1
```

---

## 📂 Project Structure
```bash
.ck/
├── state.json
├── PLAN.md
├── HISTORY.md
├── prompt.md
├── README.md
├── .gitignore
└── HISTORY_*.md.bak
```

---

## 🛠 Commands

| COMMAND | DESCRIPTION |
|--------|------------|
| ck init | Initialize project (.ck/ structure, registry, gitignore rules) |
| ck add \<text\> | Add a new open task (inserted before `## Completed`) |
| ck start \<ID\> | Focus a task (`- [>]`) |
| ck done \<ID\|range\|list\> | Mark task(s) done (`- [x]`), e.g. `3`, `2-4`, `1,3,5` |
| ck edit | Open PLAN.md in your editor (see [Editor Resolution](#editor-resolution)) |
| ck save | Task completion workflow: optional note + optional **local** commit |
| ck log | Open HISTORY.md in your editor (see [Editor Resolution](#editor-resolution)) |
| ck st | Status overview (tri-state: Previous / Focus / Next) |
| ck st --all | Full status incl. full PLAN.md |
| ck tasks | Print the local project task list to STDOUT (no editor, pipe-friendly) |
| ck list | Local view: same as `ck tasks` |
| ck st --global / ck dashboard | Cross-project dashboard table |
| ck list -g / ck list --global | Global view: same as `ck dashboard` |
| ck register [-n NAME] [--path PATH] | Add a project to the global registry |
| ck unregister [--path PATH \| NAME] | Remove a project from the registry |
| ck prune | Drop registry entries whose folders no longer exist |
| ck info | Installation diagnostics (version, branch, paths) |
| ck install | Symlink ck to ~/.local/bin (no sudo) |
| ck uninstall | Remove the ~/.local/bin symlink |
| ck update | Self-update via git fetch + pull --ff-only (refuses if dirty) |
| ck -h | Help |
| ck -v | Version |

---

## ck save Workflow
- Display current active task
- Enter commit description + optional note (inline or via your editor — see [Editor Resolution](#editor-resolution))
- Optional **local** Git commit (never pushes; skips gracefully if Git is unavailable or declined)
- Auto-archive HISTORY.md when limit reached (gapless rotation, original preserved as .bak)

---

## <a name="editor-resolution"></a>⌨️ Editor Resolution

`ck edit`, `ck log`, and the `e=editor` note option in `ck save` launch an external editor. The editor is resolved by strict precedence — the first match wins:

1. **Project config** — the `"editor"` key in `.ck.json` at the project root (the directory containing `.ck/`)
2. **`$VISUAL`** environment variable
3. **`$EDITOR`** environment variable
4. **System fallback** — `nano` if installed, otherwise `vi`

### Configure per-project (highest priority)
Create `.ck.json` next to the `.ck/` directory:
```json
{
  "editor": "code --wait"
}
```
The value may include arguments (it is passed to the shell-launched editor command). A malformed or unreadable `.ck.json` is ignored gracefully.

### Configure globally
```bash
export VISUAL="vim"   # wins over EDITOR
export EDITOR="nano"  # used when VISUAL is unset
```

> `$VISUAL` takes precedence over `$EDITOR` because that is the classic Unix convention: `VISUAL` designates a full-screen editor, `EDITOR` a line editor fallback.

---

## 🔧 Core Mechanics

### Task Syntax
The parser accepts both canonical CommonMark and the legacy no-space form; the writer always emits canonical:

```
- [ ] open task      (canonical, accepted)
- [] open task       (legacy input — canonicalized to `- [ ]` on write)
- [>] focused task  (exactly one focus is enforced on write)
- [x] done task
```

### Auto-Archive
When HISTORY.md reaches limit → rotates to `HISTORY_*.md.bak` (rotation is crash-safe: the new content is staged before the old file is renamed).

### Data Integrity & Concurrency
- All writes are atomic (temp file + rename) — readers never see torn files
- Registry, PLAN.md, HISTORY.md and state.json mutations serialize on `*.lock` files via `fcntl.flock`
- Locking is **fail-closed**: if a lock cannot be acquired within 5s the operation aborts with a clear error instead of proceeding without protection
- A corrupt registry (`projects.json`) is never silently wiped: it is moved to a timestamped `.bak` before a fresh registry is created
- The renderer never overwrites non-task lines (headers/prose are preserved on every mutation)

### VCS Integration
Local commits only. `ck save` commits if you confirm; `ck` never pushes. `ck update` fast-forwards the installation itself only when the work tree is clean.

---

## 🗺 Roadmap
- **Context Injection**: Mechanism to inject project context into external AI prompts and tools
- **Archive Management**: Advanced history control and search across HISTORY.md.bak files
- **Interactive Workflow**: Real-time plan updates during `ck save`

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
- Интеграция с Git (только локальные коммиты, без push)
- Авто-архивация
- Парсинг `- [ ]` / `- [>]` / `- [x]` (устаревший `- []` нормализуется)
- Поддержка AI
- Глобальный реестр проектов и панель мониторинга
- Fail-closed блокировка файлов, атомарная запись

---

## 📋 Требования
- Python 3.8+
- Git (опционально — ck save корректно пропустит коммит без него)
- fzf, jq (опционально)

---

## 📦 Установка
```bash
git clone https://github.com/vinyardrip/context-keeper.git
cd context-keeper
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
├── README.md
├── .gitignore
└── HISTORY_*.md.bak
```

---

## 🛠 Команды
(полный список аналогичен английской версии выше)

---

## Сценарий работы ck save
- Отображение задачи
- Ввод коммита и опциональной заметки
- Локальный коммит (push не выполняется никогда)
- Авто-архивация

---

## ⌨️ Выбор редактора

`ck edit`, `ck log` и опция `e=editor` в `ck save` запускают внешний редактор, который определяется по строгому приоритету (побеждает первое совпадение):

1. **Конфиг проекта** — ключ `"editor"` в файле `.ck.json` в корне проекта (рядом с каталогом `.ck/`)
2. **`$VISUAL`** — переменная окружения
3. **`$EDITOR`** — переменная окружения
4. **Системный fallback** — `nano` (если установлен), иначе `vi`

Настройка для конкретного проекта (наивысший приоритет):
```json
// .ck.json
{ "editor": "code --wait" }
```

Глобальная настройка:
```bash
export VISUAL="vim"   # имеет приоритет над EDITOR
export EDITOR="nano"  # используется, если VISUAL не задан
```

---

## 🔧 Механика работы
- Формат задач: `- [ ]` / `- [>]` / `- [x]` (устаревший `- []` принимается и нормализуется)
- Ротация истории (безопасная при сбоях)
- Локальные коммиты Git (без push)
- Fail-closed блокировка файлов и атомарная запись

---

## 🗺 Roadmap
- **Context Injection**: Механизм внедрения контекста в сторонние инструменты и ИИ-запросы
- **Archive Management**: Расширенное управление архивами и поиск по HISTORY.md.bak
- **Interactive Workflow**: Правка плана прямо во время `ck save`
