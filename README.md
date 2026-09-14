# Context Keeper (ck) 🧠
Minimalist Unix-way "external memory" for developers
Минималистичная «внешняя память» разработчика в стиле Unix

[![version](https://img.shields.io/badge/version-0.2.3-blue)]()
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
- 🧪 Developer Environment & Isolated Sandbox
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
- Global Registry: Cross-project dashboard (ck dashboard) backed by `~/.config/ck/projects.json`; registration is explicit
- Fail-Closed Locking: Cross-process file locking guards registry, PLAN.md, HISTORY.md and state.json writes
- Full Plan View: ck st --all
- Dev Sandbox: `ck-dev` runs any command against real data read-only, redirecting all writes into a git-ignored `.sandbox/` (`ck-clean` resets it)

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
ck -v    # → ck version 0.2.3
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
| ck init | Initialize the project locally (.ck/ structure and gitignore rules); does not touch the global registry |
| ck init --register | Initialize locally and register the project in `~/.config/ck/projects.json` |
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
| ck st --global / ck dashboard | Cross-project dashboard (compact table: Project \| Focus Task \| Progress \| Last Active) |
| ck dashboard -v / --verbose | Detailed block view: full PREV/FOCUS/NEXT triad context per project |
| ck list -g / ck list --global | Global view: same as `ck dashboard` |
| ck register [-n NAME] [--path PATH] | Add a project to the global registry |
| ck unregister [--path PATH \| NAME] | Remove a project from the registry |
| ck prune | Purge registry entries whose folders no longer exist; reports each purged path and a summary count |
| ck info | Installation diagnostics (version, branch, paths) |
| ck install | Symlink ck to ~/.local/bin (no sudo) |
| ck uninstall | Remove the ~/.local/bin symlink |
| ck update | Self-update via git fetch + pull --ff-only (refuses if dirty) |
| ck-dev \<command\> | Run any command in isolated sandbox mode (writes → `.sandbox/`) |
| ck-clean / ck dev clean | Purge the `.sandbox/` dev environment |
| ck -h | Help |
| ck -v | Version |

### Initialization & Registration

`ck init` is strictly local by default. It creates the `.ck/` project
structure and updates the project `.gitignore`, but it does **not** create or
modify `~/.config/ck/projects.json`.

Use `ck init --register` when the project should be initialized and tracked in
the global registry in one step. An already initialized project can be added
to the registry at any time with the standalone command `ck register` (use
`--path PATH` and/or `-n NAME` when needed).

### Global Registry Hygiene

`ck list -g` and `ck dashboard` keep registered projects visible even when a
project directory has been deleted or moved. Such entries are labeled
`[MISSING]`, and the output ends with an actionable tip:

```text
💡 Found X missing project(s). Run 'ck prune' to cleanup.
```

Run `ck prune` to remove those orphaned entries from
`~/.config/ck/projects.json`. The command prints every purged project path
and a summary count, so the registry cleanup is auditable.

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

## <a name="developer-environment"></a>🧪 Developer Environment & Isolated Sandbox

`ck` ships with a Zero-Trust development mode: everything you do against real project data is **read-only**, and every write lands in a disposable sandbox. It is impossible for a dev session to corrupt production user data.

### Isolated Sandbox (`.sandbox/`)

`.sandbox/` is a git-ignored directory at the repository root. It safely stores dev-mode artifacts — sandboxed project copies, pseudo-configs, and debug logs — without dirtying the main working tree:

```bash
.sandbox/
├── projects/            # redirected per-project writes
│   └── <name>-<hash>/   # one sandbox dir per real project
│       └── .ck/         # PLAN.md, HISTORY.md, state.json, locks …
├── config/              # sandboxed global registry / state copies
└── dev.log              # debug log (see below)
```

- Created automatically on the first dev-mode write; never committed (`.gitignore`).
- Sandbox copies are **seeded from the real state** (e.g. appends to a sandboxed `HISTORY.md` start from the real history), so a dev session mirrors production without mutating it.

### Safe Development Binary (`ck-dev`)

`ck-dev` is the sandbox entrypoint. It activates `CK_SANDBOX=1` (the `--sandbox` flag and `CK_DEV=1` do the same) and then behaves like `ck`, with one crucial difference:

- **Reads are real**: dashboards, status, and task lists reflect your actual projects (`~/.config/context-keeper/projects.json`, real project folders).
- **Writes are intercepted**: ALL write operations — task mutations (`add` / `start` / `done`), `PLAN.md` edits, `state.json` updates, lock files, backups, registry changes — are redirected strictly into `.sandbox/`.

Production state stays immutable while `IS_DEV` is active: the real `~/.config/context-keeper/` and real `PLAN.md` files are never modified (verified byte-for-byte, including mtime, in the test suite). Two extra guardrails are enforced in dev mode:

- `ck-dev save` records the history entry into the sandbox but **skips the Git commit phase**.
- `ck-dev update` refuses to fetch/pull the installation.

```bash
./ck-dev add "experiment safely"     # writes land in .sandbox/
./ck-dev st                          # status reads the REAL project
```

### Cleanup Utility (`ck-clean` / `ck dev clean`)

`ck-clean` (or `ck dev clean`) safely and recursively purges `.sandbox/` to reset dev state. Missing directory → graceful no-op. Single-line confirmation on stdout; failures report to stderr with a non-zero exit code.

```bash
./ck-clean
# [ck-clean] Sandbox environment cleared successfully.
```

### Debug Logging (`CK_DEBUG=1` / `-v` / `--verbose`)

Dev-mode write interception can be traced:

```bash
CK_DEBUG=1 ./ck-dev add "traced"    # or: ./ck-dev -v add "traced"
# stderr:
# [DEBUG] Intercepted write -> .sandbox/projects/myproj-<hash>/.ck/PLAN.md (real path untouched: ...)
```

Logs go **exclusively** to `stderr` and `.sandbox/dev.log` — `stdout` stays completely clean so pipes, terminal UI, and scripts are never polluted. With debugging off, interception is fully silent.

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
- 🧪 Разработка и изолированная песочница
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
- Дев-режим с песочницей: `ck-dev` выполняет команды в режиме «только чтение» реальных данных, все записи уходят в git-ignored `.sandbox/` (`ck-clean` очищает её)

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

### Инициализация и регистрация

`ck init` по умолчанию работает строго локально: создаёт структуру `.ck/` и
обновляет `.gitignore` проекта, но не создаёт и не изменяет
`~/.config/ck/projects.json`.

Флаг `ck init --register` выполняет локальную инициализацию и одновременно
добавляет проект в глобальный реестр. Уже инициализированный проект можно в
любой момент добавить отдельно командой `ck register` (при необходимости с
флагами `--path PATH` и/или `-n NAME`).

### Гигиена глобального реестра

Команды `ck list -g` и `ck dashboard` не скрывают зарегистрированные проекты,
если их каталоги были удалены или перемещены. Такие записи помечаются тегом
`[MISSING]`, а в конце вывода появляется подсказка:

```text
💡 Found X missing project(s). Run 'ck prune' to cleanup.
```

Команда `ck prune` удаляет осиротевшие записи из
`~/.config/ck/projects.json`, печатает каждый удалённый путь и итоговое число
очищенных проектов.

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

## <a name="разработка-и-изолированная-песочница"></a>🧪 Разработка и изолированная песочница

В `ck` встроен режим разработки Zero-Trust: работа с реальными данными проекта ведётся **только на чтение**, а все записи попадают в одноразовую песочницу. Dev-сессия физически не может испортить пользовательские данные.

### Изолированная песочница (`.sandbox/`)

`.sandbox/` — игнорируемый Git каталог в корне репозитория. В нём безопасно хранятся артефакты dev-режима: песочные копии проектов, псевдо-конфиги и отладочные логи — рабочее дерево остаётся чистым:

```bash
.sandbox/
├── projects/            # перенаправленные записи проектов
│   └── <имя>-<hash>/    # своя песочница на каждый реальный проект
│       └── .ck/         # PLAN.md, HISTORY.md, state.json, lock-файлы …
├── config/              # песочные копии глобального реестра и state
└── dev.log              # отладочный лог
```

- Создаётся автоматически при первой dev-записи; в Git не попадает (`.gitignore`).
- Песочные копии **наследуют реальное состояние** (например, запись в песочный `HISTORY.md` начинается с реальной истории), поэтому dev-сессия отражает продакшен, не изменяя его.

### Безопасный бинарник разработки (`ck-dev`)

`ck-dev` — точка входа в песочницу. Он устанавливает `CK_SANDBOX=1` (аналогично работают флаг `--sandbox` и `CK_DEV=1`) и ведёт себя как `ck` с одним ключевым отличием:

- **Чтение реальное**: статус, дашборды и списки задач показывают настоящие проекты (`~/.config/context-keeper/projects.json`, реальные каталоги).
- **Записи перехватываются**: ВСЕ операции записи — мутации задач (`add` / `start` / `done`), правки `PLAN.md`, обновления `state.json`, lock-файлы, бэкапы, изменения реестра — строго перенаправляются в `.sandbox/`.

Пока активен `IS_DEV`, продакшен остаётся неизменным: реальный `~/.config/context-keeper/` и реальные `PLAN.md` не модифицируются (проверяется побайтно, включая mtime, в тестах). Дополнительные защитные барьеры в dev-режиме:

- `ck-dev save` сохраняет запись истории в песочницу, но **пропускает фазу Git-коммита**.
- `ck-dev update` отказывается от fetch/pull установки.

```bash
./ck-dev add "безопасный эксперимент"   # запись уйдёт в .sandbox/
./ck-dev st                             # статус читает РЕАЛЬНЫЙ проект
```

### Утилита очистки (`ck-clean` / `ck dev clean`)

`ck-clean` (или `ck dev clean`) безопасно и рекурсивно удаляет `.sandbox/`, сбрасывая dev-состояние. Отсутствующий каталог — корректный no-op. Подтверждение — одна строка в stdout; при ошибке — сообщение в stderr и ненулевой код возврата.

```bash
./ck-clean
# [ck-clean] Sandbox environment cleared successfully.
```

### Отладочное логирование (`CK_DEBUG=1` / `-v` / `--verbose`)

Перехват записей в dev-режиме можно трассировать:

```bash
CK_DEBUG=1 ./ck-dev add "traced"    # или: ./ck-dev -v add "traced"
# stderr:
# [DEBUG] Intercepted write -> .sandbox/projects/myproj-<hash>/.ck/PLAN.md (real path untouched: ...)
```

Логи пишутся **исключительно** в `stderr` и `.sandbox/dev.log` — `stdout` остаётся полностью чистым, поэтому пайпы, терминальный UI и скрипты не загрязняются. При выключенной отладке перехват полностью молчит.

---

## 🗺 Roadmap
- **Context Injection**: Механизм внедрения контекста в сторонние инструменты и ИИ-запросы
- **Archive Management**: Расширенное управление архивами и поиск по HISTORY.md.bak
- **Interactive Workflow**: Правка плана прямо во время `ck save`
