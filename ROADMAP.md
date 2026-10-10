# ROADMAP / PLAN

Current version: v0.8.11

## Backlog
- [x] Инициализировать базовую структуру проекта (.ck/)
- [x] Реализовать объектную модель парсера задач (CommonMark + legacy `- []`)
- [x] Поддержка трех статусов: `- [ ]`, `- [x]`, `- [>]` (Focus)
- [x] Команды управления задачами: `ck add`, `ck start <ID>`, `ck done <ID|range|list>`
- [x] Триада отображения задач (`ck st`): Прошлое -> Текущее -> Будущее
- [x] Расчет прогресса (%) и счетчики задач (Всего/Done/Left)
- [x] Детектор пропусков ID (gaps)
- [x] Процессные заметки (`ck note <text>`) и блок `Unfocused / Paused Context`
- [x] Глобальный реестр проектов и дашборд (`ck dashboard`, `ck dashboard -v`, `ck prune`)
- [x] Атомарная запись и fail-closed блокировка файлов (`*.lock`)
- [x] Система ротации истории: порог `HISTORY_LIMIT`, сжатие архивов, FIFO-очистка
- [x] Сквозной просмотр истории: `ck log --all` с декомпрессией
- [x] Изолированный dev-режим (`ck-dev`), песочница и фикстуры
- [x] Интерактивный стресс-эмулятор ротации истории (`ck dev emulate`)
- [x] Утилиты установки и обновления (`ck install`, `ck update`, `ck uninstall`)
- [x] Доработать `install.sh` для установки одной командой через `curl | bash`

## Active
- [x] Автозамена дефолтной задачи при первом `ck add`
- [x] Кросс-проектные связи (`[sync: project_name]` и каскадное закрытие)
- [x] Непроектные пространства `local` и `remote`
- [x] Единая таблица `SPACES (GLOBAL CONTEXTS)` и динамический роутинг команд
- [x] Паритет команд, список, статус, реджект и управление пространствами (`ck space`)
- [x] Редизайн verbose-дашборда (`ck dashboard -v`) с UI-карточками
- [x] Динамическое обнаружение пространств в dev-режиме (`v0.7.1`)
- [x] Валидация удаления и единый UI таблиц/карточек (`v0.7.3`)
- [x] Безопасный UX удаления пространств (`v0.7.2`)
- [x] Зачистка эмодзи и единые ASCII-бейджи (`v0.7.4`)
- [x] Паритет таблиц SPACES и явный таргетинг `ck list <project>` (`v0.7.5`)
- [x] Стилизация бейджей, файловый фолбэк `ck list` и позиционные перестановки (`v0.8.0`)
- [x] Выравнивание колонок под ширину терминала и CJK-символы (`v0.8.1`)
- [x] Единый движок таблиц (`_render_standard_grid`) (`v0.8.2`)
- [x] Строгий визуальный паритет ширин таблиц (80 колонок) (`v0.8.3`)
- [x] Гарантированная ANSI-стилизация и переменная `NO_COLOR` (`v0.8.4`)
- [x] Разрешение имени проекта в имя каталога + акцент в таблицах (`v0.8.6`)
- [x] Полноэкранная подсветка строк уведомлений и magenta-имён (`v0.8.7`)
- [x] Единый движок уведомлений, баннеров и рамок (`ui.frame`) (`v0.8.8`)
- [x] Строгий таргетинг и честное сканирование вложенных проектов (`v0.8.9`)
- [x] Подробная обратная связь `ck reorder` и авто-рендер спринта (`v0.8.10`)
- [x] Seamless ck update mechanism: environment detection (symlink-resolved install path, `.git` vs standalone) — safe `git pull` for checkouts, in-place `install.sh` refresh preserving `.ck/` for standalone installs (`v0.8.11`)
- [x] Startup version check для standalone-установок: GitHub releases API + non-intrusive `ck update` notice, throttle 24h (`v0.8.11`)
- [ ] Global default spaces (local/remote): Automatic initialization of default global task spaces regardless of the current working directory
- [ ] Shell Completion: автодополнение команд, флагов, проектов и пространств в Bash / Zsh
- [ ] Подготовка клиентского модуля синхронизации состояний (`.ck/state.json`)
- [ ] Дополнительные шаблоны `prompt.md` для ролей ИИ
- [ ] Экспорт и инжекция контекста в сторонние инструменты
- [ ] `ck diff`: вывод изменений с момента последнего сохранения
- [ ] `ck undo`: отмена последнего `ck save` и откат коммита
- [ ] AI-summary (`ck suggest` / `ck summarize`)
- [ ] Interactive Plan: управление задачами прямо из TUI
- [ ] Базовое покрытие тестами через pytest (`cklib/core.py`, `cklib/history.py`)
