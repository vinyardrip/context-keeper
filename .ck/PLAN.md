## Current Sprint
- [ ] Подготовка клиентского модуля синхронизации состояний (`.ck/state.json`) <!-- id: 93n9dufi -->
- [ ] Дополнительные шаблоны `prompt.md` для ролей ИИ <!-- id: vy7wwy8k -->
- [ ] Экспорт и инжекция контекста в сторонние инструменты <!-- id: xr6qnbid -->
- [ ] `ck diff`: вывод изменений с момента последнего сохранения <!-- id: 2cw1pdlk -->
- [ ] `ck undo`: отмена последнего `ck save` и откат коммита <!-- id: q33hr08y -->
- [ ] AI-summary (`ck suggest` / `ck summarize`) <!-- id: 723zq90d -->
- [ ] Interactive Plan: управление задачами прямо из TUI <!-- id: q6lvoww6 -->
- [ ] добавить notification для пространств todo(выставление таймера работа по крон - продумать) <!-- id: y1oafu5w -->
- [ ] добавить link между проектами(один хостинг - space remote), между пространством и проектами(space today задача общая, срочная починка уязвимости) может еще что <!-- id: b5wjzi4j -->
- [ ] сделать линкованные задачи(одинаковые для link projects/spaces) при связи - пишем в одном проекте, дописываются в связанные(хостингом, пример закрыть уязвимость, действия одинаковые, задачи тоже) <!-- id: 86829s6x -->

- [ ] Разобраться с семантикой focus vs start и «пропущенными» задачами в ck <!-- id: xt58xzfb -->
- [ ] BUG: ck add выводит глобальный ID (например, 106), а ck list — позиционный (63). Из-за этого команды delete/done не работают с ID из вывода add. Синхронизировать вывод ck add с реальной позицией в PLAN.md. <!-- id: drs124n0 -->
- [ ] Remove .ck_backup_* dirs after user verifies 12 migrated projects <!-- id: ucpibo83 -->
- [ ] BUG: _plan_display_path() prefers root PLAN.md over .ck/PLAN.md — ignored unless symlink (common: users wrote PLAN.md manually before installing ck) <!-- id: enwv6ea6 -->
- [ ] BUG: ck st shows the same task in both [>] Next and [~] Paused when it has a note (duplicate title) <!-- id: hz1h95a0 -->
- [ ] FEAT: ck <space> move/swap/reorder — task reordering inside spaces (parity with project commands) <!-- id: aq96ndsg -->
- [ ] Push backlog: 37 непушенных коммитов в 8 репо по ~/projects (grand-mebel 9 — сначала проверить содержимое); без remote: ck_test(6), cfs_next(5), avto-calculator(2) — решить, заводить ли remote <!-- id: 1xkwlvw4 -->
