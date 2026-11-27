# Changelog

All notable changes to Context Keeper are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/) and the project
uses [Semantic Versioning](https://semver.org/).

## [0.14.3] - 2026-11-27

### Added

- Task deletion inside global spaces now accepts single IDs, numeric
  ranges (`2-4`) and lists, either space-separated (`1 3 5`) or
  comma-separated (`1,3,5`): `ck delete <spec> -s <space>` and the
  `ck rm` alias resolve every target first and refuse the whole
  operation if any ID is invalid or missing.
- Targets are deleted highest-ID-first, so positional re-indexing and
  the `<space>.HISTORY.md` note shifting cannot redirect a later
  deletion to the wrong task. The note sidecar is remapped after each
  removal, and the active project's `PLAN.md` / `.ck/state.json` stay
  untouched for cross-space deletions.
- Confirmation prompts, non-interactive refusals and result notices
  now name the target IDs/count and the target space (`project` for
  the active project). Whole-space removal remains the separate
  `ck space delete <name>` command.

### Tests

- `tests/test_delete.py`: space deletion coverage for ranges,
  comma/positional lists, re-indexing, note archiving and sidecar
  shifting, interactive and non-interactive messages, mixed invalid
  specs, and ranges containing missing IDs; the full `pytest` suite
  is green.

## [0.14.2] - 2026-11-27

### Fixed

- `ck space list` no longer injects the active project as an extra row:
  the view now renders only real spaces (built-ins, discovered space
  files and initialized journals), independent of the current
  directory. The project-specific footer (“highlighted project” /
  “No project in this directory”) is gone with the row.
- Legacy `spaces_order` data that still contains the `project` sentinel
  keeps loading: the renderer skips it and leaves the stored sequence
  byte-identical, while `ck space move` / `swap` / `reorder` / `rename`
  / `delete` continue to accept and preserve existing orders.

### Tests

- Regression coverage for `ck space list` inside and outside a
  project, with and without persisted `spaces_order` entries, plus the
  updated table/rendering and space-mutation expectations.

## [0.14.1] - 2026-11-27

### Added

- Task reordering inside global spaces and daily/period journals:
  `ck <space> move <ID> <pos|top|bottom>`, `ck <space> swap <ID1>
  <ID2>` and `ck <space> reorder <ID...>` now work for `local`,
  `remote`, custom spaces and the `today` / `week` / `month` / `year`
  journals. Operands accept positional IDs or stable task UUIDs.
- Reordering uses the shared `TaskList` slot engine: only active tasks
  move, completed `[x]` tasks are refused with exit 1, IDs re-index
  1..N, and stable UUIDs, inline notes and descriptions stay bound to
  their task. The `<space>.json` note sidecar is remapped to the
  post-reorder positions; writes remain atomic under the file lock.
- `reorder` prints the multi-line summary and then re-renders the
  refreshed space listing; `move` / `swap` keep a single-line
  confirmation. A daily journal rolls over lazily before the command,
  exactly like every other `ck today *` action.
- Bash/Zsh completion and help now advertise `move` / `swap` /
  `reorder` for spaces; README (EN+RU), ROADMAP and CHANGELOG are
  updated.

### Tests

- `tests/test_space_reorder.py`: unit and CLI coverage for non-daily
  and daily spaces, top/bottom targets, completed-task refusal,
  invalid/out-of-range IDs, note/UUID preservation and
  `ck today move 5 6`.

## [0.14.0] - 2026-11-25

### Added

- Stable task UUIDs as first-class addresses: `ck done`, `ck delete`,
  `ck start`, `ck focus`, `ck note`, `ck desc` and the space-routed
  `focus` / `start` / `note` variants accept the 8-character base36
  task UUID anywhere a positional ID works, including mixed specs
  (`a1b2c3d4,3-4`), ranges (`2-4`) and lists (`1,3,5`).
- Copy-paste-safe UUID suffixes in `ck list` / `ck st` / `ck notes`
  (`[#a1b2c3d4]`); legacy tasks without a UUID render without the
  suffix.
- TTY-only context banner in `ck list`:
  `[i] project: <name> — <path> (<N> open)`. Piped and redirected
  stdout stays byte-identical, so the `grep` / `cut` contract is
  unchanged; discovery and unknown-project branches print no banner.
- No-context `ck list` fallback: outside every project with no nested
  discovery matches, up to 10 registered projects are listed under
  `Registered projects:` (then a pointer to `ck dashboard`). Every
  printed name is addressable via `ck list <project_name>` from any
  directory.
- `ck done` mirrors completions into the project `ROADMAP.md`: rows
  marked `- [ ]` / `- [>]` / `- [~]` whose title exactly matches a
  completed task flip to `- [x]`. Best-effort — a missing file is a
  no-op and I/O errors never break the `done` result.

### Changed

- `ck delete` accepts full task specs (ranges, lists, UUIDs, mixed
  specs) with a single confirmation and deletes highest-ID-first,
  closing the sequential-reindex bug class.
- Package version unified to `0.14.0` across `pyproject.toml`,
  `cklib.config.VERSION`, `cklib.__version__`, README and ROADMAP.
- Storage schema remains the 0.13 split layout: 0.13.x projects are
  read and written without a migration.

### Fixed

- `ck add` / `ck done` / `ck list` documentation and help now describe
  spec-based addressing consistently (`<ID|spec>`).
- Repository hygiene: `*.egg-info/`, `uv.lock`, Python caches and
  virtualenv artifacts are ignored.

## [0.13.5] - 2026-11-16

### Added

- Multiline task descriptions (`ck desc <ID>` show / set / `--stdin` /
  `--clear`), stored inline in `PLAN.md` as indented `> text` lines.
- Task due dates (`--due YYYY-MM-DD`) with the `@due(...)` marker,
  overdue highlighting in `ck st`, and the quiet `ck notify --check`
  cron entry point (exit 0 / 1 / 2).

## [0.13.4] - 2026-11-16

### Added

- Period journals `week` / `month` / `year` beside `today`: dated
  headers, lazy period rollover, reserved names, and the same
  lifecycle guards as the daily journal.

## [0.13.3] - 2026-11-15

### Added

- `ck export` assembles PLAN.md + HISTORY.md + recent Git log into one
  Markdown document on stdout; `--task <ID>` narrows it to one task and
  `--with-errors` appends recent error logs.
