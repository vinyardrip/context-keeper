# Context Keeper Development Plan & Roadmap

## Active / Pending Features
- [ ] Написать дополнительные шаблоны prompt.md для разных ролей ИИ
- [ ] Настроить механизмы экспорта/инжекции контекста в сторонние ИИ-инструменты

## Ideas & Discussion (Backlog v0.5+)
- [ ] `ck diff`: вывод изменений в контексте/плане с момента последнего сохранения
- [ ] `ck undo`: безопасная отмена последнего `ck save` и откат коммита
- [ ] AI-summary (`ck suggest` / `ck summarize`): генерация commit message / заметки через ИИ
- [ ] Interactive Plan: управление задачами в PLAN.md прямо из CLI / TUI
- [ ] Shell Completion: автодополнение команд в Bash / Zsh
- [ ] Unit Testing: базовое покрытие тестами через pytest (`cklib/core.py`, `cklib/history.py`)

## Completed
- [x] Инициализировать базовую структуру проекта (.ck/)[cite: 2]
- [x] Реализовать объектную модель парсера задач (CommonMark + legacy `- []`)
- [x] Поддержка трех статусов: `- [ ]`, `- [x]`, `- [>]` (Focus)
- [x] Команды управления задачами: `ck add`, `ck start <ID>`, `ck done <ID|range|list>`
- [x] Триада отображения задач (`ck st`): Прошлое -> Текущее -> Будущее
- [x] Расчет прогресса (%) и счетчики задач (Всего/Done/Left)
- [x] Детектор пропусков ID (gaps)
- [x] Процессные заметки (`ck note <text>`) и блок `Unfocused / Paused Context`
- [x] Глобальный реестр проектов и дашборд (`ck dashboard`, `ck dashboard -v`, `ck prune`)
- [x] Атомарная запись и fail-closed блокировка файлов (`*.lock`)
- [x] Система ротации истории: порог `HISTORY_LIMIT`, сжатие `.md.gz` (`COMPRESS_ARCHIVES`), FIFO-очистка (`MAX_BAK_FILES`)
- [x] Сквозной просмотр истории: `ck log --all` с прозрачной декомпрессией
- [x] Изолированный dev-режим (`ck-dev`), песочница (`.sandbox/`) и фикстуры (`ck dev setup`)
- [x] Интерактивный стресс-эмулятор ротации истории (`ck dev emulate`)
- [x] Утилиты установки и обновления (`ck install`, `ck update`, `ck uninstall`)
