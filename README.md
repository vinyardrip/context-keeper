# Context Keeper (ck) 🧠
Minimalist Unix-way "external memory" for developers
Минималистичная «внешняя память» разработчика в стиле Unix

[![version](https://img.shields.io/badge/version-0.8.21-blue)]()
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
- Auto-Archiving: Automatic rotation of history files when reaching the configurable HISTORY_LIMIT, with gzip-compressed `.md.gz` archives (COMPRESS_ARCHIVES) and FIFO retention via MAX_BAK_FILES
- Strict Parsing: Task statuses `- [ ]` / `- [>]` / `- [x]` (legacy `- []` input is accepted and canonicalized on write)
- AI-Ready: Optimized context preservation for AI workflows
- Self-Contained: Templates embedded in the package
- Self-Installer: ck install / ck uninstall (user-level physical copy — a regular executable file, never a symlink; no sudo)
- Self-Updater: ck update (git fetch + `pull --ff-only` for checkouts; in-place `install.sh` refresh for standalone installs, preserving `.ck/`; refuses on dirty work tree)
- CLI Task Management: ck add <text>, ck start <ID>, ck done <ID|range|list>
- Process Notes: `ck note <text>` attaches a scratchpad to the active task — shown in `ck st`, archived to HISTORY.md on `ck done`, and carried through focus switches via the Unfocused / Paused Context block
- Global Registry: Cross-project dashboard (ck dashboard) backed by `~/.config/ck/projects.json`; registration is explicit
- Fail-Closed Locking: Cross-process file locking guards registry, PLAN.md, HISTORY.md and state.json writes
- Full Plan View: ck st --all
- Dev Sandbox: `ck-dev` runs any command against real project data read-only, redirecting all writes into a git-ignored `.sandbox/` (`ck-clean` resets it); global spaces are strictly isolated under `.sandbox/config/spaces/`; `ck-dev sandbox setup` builds a disposable mock environment with bulk test data; entry keeps `ROADMAP.md -> .ck/PLAN.md` safely symlinked
- Shell Completion: `ck completion bash|zsh` for commands, flags, registered projects and global spaces (no filename fallback; honors an executable `CK_BIN`)

---

## 📋 Requirements
- Python 3.8+
- Git (optional — ck save gracefully skips commits without it)
- curl + tar (only for the one-line installer)
- fzf, jq (optional integrations)

---

## 📦 Installation

### One-line install (recommended)
```bash
curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/install.sh | bash
```
Downloads the `main` source as a **stream** straight into `~/.local/share/context-keeper` (no temporary `.tar.gz`, no cloned `.git`) and links `~/.local/bin/ck` at the launcher — no sudo. If `~/.local/bin` is not on your `$PATH`, the installer prints the exact command to add it (bash / zsh / `fish_add_path`). Re-running the installer updates the code in place and preserves your runtime data (`.ck/`).

### Developer Setup / Manual Installation
```bash
git clone https://github.com/vinyardrip/context-keeper.git
cd context-keeper
./ck init      # optional: try it out
./ck install   # physical copy to ~/.local/bin/ck + cklib snapshot
```

### Running in Dev Mode (`./ck-dev`)

Never install a dev build globally — run the checkout's wrapper directly:

```bash
./ck-dev                  # enter the interactive sandbox session (exit → type 'exit')
./ck-dev <command>        # run a single command in sandbox mode
./ck-dev sandbox setup    # build the disposable mock environment (.sandbox/)
./ck-clean                # purge .sandbox/ afterwards
```

On every entry the wrapper also keeps the project's internal roadmap safely linked — one canonical file, two well-known names, zero copies:

```bash
ln -sf "$(pwd)/ROADMAP.md" "$(pwd)/.ck/PLAN.md"
```

`ROADMAP.md` (repo root) is the real file; `.ck/PLAN.md` is a symlink to it. If only a real `.ck/PLAN.md` exists, it is promoted (renamed, content preserved) to `ROADMAP.md` once and the link is created back. Nothing is moved on session exit and a divergent real `.ck/PLAN.md` is never overwritten.

> Production isolation: `ck install` writes a **physical copy** of the launcher to `~/.local/bin/ck` (regular executable file, 0755 — never a symlink) and snapshots the `cklib/` package to `~/.local/share/ck/cklib`. The installed command is a static snapshot: editing this checkout does not change `~/.local/bin/ck` until you explicitly re-run `ck install` (or `ck update`).
> The one-line installer instead symlinks `~/.local/bin/ck` at the launcher under `~/.local/share/context-keeper` — `ck update` refreshes that tree in place (re-running `install.sh`, `.ck/` preserved), no manual re-installation needed.
> Note: `ck` is a thin wrapper over the `cklib/` package — install from the repository (or via `pip install .`), not as a standalone single file.

### Verify
```bash
ck -v    # → ck version 0.8.21
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
├── HISTORY_*.md.gz      (compressed rotation archives — default)
└── HISTORY_*.md.bak     (legacy uncompressed archives)
```

---

## 🛠 Commands

| COMMAND | DESCRIPTION |
|--------|------------|
| ck init | Initialize the project locally (.ck/ structure and gitignore rules); does not touch the global registry |
| ck init --register | Initialize locally and register the project in `~/.config/ck/projects.json` |
| ck add \<text\> | Add a new open task (inserted before `## Completed`). On a freshly initialized plan holding only the untouched `ck init` seed task (`Describe the first task` / `Описать первую задачу`), the seed is replaced in place instead of appending a second entry |
| ck move \<ID\> \<pos\|top\|bottom\> | Move a PENDING/ACTIVE task (`[ ]` / `[>]`) to a 1-based position among the active tasks, or to the first/last slot. The other active tasks shift to close the gap; titles, status markers and the focus are left untouched |
| ck swap \<ID1\> \<ID2\> | Exchange the positions of two active tasks |
| ck reorder \<ID1\> \<ID2\> ... | Set the relative order of the active tasks. Listed IDs move ahead of the unlisted ones, which keep their relative order — so `ck reorder 3 1` simply means "3 before 1". The confirmation is multi-line (applied IDs, then the resulting order with their post-reorder positions) and is followed by the auto-rendered sprint view — the exact `ck list` output for that project |
| _(reordering a completed task)_ | **Refused.** A completed (`[x]`) task is history and is never reordered: the entire operation aborts with exit code 1 and `[err] Cannot move completed task #<id>. Only pending/active tasks can be reordered.` |
| ck start \<ID\> | Focus a task (`- [>]`); `ck start 0` resets focus. A noted task that loses focus moves to Unfocused / Paused Context (note preserved until `ck done` archives it); a noteless one gets a soft attach-a-note hint |
| ck done \<ID\|range\|list\> | Mark task(s) done (`- [x]`), e.g. `3`, `2-4`, `1,3,5`. Bare `ck done` completes the CURRENT FOCUS (usage error without one); the completed task's process note is archived into HISTORY.md and cleared |
| ck delete \<ID\> [-s \<space\>] / ck rm \<ID\> | Permanently remove ONE task and re-index the remaining task IDs 1..N. The default target is the active project (`project`); `-s` / `--space <name>` targets a global space instead, mutating only that space file and leaving the project's `.ck/state.json` and `PLAN.md` untouched. Prints `Task #<ID>: <title>` and asks `Permanently delete task #<ID> from space '<space>'? [y/N]`; `-f` / `--force` skips the prompt — a non-interactive run without `-f` refuses (exit 2). Notes are archived to the target's `HISTORY.md`; a targeted space archives to `~/.config/ck/spaces/<space>.HISTORY.md`. Whole spaces are removed with the separate `ck space delete <name>` |
| ck note \<text\> | Attach/update a process note on the active task (shown in `ck st`; archived to HISTORY.md by `ck done`) |
| ck edit | Open PLAN.md in your editor (see [Editor Resolution](#editor-resolution)) |
| ck save | Checkpoint workflow (does **not** complete the task): append an optional note + short summary to HISTORY.md, then optionally create a **local** Git commit. Completion stays a separate `ck done <ID>` action |
| ck log | Open HISTORY.md in your editor (see [Editor Resolution](#editor-resolution)) |
| ck log --all | View the full history: every rotation archive (`.md.gz` decompressed / legacy `.md.bak`, oldest first) concatenated with the current HISTORY.md |
| ck st | Status overview (tri-state: Previous / Focus / Next); an explicit focus is duplicated at the top as `-> CURRENT FOCUS: [#<id>] <title>` (with its note). WORK CONTEXT shows `<< Done`, a conditional `[!] Skipped` (only on a real execution gap), `[>] Focus` / `Next`, `>> Upcoming`, and a highlighted bottom banner `>> Backlog: N task(s) remaining` |
| ck notes | List all active process notes: `[>] Active Focus:` plus `[!] Unfocused / Paused Context:` with each paused task's bound note (`[i] No active process notes found.` when none) |
| ck st -l / --list | Print the task list to STDOUT. Inside a project this is identical to `ck list`; outside one it follows the `ck st` hierarchy (global-registry fallback), whereas `ck list` deliberately never guesses — see [ck list Outside a Project](#ck-list-outside-a-project) |
| ck st -e / --edit | Open PLAN.md in your editor (same as `ck edit`) |
| ck st -g / --global | Cross-project dashboard (same as `ck dashboard`) |
| ck st --all | Full status incl. full PLAN.md |
| ck -l / --list | Top-level shorthand for `ck st -l` (list tasks) |
| ck -e / --edit | Top-level shorthand for `ck st -e` (edit plan) |
| ck -g / --global | Top-level shorthand for `ck st -g` (global dashboard) |
| ck list | Print the task list to STDOUT (pipe-friendly). Accepts an optional project name: `ck list <project_name>` lists that REGISTERED project from any directory (an unknown name exits 1 with `ERROR: Project '<name>' not found.`). With no argument outside every project it never guesses — one nested project is listed with its name in the header (`## Current Sprint (alpha)`), several (or none) produce the list of available nested projects plus the `ck list <project_name>` hint |
| ck dashboard | Cross-project dashboard (compact table: Project \| Focus Task \| Progress \| Last Active) |
| ck dashboard -v / --verbose | Detailed view: one card per project **and** per space, framed by thin `─` dividers (no side rails, so long titles never wrap), each carrying the progress line and the full `<< PREV` / `[>] FOCUS` / `>> NEXT` triad plus its process notes |
| ck register [-n NAME] [--path PATH] | Add a project to the global registry |
| ck unregister [--path PATH \| NAME] | Remove a project from the registry |
| ck prune | Purge registry entries whose folders no longer exist; reports each purged path and a summary count |
| ck space list / ls | Space management view: every discovered global space (dynamically scanned from `~/.config/ck/spaces/*.md`, including empty ones) plus the active project — the virtual `project` space, highlighted with bold accent on its name (no text tag), i.e. the context un-prefixed commands (`ck st`, `ck list`) resolve against. Rows follow the persisted `spaces_order` sequence (default: `project` first). Rendered as a thin box-drawing table (`┌─┬┐ │ ├─┼┤ └─┴┘`) with quiet hairline borders |
| ck space create \<name\> / new | Create a new space from the default template (`## Current Sprint` / `## Completed`); rejects unsafe, reserved (`list`/`create`/`rename`/`delete`/`project`/`local`/`remote`) and taken names |
| ck space rename \<old\> \<new\> / mv | Rename a space, carrying its `.json` note sidecar and `.HISTORY.md` archive along so no process notes or audit history are lost; a persisted position is carried over to the new name |
| ck space move \<name\> \<position\> | Move a space to a 1-based position in the persisted `spaces_order` sequence (the virtual `project` space is a valid target). The order is stored in `~/.config/ck/state.json` and drives both `ck space list` and the dashboard's `SPACES` table |
| ck space swap \<name1\> \<name2\> | Exchange the positions of two spaces (any two entries of the sequence, including `project`) |
| ck space reorder | Normalize/re-index the persisted sequence against the spaces that currently exist: stale names are dropped, new spaces appended, duplicates collapsed, and the `project` sentinel keeps its slot. Prints the numbered result |
| ck space delete \<name\> [-f] | Permanently delete a space **and** its note sidecar / `.HISTORY.md`, then drop it from the persisted order. The target is validated **first** (a missing space reports `ERROR:` with exit 1 immediately — no stdin read, no prompt; invalid input exits 2), and an existing space asks `Permanently delete …? [y/N]` on a terminal and refuses without confirmation when non-interactive; `-f` / `--force` (or `-y` / `--yes`) skips the prompt. The virtual `project` space and the built-in `local` / `remote` can never be deleted. No `rm` alias — the verb is spelled out, and task deletion stays the separate top-level `ck delete <ID>` |
| ck local add \<text\> | Add a task to the workstation space (`~/.config/ck/spaces/local.md`; `local`/`remote` are auto-initialized at startup — an existing space file is never overwritten) |
| ck local list | List workstation space tasks in the project `ck list` layout (`##` section headers, `[ ]`/`[>]`/`[x]` markers), with attached process notes rendered as `* Note:` lines under their tasks |
| ck local st / ck local status | Detailed workstation space status view — same layout as `ck st` (header bar, progress, CURRENT FOCUS with its process note, WORK CONTEXT: Done / Skipped / Focus / Next / Upcoming / Unfocused-Paused / Backlog) |
| ck local st -l / --list | Space task listing (synonym of `ck local list`) |
| ck local st -e / --edit | Open the space file in the editor (synonym of `ck local edit`) |
| ck local st -g / --global | Global dashboard (synonym of `ck local dashboard`) |
| ck local done \<ID\|range\|list\> | Mark workstation space task(s) done (`- [x]`); bare `ck local done` completes the space's CURRENT FOCUS (usage error without one) |
| ck local focus \<ID\> | Focus a workstation space task (`- [>]`), demoting any other focus; `ck local focus 0` resets focus |
| ck local start \<ID\> | Alias for `ck local focus <ID>` |
| ck local note \<ID> \<text\> | Attach a process note to a workstation space task (stored in `~/.config/ck/spaces/local.json`) |
| ck local notes | List every active process note of the space — the focused task's note under `[>] Active Focus:` and the notes bound to open, unfocused tasks under `[!] Unfocused / Paused Context:` (`[i] No active process notes found.` when none); same format as the project `ck notes` |
| ck local edit | Open the workstation space file (`~/.config/ck/spaces/local.md`) in the editor resolved by the standard logic (`.ck.json` `editor` key → `$VISUAL` → `$EDITOR` → nano/vi) |
| ck local dashboard | Alias for `ck dashboard`; `-v` adds the detailed PREV/FOCUS/NEXT blocks (spaces included) |
| ck remote add \<text\> | Add a task to the infrastructure space (`~/.config/ck/spaces/remote.md`) |
| ck remote list | List infrastructure space tasks in the project `ck list` layout, with attached process notes rendered under their tasks |
| ck remote st / ck remote status | Detailed infrastructure space status view (same layout as `ck st`, same `-l` / `-e` / `-g` flags) |
| ck remote done \<ID\|range\|list\> | Mark infrastructure space task(s) done (bare form completes the space's CURRENT FOCUS) |
| ck remote focus \<ID\> | Focus an infrastructure space task (`0` resets focus) |
| ck remote start \<ID\> | Alias for `ck remote focus <ID>` |
| ck remote note \<ID> \<text\> | Attach a process note to an infrastructure space task (stored in `~/.config/ck/spaces/remote.json`) |
| ck remote notes | List every active process note of the infrastructure space |
| ck remote edit | Open the infrastructure space file (`~/.config/ck/spaces/remote.md`) in the editor |
| ck remote dashboard | Alias for `ck dashboard` |
| ck \<space\> \<command\> | Dynamic space routing: ANY space file placed in `~/.config/ck/spaces/<name>.md` is automatically routable with the same commands (`add` / `list` / `st` / `done` / `focus` / `start` / `note` / `notes` / `edit` / `dashboard`), rendered uniformly in the `SPACES (GLOBAL CONTEXTS)` dashboard table |

> **Dev-mode path isolation.** While a sandbox session is active
> (`ck-dev` / `CK_SANDBOX=1` / `CK_DEV=1` / cwd under `.sandbox/`), every
> `~/.config/ck/spaces/...` path and the space order in
> `~/.config/ck/state.json` are replaced by `.sandbox/config/spaces/...` and
> `.sandbox/config/state.json`. Host spaces are never read, discovered or
> written; the sandbox starts with its own `local` / `remote` scaffolds.

The `ck dashboard` / `ck -g` output always opens with a unified
`SPACES (GLOBAL CONTEXTS)` table (`Space | Focus Task | Progress | Last
Active`) covering the `local` and `remote` spaces plus every custom
space file discovered in `~/.config/ck/spaces/`, rendered above the
Git projects table through the **same table engine** — there is exactly
one grid renderer in `ck`, and `GLOBAL DASHBOARD`, `SPACES (GLOBAL
CONTEXTS)` and `ck space list` all call it with one shared cell builder.

They render at **exactly the same width** — 80 columns, with the column
seams at identical offsets — because every column has a fixed width
(`name` 25, `focus` 17, `progress` 9, `last` 16) rather than a
content-derived one. The two tables therefore differ only in their title
and in the label of the first column, no matter what they contain. Each
row is two lines: `Space` name + contracted path, `[N] [>]` + task
title, `<done>/<total>` + `(<pct>%)`. Content wider than its column is
cut with a trailing `...` rather than widening the grid.

In `GLOBAL DASHBOARD` the first column is the project's **directory
name**, with the contracted path beneath it as a secondary line — a
label passed to `ck register -n` never replaces the folder name, so a
generic container entry such as `.../.sandbox/projects` cannot be
mistaken for a project called `projects`. Names render bold for visual
hierarchy; the active project in `ck space list` is bold cyan. With
`ck dashboard -v` every space additionally gets its own bordered card
(`MY SPACES`) with the progress line and the full `<< PREV` / `[>] FOCUS`
/ `>> NEXT` context triad plus its process notes — the same verbose
treatment registered projects get, and the compact table is not repeated
above the cards.
| ck completion bash\|zsh | Print the matching shell completion script to STDOUT: `eval "$(ck completion zsh)"`. Completions cover the core commands plus registered project names (`~/.config/ck/projects.json`) and space names for `ck space move|swap|rename|delete` (from the persisted `spaces_order`). `ck st <TAB>` / `ck list <TAB>` suggest projects; after the `st` project or a flag, completion switches to valid flags/options. Space completion keeps the sequence order and filters protected contexts (`project` for rename/delete, built-ins for delete) plus the first `swap` operand. A per-call `_ck_bin` resolver prefers an executable `CK_BIN` (a `ck-dev` session exports its local launcher) and falls back to the `ck` on `PATH`; top-level completion never falls back to filenames. A missing, empty or unreadable source simply yields no names (never a traceback) |
| ck info | Installation diagnostics (version, branch, paths, `launcher_path`) plus an accurate `install_mode`: `symlink (one-liner tree)`, `physical copy` after `ck install`, or `standalone/dev launcher` for a direct checkout |
| ck install | Copy ck to ~/.local/bin/ck (physical executable file, no symlink) + cklib snapshot to ~/.local/share/ck; no sudo |
| ck uninstall | Remove ~/.local/bin/ck, the cklib snapshot and any legacy ~/.local/bin/ck-dev |
| ck update | Self-update via git fetch + pull --ff-only (refuses if dirty); standalone installs refresh in place via `install.sh` (`.ck/` preserved) |
| ck-dev \<command\> | Run any command in isolated sandbox mode (writes → `.sandbox/`) |
| ck-dev (bare) | Enter sandbox mode: warning banner (dynamically sized, with the resolved active binary), Global Dashboard, then an interactive session subshell |
| ck dev setup | Build an isolated mock environment in `.sandbox/` (bulk test data) |
| ck dev emulate | Interactive history-rotation stress emulator (sandbox only): 3 generate → rotate cycles in `.sandbox/projects/sandbox_stress` with tiny limits, then prints how to inspect via `ck log --all` |
| ck dev clean / ck sandbox clean | Remove the `.sandbox/` directory |
| ck-dev sandbox setup | Build an isolated mock environment in `.sandbox/` (bulk test data) |
| ck-dev sandbox clean | Purge the `.sandbox/` dev environment |
| ck-clean / ck dev clean / ck sandbox clean | Purge the `.sandbox/` dev environment |
| ck -h | Help |
| ck -v | Version |

### Space Ordering

Space order is a **global** sequence — spaces exist outside any one project —
persisted in `~/.config/ck/state.json` under `spaces_order` (atomic write under
a fail-closed lock). It drives both `ck space list` and the dashboard's
`SPACES (GLOBAL CONTEXTS)` table, so the two can never disagree.

The sequence always contains the virtual **`project`** space: the active
project row ("the default directory-based space"), which un-prefixed commands
resolve against. It is listed first by default, can be moved/swapped like any
other entry, and can never be created, renamed or deleted.

```text
$ ck space move work 1
[ok] Moved space 'work' -> position 1

$ ck space swap local remote
[ok] Swapped spaces 'local' <-> 'remote'

$ ck space reorder
[ok] Normalized space order:
       1. work
       2. project
       3. remote
       4. local
       5. blog
```

`ck space reorder` reconciles the stored sequence with reality: names whose
files are gone are dropped, newly discovered spaces are appended, duplicates
collapse, and the `project` sentinel keeps its slot (or is restored to the
front if a hand-edited state file omitted it). Positions are 1-based and
include the `project` row. Invalid positions and missing spaces are clean
errors (exit 2 / exit 1 respectively) and never persist a partial order.

> **Task vs. space mutation.** `ck move` / `swap` / `reorder` reorder **tasks**
> inside the current `PLAN.md`; `ck space move` / `swap` / `reorder` reorder the
> **global spaces** and never touch a plan. Likewise `ck delete <ID>` deletes a
> task, while `ck space delete <name>` deletes a whole space and its notes.

### Shell Completion

`ck completion bash` / `ck completion zsh` print a completion script to
STDOUT — nothing is written to disk — so it can be loaded per session or
added to your shell config:

```bash
# Bash (~/.bashrc)
eval "$(ck completion bash)"

# Zsh (~/.zshrc, after `autoload -Uz compinit && compinit`)
eval "$(ck completion zsh)"
```

The scripts complete the core navigation commands and, most importantly, the
**registered project names**: `ck <TAB>`, `ck st <TAB>` and `ck list <TAB>`
suggest projects from `~/.config/ck/projects.json` immediately, with no second
lookup. `ck st` treats the project as its **first positional**: once a project
name or a flag is present, completion switches to valid flags/options instead
of repeating project names.

| Position | Suggests | Behaviour |
| --- | --- | --- |
| `ck <TAB>` | commands + registered project names | command/project completion only — never filenames from the cwd |
| `ck st <TAB>` | registered project names | the project is the only positional |
| `ck list <TAB>` | registered project names | same registry source as `ck st` |
| `ck unregister <TAB>` | registered project names | |
| `ck st alpha <TAB>` | `-l`, `--list`, `-e`, `--edit`, `-g`, `--global`, `--all` | after a project or flag, projects are not repeated |
| `ck st -l <TAB>` | valid `st` flags only | |
| `ck space <TAB>` | `list`, `create`, `rename`, `move`, `swap`, `reorder`, `delete` | |
| `ck space move\|swap\|rename\|delete <TAB>` | space names in `spaces_order` | `project` filtered from rename/delete, built-ins from delete, first `swap` operand excluded |
| `ck completion <TAB>` | `bash`, `zsh` | |
| `ck register --path <TAB>` | directories | the one intentional filesystem completion |

Space completion follows the **persisted `spaces_order`** from
`~/.config/ck/state.json`, so suggestions match `ck space list` exactly. The
virtual `project` row is surfaced for `move` / `swap` but filtered from
`rename` / `delete`; `delete` also hides the protected built-ins
(`local` / `remote`). A missing, empty or unreadable state file or registry
degrades to plain command completion — no traceback, no noise in the prompt.

Both scripts resolve the helper per TAB press through a small `_ck_bin`
function: a non-empty **executable `CK_BIN`** wins (a `ck-dev` session exports
the checkout's local launcher), otherwise the script falls back to the `ck`
found on `PATH`. A stale `CK_BIN` left by a removed checkout therefore
degrades to the installed binary instead of breaking completion; this protects
against `mise` or a re-exported `PATH` resolving a different `ck`. After
`ck update` re-installs the launcher, re-source the line (or open a new shell)
to pick up the latest script.

> **Fixture routing guard.** Paths under `.sandbox/` are disposable dev
> fixtures, so `ck register` rejects them in production (dev sessions still
> register them into the sandbox registry copy). Project completion also hides
> any `.sandbox/` entries left in an older registry, so a TAB press can never
> route `ck st` / `ck list` into mock data.

### Initialization & Registration

`ck init` is strictly local by default. It creates the `.ck/` project
structure and updates the project `.gitignore`, but it does **not** create or
modify `~/.config/ck/projects.json`.

Use `ck init --register` when the project should be initialized and tracked in
the global registry in one step. An already initialized project can be added
to the registry at any time with the standalone command `ck register` (use
`--path PATH` and/or `-n NAME` when needed).

Production registration validates the target: a path under any `.sandbox/`
tree is rejected with a clean error (exit 2) instead of being written to
`~/.config/ck/projects.json`. Dev sessions are exempt because their registry
writes land in the sandbox copy.

### Global Registry Hygiene

`ck dashboard` keeps registered projects visible even when a
project directory has been deleted or moved. Such entries are labeled
`[MISSING]`, and the output ends with an actionable tip:

```text
[i] Found X missing project(s). Run 'ck prune' to cleanup.
```

Run `ck prune` to remove those orphaned entries from
`~/.config/ck/projects.json`. The command prints every purged project path
and a summary count, so the registry cleanup is auditable.

### `ck list` Outside a Project

`ck list` never guesses. Run outside every project it does **not** silently
adopt the most recently active entry from the global registry — instead it
names what it found:

```text
$ cd ~/work            # holds registered projects alpha and beta
$ ck list
[!] Current directory is not a context-keeper project.

Available nested projects:
  - alpha
  - beta

To view tasks, navigate to a project directory or run: ck list <project_name>
```

A single nested project needs no choice, so its tasks are printed with the
project named in the section header:

```text
$ ck list
## Current Sprint (alpha)
[ ] 1. ship the migration
[>] 2. write the release notes
```

`ck list <project_name>` targets a registered project explicitly and works
from any directory; an unknown name is an error (exit code 1), never an empty
listing:

```text
$ ck list nope
ERROR: Project 'nope' not found.
```

When the registry knows about nothing nested under the current directory, `ck list`
also **scans the filesystem** (child and grandchild directories, depth ≤ 2) for
folders that physically carry a `PLAN.md`. Unregistered projects found that way are
listed *and* addressable by name, so the notice is always actionable — an empty
sandbox or a never-registered folder of projects still shows you what is there.
Hidden directories, dependency trees (`node_modules`, `venv`, …) and symlinked
directories are skipped.

### `ck st` Outside a Project

`ck st` follows the very same discovery rules — it never silently adopts the most
recently active registry entry either:

- exactly **one** project nested under the current directory → its status is
  rendered, annotated with `<- <path>` so you can see which project was picked;
- **several** nested projects → the same choice notice, listing every option and
  pointing at `ck st <project_name>` / `ck list <project_name>`;
- **none** → the standard global fallback (most recently active registered
  project) or the explicit `No active project found` state — a clean exit, never
  a traceback.

`ck st <project_name>` targets a project explicitly from any directory, exactly
like `ck list <project_name>`; an unknown name exits 1 with
`ERROR: Project '<name>' not found.` The view flags then apply to the *named*
project: `ck st alpha -l` lists alpha's tasks, `ck st alpha --all` prints alpha's
status plus its full `PLAN.md`. `ck st -g` is the cross-project dashboard, so it
takes no project name (pairing them is a usage error).

`ck st --all` only ever prints the plan of the project the status block above it
actually shows — an ambiguous directory never pairs the discovery notice with
some other project's plan.

---

## Task Reordering

Reordering reshuffles **pending work** — the `[ ]` and `[>]` tasks — and nothing
else. Titles, status markers, headers, prose and the `## Completed` block are all
preserved; the focus marker travels with its task.

```text
$ ck list
## Current Sprint
[ ] 1. write the migration
[ ] 2. review the PR
[>] 3. ship the release
```

```text
$ ck move 3 1
[ok] Moved task #3 -> position 1

$ ck list
## Current Sprint
[>] 1. ship the release
[ ] 2. write the migration
[ ] 3. review the PR
```

Task IDs are **positional** (assigned 1..N in document order), so the number beside
a task changes when it moves — `#3` above became `#1`. The confirmation line always
echoes the ID you typed and the position it landed in.

`top` and `bottom` are accepted as aliases for the first and last active slot:

```text
$ ck move 1 bottom
[ok] Moved task #1 -> position 3
```

Swap two tasks' positions:

```text
$ ck swap 1 2
[ok] Swapped task #1 <-> task #2
```

Or set a whole relative order at once. Listed IDs move ahead of the unlisted ones,
which keep their relative order — so you never have to retype the entire list:

```text
$ ck reorder 2 1
[ok] Reordered active tasks: 2, 1
     New order:
       1. write the migration
       2. review the PR
       3. ship the release

## Current Sprint
[ ] 1. write the migration
[ ] 2. review the PR
[>] 3. ship the release
```

The confirmation is deliberately multi-line: the first line echoes the IDs you
applied, the indented block underneath is the order now stored in `PLAN.md`.
IDs are **positional**, so they are re-numbered after the move — the summary
shows the IDs as they exist *after* the reorder.

Because the file just changed, `ck reorder` immediately re-renders the sprint
view (byte-for-byte the `ck list` output for that project) so you see the result
without running a second command. `ck move` and `ck swap` keep their
single-line confirmation and do **not** auto-render.

### Completed tasks are never reordered

A completed (`[x]`) task records history, and its position is part of that record.
Targeting one aborts the **entire** operation — the file is left byte-identical,
so a command can never half-apply:

```text
$ ck move 4 1
[err] Cannot move completed task #4. Only pending/active tasks can be reordered.

$ echo $?
1
```

Exit codes: `0` success · `1` attempted to reorder a completed task · `2` usage
error (unknown task ID, out-of-range position, non-numeric ID, missing argument).

---

## Task Deletion

`ck delete <ID>` (alias: `ck rm <ID>`) permanently removes one task from the
active project's `PLAN.md` by default. Headers, prose and every other task are
preserved.

```text
$ ck delete 2
Task #2: review the PR
Permanently delete task #2 from space 'project'? [y/N]: y
[ok] Deleted task #2: review the PR from space 'project' (2 task(s) remaining, IDs re-indexed).
```

Use `-s` / `--space <name>` to target a global space explicitly:

```text
$ ck delete 3 --space work -f
Task #3: rotate the API token
[ok] Deleted task #3: rotate the API token from space 'work' (4 task(s) remaining, IDs re-indexed).
[info] Archived 1 process note(s) to HISTORY.md of space 'work'.
```

A cross-space deletion is isolated: it rewrites only
`~/.config/ck/spaces/<name>.md`, its `.json` note sidecar and its
`<name>.HISTORY.md`. The active project's `PLAN.md` and `.ck/state.json` are
never modified or switched. With `-s` omitted the command defaults
strictly to the active `project` context; it never guesses a space from the
working directory.

Task IDs are **positional**, so deleting one closes the numeric gap
immediately: the remaining tasks are renumbered 1..N in document order — no
broken sequences, no duplicate IDs. Process notes bound to the deleted task
(the active note or a paused one) are archived into the target's `HISTORY.md`
instead of being dropped, and the note IDs in the target's state shift with
the re-index so remaining notes stay bound to the right tasks.

- `-f` / `--force` skips the confirmation prompt for automation.
- Without `-f`, a non-interactive session (piped stdin/stdout) **refuses**
rather than deleting blind (exit 2).
- Task IDs are numbers: `ck delete work` is a clean error that points at
`ck space delete work` instead of guessing.
- An unknown `--space` target is a clean not-found error (exit 1); unsafe
names are rejected as usage errors (exit 2).

Exit codes: `0` deleted · `1` space not found · `2` aborted, unknown/invalid
ID, invalid space name, missing argument.

> **Task vs. space deletion.** `ck delete <ID> [-s <space>]` removes one
> task; it never removes a global space. Deleting a whole space and its note
> sidecar is the deliberately spelled-out `ck space delete <name> [-f]`
> command (no short alias).

---

## ck save Workflow
`ck save` records a **checkpoint**; it does not complete the active task.
Task completion remains the explicit `ck done <ID>` action (which also
archives that task's note into HISTORY.md).

1. Show the current active task and state plainly that it stays open.
2. **Step 1 — local history (no Git):** enter an optional note body (inline
   or via your editor), then a short summary; a dated entry is appended to
   `.ck/HISTORY.md`.
3. **Step 2 — optional local Git commit:** choose a commit message and
   confirm. Only the Context Keeper artifacts are staged; `ck` never pushes.
   If Git is unavailable or you decline, the history entry stays saved.
4. Auto-archive HISTORY.md when the limit is reached (gapless rotation,
   original preserved as a compressed `.md.gz` archive by default).

---

## Process Notes & Unfocused / Paused Context

A **process note** (`ck note <text>`) is a lightweight scratchpad attached to the
active task. It lives in `.ck/state.json`, renders under the task it belongs to,
and follows a strict lifecycle:

- **Attach / update** — `ck note <text>` writes the note onto the active task
  (focused task, else first open task).
- **Display** — `ck st` shows `* Note: <text>` under the task that owns it, and
  `ck dashboard -v` shows it per project (including for registered projects
  viewed from another directory).
- **Complete** — `ck done <ID>` archives the note into `HISTORY.md` as a dated
  entry, then clears it from the active-task state. The note is user data: it
  graduates into history, it is never silently dropped.

### Switching focus

When focus moves away from a task (via `ck start <NEW_ID>` or a focus reset with
`ck start 0`), the task that lost focus is handled according to whether it
carries a note:

- **With a note** — the task moves to a dedicated `Unfocused / Paused Context`
  block in `ck st` (and `ck dashboard -v`). Its note stays attached and travels
  with it, so the scratchpad is never orphaned under the new focus:

  ```text
  -> WORK CONTEXT:
     << Done:
        - [1] setup repo [x]
     [>] Focus:
        - [3] write docs
     >> Upcoming: (none)
     >> Backlog: (none)
     Unfocused / Paused Context:
        - [2] implement API mapping
          * Note: paused mid-refactor — mapping layer half done
  ```

  The switch itself reports the archival:

  ```text
  -> Focused [3]: write docs
  [i] Task [2] implement API mapping lost focus — moved to Unfocused / Paused Context (note preserved; archived by `ck done`).
  ```

- **Without a note** — before the switch completes, an interactive TTY session
  offers to capture the context first:

  ```text
  $ ck start 3
  Task #2 lost focus. Add a process note? [y/N]: y
  Note text: paused mid-refactor — mapping layer half done
  * Note saved for [2]: paused mid-refactor — mapping layer half done
  -> Focused [3]: write docs
  ```

  Answering `N` (or just pressing Enter) proceeds with the switch and emits the
  soft hint instead:

  ```text
  -> Focused [3]: write docs
  [!] Task #2 lost focus without a note. Attach one via `ck note <text>`.
  ```

  Non-interactive sessions (CI, pipes, scripts — stdin or stdout not a TTY)
  never prompt and always get the soft hint. Flags: `--no-input` skips the
  prompt unconditionally; `-y` / `--yes` assumes "yes" and asks only for the
  note text.

  (Switching back with `ck start 2` resumes the task and restores its note;
  completing it with `ck done 2` archives the note into HISTORY.md and clears
  it.)

- **Idempotency** — `ck start <ID>` on the already-focused task is a clean
  no-op: it prints `Task #<ID> is already focused.`, never prompts for a note,
  and does not re-assign focus.

Every open task that previously held focus — with or without a note — stays
listed under **Unfocused / Paused Context** in `ck st` and `ck dashboard -v`
until it is re-focused or completed; paused tasks are excluded from the generic
`[!] Skipped` list so they are never buried there. A paused task's note stays
bound to it across further focus switches and is restored as the active note
when the task is re-focused.

### Status layout (`ck st`)

`ck st` renders a strict, PURE-ASCII WORK CONTEXT block. Sections appear in
this order:

1. **`<< Done`** — the completed tasks closest to the progress point (last 2
   shown, with a `(N tasks)` count and `... (+N more done)` overflow when more).
2. **`[!] Skipped`** — shown **only** when an explicit focus sits after the
   last completed task and open tasks were passed over *between* that
   completion and the focus (a literal gap in execution order). Sequential
   upcoming tasks are **never** labelled Skipped, and the section is omitted
   entirely when there is no real execution gap.
3. **`[>] Focus`** (explicit focus via `ck start`) or **`[>] Next`** (the
   auto-resolved first pending task — display only, `PLAN.md` is never
   mutated).
4. **`>> Upcoming`** — exactly one next pending task after Focus/Next.
5. **`Unfocused / Paused Context`** — still-open tasks that lost focus while
   carrying a process note (rendered last, with the note attached).
6. **`>> Backlog`** — the highlighted BOTTOM BANNER of the block: a single
   count line (`N task(s) remaining`, singular when `N = 1`) for the TOTAL
   remaining uncompleted tasks, i.e. `Total − Done`. It is deliberately NOT
   reduced by the tasks shown in Focus/Next, Upcoming, Skipped, or the
   Unfocused / Paused Context block — those sections name individual tasks,
   the banner counts the overall remaining workload. Rendered with an
   orange/amber background highlight (inverted, bold black-on-amber) so it
   visually pops as the last line of WORK CONTEXT.

When no task is focused but pending work remains, `ck st` prints a read-only
hint banner — `[!] No active focus set. Run 'ck start <ID>'` — and points at
`[>] Next`. It never auto-focuses or writes to `PLAN.md`.

### Viewing notes: `ck notes`

`ck notes` lists every active process note in the project in two sections —
the focused task's note and each paused task's bound note (paused entries
without a note are shown with a `(no note)` marker so the paused context stays
fully visible):

```text
$ ck notes
[>] Active Focus:
   - [3] write docs
[!] Unfocused / Paused Context:
   - [2] implement API mapping
     * Note: paused mid-refactor — mapping layer half done
   - [4] refactor parser
     (no note)
```

When nothing carries a note: `[i] No active process notes found.`

---

## Colors

Status badges are colored **by default** and keep their color when the output is
piped or redirected to a file: `[!]` bold yellow, `[i]` cyan, `[ok]` bold green,
`[err]` and `ERROR:` bold red.

A notice colors its **entire line**: the badge and the sentence that follows are
one styled run, closed by a single reset at the end of the line.

```text
[!] PLAN.md not found.          <- whole line bold yellow, not just "[!]"
[i] Run 'ck init' in a project. <- whole line cyan
```

This is the same engine for root-level commands and for the hints printed under
the dashboard tables, so a notice looks identical wherever it appears. Because
styling occupies zero terminal columns, coloring the full line never reflows the
table above it.

Every message color comes from one table, so the palette cannot drift between a
badge and a banner:

| Role | Color | Used by |
|---|---|---|
| `warn` | bold yellow `\033[1;33m` | `[!]` badges, the `[!] SANDBOX MODE ACTIVE` banner, focus hints |
| `info` | cyan `\033[36m` | `[i]` badges and hints |
| `ok` | bold green `\033[1;32m` | `[ok]` badges, success confirmations |
| `err` | bold red `\033[1;31m` | `[err]` badges, `ERROR:`, hard failures |
| project | bold magenta `\033[1;35m` | project names in `GLOBAL DASHBOARD` |

Each is emitted as a **single combined SGR run**, never as a split `1m` + `33m`
pair — a split pair can leave the trailing text inheriting bold-but-uncolored
state on some terminals.

Project names use bold magenta precisely so they can never be mistaken for a cyan
`[i]` hint. Space names in `SPACES` keep their own styling, and the active project
in `ck space list` is accented without a literal `[PROJECT]` tag.

The sandbox banner is drawn by the same framing primitive as every other banner:

```text
┌─────────────────────────────────────────────────────┐
│ [!] SANDBOX MODE ACTIVE                             │
│ Active binary: /home/user/.local/bin/ck             │
│ Type 'exit' or press Ctrl+D to return to production │
└─────────────────────────────────────────────────────┘
```

The frame is measured in terminal **columns** on the plain text and styled
afterwards, so a long path widens the box instead of wrapping, and no escape
sequence can ever shift a border.

`NO_COLOR` is the only opt-out:

```bash
NO_COLOR=1 ck st          # plain text, no escapes
ck dashboard > log.txt    # badges stay colored inside the file
```

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
When `HISTORY.md` reaches `HISTORY_LIMIT` entries it is rotated into a timestamped archive and a fresh file is started. Rotation is crash-safe: the new content is staged before the old file is renamed.

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `HISTORY_LIMIT` | `1000` | Entry count in `HISTORY.md` that triggers rotation |
| `MAX_BAK_FILES` | `100` | Maximum retained archives; the oldest are purged FIFO after each rotation |
| `COMPRESS_ARCHIVES` | `True` | Rotate into gzip-compressed `HISTORY_<date>_<time>.md.gz` (Python's built-in `gzip` module — fully cross-platform, no external binaries) instead of legacy `HISTORY_<date>_<time>.md.bak` |

`ck log --all` shows the combined history: every archive (`.md.gz` transparently decompressed and legacy `.md.bak`, oldest first) followed by the current `HISTORY.md`. A corrupted archive is skipped with a warning instead of breaking the view.

### Data Integrity & Concurrency
- All writes are atomic (temp file + rename) — readers never see torn files
- Registry, PLAN.md, HISTORY.md and state.json mutations serialize on `*.lock` files via `fcntl.flock`
- Locking is **fail-closed**: if a lock cannot be acquired within 5s the operation aborts with a clear error instead of proceeding without protection
- A corrupt registry (`projects.json`) is never silently wiped: it is moved to a timestamped `.bak` before a fresh registry is created
- The renderer never overwrites non-task lines (headers/prose are preserved on every mutation)

### VCS Integration
Local commits only. `ck save` commits if you confirm; `ck` never pushes. `ck update` fast-forwards the installation itself only when the work tree is clean, then refreshes an installed copy (`~/.local/bin/ck`) so production stays in sync with the checkout. Standalone (one-line installer) deployments have no `.git` metadata: there `ck update` re-runs the remote installer in place (`curl | tar`), preserving `.ck/`. Standalone installs also get a throttled startup version check against the GitHub releases API — a single stderr line prompting `ck update` when a newer release exists (opt out: `CK_DISABLE_UPDATE_CHECK=1`).

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
├── config/              # sandboxed global registry / state / spaces
│   └── spaces/          # local/, remote/, custom spaces (isolated)
└── dev.log              # debug log (see below)
```

- Created automatically on the first dev-mode write; never committed (`.gitignore`).
- Project sandbox copies are **seeded from the real state** (e.g. appends to a sandboxed `HISTORY.md` start from the real history), so a dev session mirrors production without mutating it.
- **Global spaces are the exception: they are strictly isolated.** `~/.config/ck/spaces/` is replaced by `.sandbox/config/spaces/` for the whole session, so host spaces are never read or discovered; the sandbox seeds its own empty `local` / `remote` scaffolds.

### Safe Development Binary (`ck-dev`)

`ck-dev` is the sandbox entrypoint. It activates `CK_SANDBOX=1` (the `--sandbox` flag and `CK_DEV=1` do the same) and then behaves like `ck`, with one crucial difference:

- **Reads are real for projects** (with sandbox read-your-writes): dashboards, status, and task lists reflect your actual projects — unless a project sandbox copy exists (seeded by a previous dev-mode write or `sandbox setup`), in which case that copy is read so chained dev commands compose. **Global spaces read only the sandbox copy** — production spaces never leak into the session.
- **Writes are intercepted**: ALL write operations — task mutations (`add` / `start` / `done`), `PLAN.md` edits, `state.json` updates, lock files, backups, spaces (`add` / `focus` / `done` / `note` / `space create|rename|delete`), registry changes — are redirected strictly into `.sandbox/`.

Bare `ck-dev` enters an interactive **session subshell**. On entry the warning banner reports the **active binary**, resolved dynamically at runtime (`command -v ck` semantics — never hardcoded), and after you leave by typing `exit` (or pressing Ctrl+D) the exit handler reports the global binary that takes over:

```text
┌──────────────────────────────────────────────────────────────┐
│ [!] SANDBOX MODE ACTIVE                                      │
│ Active binary: /home/you/projects/context-keeper/ck          │
│ Type 'exit' or press Ctrl+D to return to production          │
└──────────────────────────────────────────────────────────────┘
...
[ok] Exited sandbox mode. Active binary is now: /home/you/.local/bin/ck
```

The banner width is **dynamic** — it expands or contracts to fit the active binary path without wrapping or breaking the borders.

The sandbox path is pinned to the **front of `PATH`** for the session (and self-healed on every resolution inside the session shell), so `command -v ck` inside the sandbox returns the checkout's local dev binary even when a globally installed `ck` or a shell rc re-export would otherwise shadow it.

A top-level `exit` argument (e.g. `./ck-dev exit`) is a **safeguard**, not an exit: a subshell session is a real shell process that only your typed `exit` / Ctrl+D can close, so the wrapper refuses with exit code 1 and no new subshell:

```text
$ ./ck-dev exit
[!] 'ck-dev exit' cannot close an active subshell.
[!] To exit sandbox mode, type 'exit' or press Ctrl+D.
```

Production state stays immutable while `IS_DEV` is active: the real `~/.config/context-keeper/` and real `PLAN.md` files are never modified (verified byte-for-byte, including mtime, in the test suite). Two extra guardrails are enforced in dev mode:

- `ck-dev save` records the history entry into the sandbox but **skips the Git commit phase**.
- `ck-dev update` refuses to fetch/pull the installation.

```bash
./ck-dev add "experiment safely"     # writes land in .sandbox/
./ck-dev st                          # status reads the REAL project
```

### Cleanup Utility (`ck-clean` / `ck dev clean` / `ck sandbox clean`)

`ck-clean` (or `ck dev clean` / `ck sandbox clean`) safely and recursively purges `.sandbox/` to reset dev state. Missing directory → graceful no-op. Single-line confirmation on stdout; failures report to stderr with a non-zero exit code.

```bash
./ck-clean
# [ck-clean] Sandbox environment cleared successfully.
```

### Automated Sandbox Fixtures (`ck-dev sandbox setup`)

`ck-dev sandbox setup` builds a complete, disposable mock environment inside `.sandbox/` — registered and unregistered projects, bulk history/archive data, and a registry entry whose folder is deliberately missing:

```bash
.sandbox/
├── config/
│   └── projects.json          # registry mock: alpha + orphaned-deleted
└── projects/
    ├── alpha/                 # registered & active: 6 tasks (title numbering skips 5),
    │   └── .ck/               # 120 history.log entries, 5+2 archives,
    │                          # HISTORY.md exactly at the rotation limit
    ├── beta/                  # initialized, NOT registered (ck register)
    ├── gamma/                 # plain dir, no .ck/ (non-ck behavior)
    └── (orphaned-deleted)     # registry-only → [MISSING] tag / ck prune
```

Because a sandboxed registry copy now exists, dev-mode reads (dashboards, `prune`) use it — the fixture environment fully replaces production state for the session. Setup writes are confined to `.sandbox/` **by construction**; `~/.config/ck/` is never touched.

```bash
./ck-dev sandbox setup
CK_SANDBOX=1 ./ck-dev dashboard   # → alpha + [MISSING] orphaned-deleted
CK_SANDBOX=1 ./ck-dev prune     # → purges orphaned-deleted (sandbox only)
./ck-dev sandbox clean          # → reset the workspace
```

### History Rotation Stress Emulator (`ck dev emulate`)

`ck dev emulate` is a developer-only, interactive demonstration of the history
machinery end to end. It is strictly sandbox-guarded: without an active dev
session or an existing `.sandbox/` directory it aborts with
`[i] Not in sandbox mode — emulator aborted.` and creates nothing.

The emulator works inside a dedicated `.sandbox/projects/sandbox_stress/`
project (isolated from the `alpha`/`beta` fixtures), writes a LOCAL `.ck.json`
config override (`HISTORY_LIMIT=5`, `MAX_BAK_FILES=2`, `COMPRESS_ARCHIVES=true`)
and runs **3 cycles**, each pausing ~1.7s between steps:

1. Generate a batch of tasks + history entries (`[info] Generating task batch
   for cycle N...`);
2. Log the threshold hit (`[info] HISTORY.md threshold reached`) and the
   pre-archive state;
3. Rotate into a compressed archive (`[+] Rotated HISTORY.md →
   HISTORY_<timestamp>.md.gz`), enforce FIFO purge at `MAX_BAK_FILES`
   (`[fifo] purged …`), pause, repeat;
4. Finish with a summary panel: retained archives (gz vs raw sizes), the
   `ck log --all` viewing command, and the `ck dev clean` cleanup hint.

```bash
./ck-dev dev emulate                                   # or: ck dev emulate
cd .sandbox/projects/sandbox_stress && ck log --all    # full merged history
./ck-dev dev clean                                     # reset when done
```

### Manual Testing Walkthrough (Contributors)

**Safety first — zero host impact.** The moment a sandbox session starts, every global registry interaction — `register` / `unregister` / `prune` mutations and dashboard reads alike — is re-routed from your host machine's `~/.config/ck/projects.json` to `.sandbox/config/projects.json`, every project write (`PLAN.md`, `state.json`, `HISTORY.md`, lock files, rotation archives) lands under `.sandbox/projects/`, and every global space (files, note sidecars, histories and `spaces_order`) is confined to `.sandbox/config/spaces/` plus `.sandbox/config/state.json`. The host registry and host spaces are never opened, read or written — the test suite verifies they stay byte-identical (content and mtime) across full dev sessions. `sandbox setup` writes only inside `.sandbox/` by construction, and `sandbox clean` removes only `.sandbox/` itself.

The sandbox infrastructure is the fastest way to exercise `ck` by hand — every command below runs against the mock environment, and your real registry (`~/.config/ck/`) and projects stay untouched. Use it before submitting changes to reproduce dashboard layouts, registry flows, and edge cases (missing projects, task-ID gaps, history rotation) in seconds.

Isolated commands use the explicit form `CK_SANDBOX=1 ./ck-dev <command>`: the env var is what activates write interception, so it also works with any other entrypoint (`./ck`, an installed `ck`) and inside scripts/CI, while `./ck-dev` remains the convenient wrapper for interactive use.

```bash
# 1. Build the disposable mock environment (.sandbox/)
./ck-dev sandbox setup

# 2. Global view — registered + missing projects at a glance
CK_SANDBOX=1 ./ck-dev dashboard
# | alpha                      | [3] [>] Active focus task | 2/6 (33.3%) | 1h ago |
# | [MISSING] orphaned-deleted | n/a                       | missing     | 12d ago |

# 3. Work inside a fixture project — commands edit fixtures IN PLACE
#    (they already live in .sandbox/, so no further redirection happens)
cd .sandbox/projects/alpha
CK_SANDBOX=1 ../../ck-dev st        # progress + WORK CONTEXT triad
CK_SANDBOX=1 ../../ck-dev done 4    # complete a pending task
CK_SANDBOX=1 ../../ck-dev start 6   # move the focus marker
CK_SANDBOX=1 ../../ck-dev add "fresh idea from manual testing"
cd ../../..

# 4. Registry flows — mutate the MOCK registry only
CK_SANDBOX=1 ./ck-dev prune                     # drops orphaned-deleted
CK_SANDBOX=1 ./ck-dev register --path .sandbox/projects/beta
CK_SANDBOX=1 ./ck-dev dashboard                   # beta now on the dashboard

# 5. Non-ck behavior — gamma has no .ck/ (empty status, no crash)
cd .sandbox/projects/gamma
CK_SANDBOX=1 ../../ck-dev st
cd ../../..

# 6. Reset the workspace — .sandbox/ is gone, fixtures rebuildable anytime
./ck-dev sandbox clean
```

What each fixture is for:

| Fixture | State | Manual-test targets |
| --- | --- | --- |
| `alpha` | registered, active | dashboards, focus triad, title-numbering gap (`5` is missing), bulk `history.log` (120 entries), archive inspection, rotation limit boundary |
| `beta` | initialized, **not** registered | `ck register` without `--register`, standalone registration |
| `gamma` | plain directory, no `.ck/` | CLI behavior in non-ck environments |
| `orphaned-deleted` | registry-only, no folder | `[MISSING]` tags, `ck prune` |

Hard guarantees while testing: setup/clean write only inside `.sandbox/`; dev-mode writes (including lock files and rotation archives) never leave the sandbox; `ck-dev save` skips the Git-commit phase; `ck-dev update` refuses remote fetches. When in doubt, verify with `CK_DEBUG=1` — every interception is traced to `stderr` and `.sandbox/dev.log`.

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

### Active / Pending Features
- [ ] Write additional prompt.md templates for different AI roles
- [ ] Context export/injection into external AI tools

### Ideas & Discussion (Backlog v0.5+)
- [ ] `ck diff`: show context/plan changes since the last save
- [ ] `ck undo`: safely undo the last `ck save` and revert the commit
- [ ] AI-summary (`ck suggest` / `ck summarize`): AI-generated commit messages / notes
- [ ] Interactive Plan: manage PLAN.md tasks directly from the CLI / TUI

### Completed
- [x] Basic project structure (`.ck/`) initialization
- [x] Object-oriented task parser model (CommonMark + legacy `- []`)
- [x] Three task statuses: `- [ ]`, `- [x]`, `- [>]` (Focus)
- [x] Task management commands: `ck add`, `ck start <ID>`, `ck done <ID|range|list>`
- [x] Task triad display (`ck st`): Past → Current → Future
- [x] Progress calculation (%) and task counters (Total/Done/Left)
- [x] ID gap detector
- [x] Process notes (`ck note <text>`) and the `Unfocused / Paused Context` block
- [x] Global project registry and dashboard (`ck dashboard`, `ck dashboard -v`, `ck prune`)
- [x] Atomic writes and fail-closed file locking (`*.lock`)
- [x] History rotation system: `HISTORY_LIMIT` threshold, `.md.gz` compression (`COMPRESS_ARCHIVES`), FIFO purge (`MAX_BAK_FILES`)
- [x] Cross-archive history viewing: `ck log --all` with transparent decompression
- [x] Isolated dev mode (`ck-dev`), sandbox (`.sandbox/`) and fixtures (`ck dev setup`)
- [x] Interactive history-rotation stress emulator (`ck dev emulate`)
- [x] Install and update utilities (`ck install`, `ck update`, `ck uninstall`)
- [x] Shell Completion for Bash / Zsh: commands, flags, registered projects and spaces (`ck completion bash|zsh`, executable `CK_BIN` fallback)
- [x] Baseline pytest coverage (`cklib/core.py`, `cklib/history.py` and the full CLI/sandbox suite)
- [x] Strict sandbox isolation for global spaces (`.sandbox/config/spaces/`) and `ck save` history-vs-Git wording audit (`v0.8.21`)

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
- Авто-архивация: ротация HISTORY.md по лимиту HISTORY_LIMIT со сжатыми `.md.gz`-архивами (COMPRESS_ARCHIVES) и FIFO-очисткой по MAX_BAK_FILES
- Парсинг `- [ ]` / `- [>]` / `- [x]` (устаревший `- []` нормализуется)
- Поддержка AI
- Глобальный реестр проектов и панель мониторинга
- Fail-closed блокировка файлов, атомарная запись
- Дев-режим с песочницей: `ck-dev` выполняет команды в режиме «только чтение» реальных данных, все записи уходят в git-ignored `.sandbox/` (`ck-clean` очищает её); глобальные пространства строго изолированы в `.sandbox/config/spaces/`; `ck-dev sandbox setup` строит одноразовое окружение-макет с объёмными тестовыми данными, `ck dev emulate` — интерактивный стресс-эмулятор ротации истории (3 цикла, `.md.gz`-архивы, FIFO-очистка) внутри `.sandbox/projects/sandbox_stress`
- Автодополнение shell: `ck completion bash|zsh` для команд, флагов, зарегистрированных проектов и глобальных пространств (без filename-fallback; учитывает исполняемый `CK_BIN`)

---

## 📋 Требования
- Python 3.8+
- Git (опционально — ck save корректно пропустит коммит без него)
- fzf, jq (опционально)

---

## 📦 Установка

### Установка одной командой (рекомендуется)
```bash
curl -sSL https://raw.githubusercontent.com/vinyardrip/context-keeper/main/install.sh | bash
```
Скачивает исходники ветки `main` **потоком** прямо в `~/.local/share/context-keeper` (без временного `.tar.gz` и без `.git`) и создаёт симлинк `~/.local/bin/ck` на лаунчер. Sudo не требуется. Если `~/.local/bin` отсутствует в `$PATH`, скрипт выведет точную команду для его добавления (bash / zsh / `fish_add_path`). Повторный запуск обновляет код на месте и сохраняет пользовательские данные (`.ck/`).

### Ручная установка (для разработки)
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
├── HISTORY_*.md.gz      # сжатые архивы ротации (по умолчанию)
└── HISTORY_*.md.bak     # устаревшие несжатые архивы
```

---

## 🛠 Команды
(полный список аналогичен английской версии выше)

> **Изоляция путей в dev-режиме.** Пока активна песочница (`ck-dev` /
> `CK_SANDBOX=1` / `CK_DEV=1` / cwd внутри `.sandbox/`), все пути
> `~/.config/ck/spaces/...` и порядок пространств в
> `~/.config/ck/state.json` заменяются на `.sandbox/config/spaces/...` и
> `.sandbox/config/state.json`. Пространства хоста не читаются, не
> обнаруживаются и не изменяются; песочница начинает с собственными
> пустыми шаблонами `local` / `remote`.

### Инициализация и регистрация

`ck init` по умолчанию работает строго локально: создаёт структуру `.ck/` и
обновляет `.gitignore` проекта, но не создаёт и не изменяет
`~/.config/ck/projects.json`.

Флаг `ck init --register` выполняет локальную инициализацию и одновременно
добавляет проект в глобальный реестр. Уже инициализированный проект можно в
любой момент добавить отдельно командой `ck register` (при необходимости с
флагами `--path PATH` и/или `-n NAME`).

### Гигиена глобального реестра

Команда `ck dashboard` не скрывает зарегистрированные проекты,
если их каталоги были удалены или перемещены. Такие записи помечаются тегом
`[MISSING]`, а в конце вывода появляется подсказка:

```text
[i] Found X missing project(s). Run 'ck prune' to cleanup.
```

Команда `ck prune` удаляет осиротевшие записи из
`~/.config/ck/projects.json`, печатает каждый удалённый путь и итоговое число
очищенных проектов.

### `ck list` вне проекта

`ck list` никогда не угадывает. Запущенная вне любого проекта, команда **не**
подставляет молча последний активный проект из глобального реестра — вместо
этого она показывает, что нашла:

```text
$ cd ~/work            # внутри зарегистрированы проекты alpha и beta
$ ck list
[!] Current directory is not a context-keeper project.

Available nested projects:
  - alpha
  - beta

To view tasks, navigate to a project directory or run: ck list <project_name>
```

Когда вложенный проект ровно один, выбирать не нужно — его задачи выводятся
с явным указанием проекта в заголовке секции:

```text
$ ck list
## Current Sprint (alpha)
[ ] 1. ship the migration
[>] 2. write the release notes
```

`ck list <project_name>` явно указывает зарегистрированный проект и работает из
любого каталога; неизвестное имя — это ошибка (код выхода 1), а не пустой вывод:

```text
$ ck list nope
ERROR: Project 'nope' not found.
```

Если в реестре нет ни одной записи, вложенной в текущий каталог, `ck list`
дополнительно **сканирует файловую систему** (дочерние и вложенные каталоги,
глубина ≤ 2) и находит папки, где физически лежит `PLAN.md`. Найденные
незарегистрированные проекты показываются в списке **и** доступны по имени, так
что подсказка всегда остаётся рабочей. Скрытые каталоги, деревья зависимостей
(`node_modules`, `venv`, …) и симлинки пропускаются.

### `ck st` вне проекта

`ck st` следует тем же правилам обнаружения — молча подставлять последний
активный проект реестра команда тоже не будет:

- ровно **один** проект во вложенных каталогах текущего — выводится его статус
  с аннотацией `<- <путь>`, чтобы было видно, какой проект выбран;
- **несколько** вложенных проектов — то же уведомление со списком всех вариантов
  и подсказкой `ck st <project_name>` / `ck list <project_name>`;
- **ноль** — стандартный фолбэк на глобальный реестр (самый недавно активный
  зарегистрированный проект) либо явное состояние `No active project found`:
  чистый выход, без трейсбэков.

`ck st <project_name>` явно указывает проект из любого каталога — по тем же
правилам, что и `ck list <project_name>` (имя реестра → имя каталога → скан);
неизвестное имя — код выхода 1 и `ERROR: Project '<name>' not found.` Флаги вида
применяются к **именованному** проекту: `ck st alpha -l` выводит задачи alpha,
`ck st alpha --all` — её статус и полный `PLAN.md`. `ck st -g` — это кросс-проектный
дашборд, поэтому имя проекта он не принимает (сочетание — usage-ошибка).

`ck st --all` печатает PLAN.md только того проекта, чей статус показан выше, —
при нескольких кандидатах план не выбирается молча.

---

## Перестановка задач

Команды перестановки меняют порядок **только незавершённых** задач — `[ ]` и
`[>]`. Заголовки, проза, секция `## Completed`, статусы и заголовки задач не
изменяются; маркер фокуса перемещается вместе со своей задачей.

```text
$ ck list
## Current Sprint
[ ] 1. write the migration
[ ] 2. review the PR
[>] 3. ship the release

$ ck move 3 1
[ok] Moved task #3 -> position 1

$ ck move 1 bottom
[ok] Moved task #1 -> position 3

$ ck swap 1 2
[ok] Swapped task #1 <-> task #2

$ ck reorder 2 1
[ok] Reordered active tasks: 2, 1
     New order:
       1. write the migration
       2. review the PR
       3. ship the release

## Current Sprint
[ ] 1. write the migration
[ ] 2. review the PR
[>] 3. ship the release
```

Идентификаторы задач **позиционные** (нумеруются 1..N в порядке документа), поэтому
после перестановки номер рядом с задачей меняется. Команда `ck move 1 bottom`
переносит задачу на последнюю активную позицию.

Подтверждение `ck reorder` намеренно многострочное: первая строка перечисляет
применённые ID, отступленный блок под ней — порядок, который теперь записан в
`PLAN.md`. ID позиционные, поэтому после перестановки они перенумеровываются,
и в сводке показываются ID **после** применения команды. Так как файл только что
изменился, `ck reorder` сразу же отрисовывает представление спринта (то же самое,
что выводит `ck list` для этого проекта) — второй командой запускать ничего не
нужно. Одиночные `ck move` и `ck swap` оставляют однострочное подтверждение и
спринт автоматически **не** показывают.

### Завершённые задачи переставлять нельзя

Задача `[x]` — это история, и её позиция является частью этой истории. Попытка
трогать такую задачу прерывает **всю** операцию: файл остаётся побайтово
неизменным, команда не может примениться наполовину.

```text
$ ck move 4 1
[err] Cannot move completed task #4. Only pending/active tasks can be reordered.

$ echo $?
1
```

Коды выхода: `0` успех · `1` попытка переставить завершённую задачу · `2` ошибка
использования (неизвестный ID, позиция вне диапазона, нечисловой ID, недостаточно
аргументов).

---

## Сценарий работы ck save
`ck save` фиксирует **чекпоинт** и **не завершает** активную задачу.
Завершение остаётся отдельным явным действием `ck done <ID>` (оно же
архивирует заметку этой задачи в HISTORY.md).

1. Показать активную задачу и явно сообщить, что она остаётся открытой.
2. **Шаг 1 — локальная история (без Git):** ввести опциональный текст
   заметки (inline или в редакторе), затем короткое резюме; датированная
   запись добавляется в `.ck/HISTORY.md`.
3. **Шаг 2 — опциональный локальный Git-коммит:** выбрать сообщение
   коммита и подтвердить. Индексируются только артефакты Context Keeper;
   `ck` никогда не выполняет push. Если Git недоступен или вы отказались,
   запись истории остаётся сохранённой.
4. Авто-архивация HISTORY.md при превышении лимита (бесшовная ротация,
   оригинал сохраняется как сжатый `.md.gz`-архив).

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
- Ротация истории (безопасная при сбоях): `HISTORY_LIMIT` записей → архив `HISTORY_*.md.gz` (`COMPRESS_ARCHIVES`, сжатие встроенным модулем `gzip`), удержание `MAX_BAK_FILES` архивов (FIFO-очистка старых); весь журнал — `ck log --all` (архивы `.md.gz` и legacy `.md.bak` читаются прозрачно, повреждённые пропускаются с предупреждением)
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
├── config/              # песочные реестр, state и пространства
│   └── spaces/          # local/, remote/, кастомные пространства (изолированы)
└── dev.log              # отладочный лог
```

- Создаётся автоматически при первой dev-записи; в Git не попадает (`.gitignore`).
- Песочные копии **проектов наследуют реальное состояние** (например, запись в песочный `HISTORY.md` начинается с реальной истории), поэтому dev-сессия отражает продакшен, не изменяя его.
- **Глобальные пространства — исключение: они строго изолированы.** На всю сессию `~/.config/ck/spaces/` заменяется на `.sandbox/config/spaces/`, поэтому пространства хоста не читаются и не обнаруживаются; песочница создаёт собственные пустые шаблоны `local` / `remote`.

### Безопасный бинарник разработки (`ck-dev`)

`ck-dev` — точка входа в песочницу. Он устанавливает `CK_SANDBOX=1` (аналогично работают флаг `--sandbox` и `CK_DEV=1`) и ведёт себя как `ck` с одним ключевым отличием:

- **Чтение проектов реальное** (с read-your-writes в песочнице): статус, дашборды и списки задач показывают настоящие проекты — если нет песочной копии проекта (созданной предыдущей dev-записью или `sandbox setup`); при её наличии читается копия, чтобы цепочки dev-команд корректно складывались. **Глобальные пространства читаются только из песочной копии** — продакшен-пространства не попадают в сессию.
- **Записи перехватываются**: ВСЕ операции записи — мутации задач (`add` / `start` / `done`), правки `PLAN.md`, обновления `state.json`, lock-файлы, бэкапы, пространства (`add` / `focus` / `done` / `note` / `space create|rename|delete`), изменения реестра — строго перенаправляются в `.sandbox/`.

Пока активен `IS_DEV`, продакшен остаётся неизменным: реальный `~/.config/context-keeper/` и реальные `PLAN.md` не модифицируются (проверяется побайтно, включая mtime, в тестах). Дополнительные защитные барьеры в dev-режиме:

- `ck-dev save` сохраняет запись истории в песочницу, но **пропускает фазу Git-коммита**.
- `ck-dev update` отказывается от fetch/pull установки.

```bash
./ck-dev add "безопасный эксперимент"   # запись уйдёт в .sandbox/
./ck-dev st                             # статус читает РЕАЛЬНЫЙ проект
```

### Утилита очистки (`ck-clean` / `ck dev clean` / `ck sandbox clean`)

`ck-clean` (или `ck dev clean` / `ck sandbox clean`) безопасно и рекурсивно удаляет `.sandbox/`, сбрасывая dev-состояние. Отсутствующий каталог — корректный no-op. Подтверждение — одна строка в stdout; при ошибке — сообщение в stderr и ненулевой код возврата.

```bash
./ck-clean
# [ck-clean] Sandbox environment cleared successfully.
```

### Автоматические фикстуры песочницы (`ck-dev sandbox setup`)

`ck-dev sandbox setup` строит полное одноразовое окружение-макет внутри `.sandbox/` — зарегистрированные и незарегистрированные проекты, объёмные данные истории/архивов и запись реестра без каталога:

```bash
.sandbox/
├── config/
│   └── projects.json          # макет реестра: alpha + orphaned-deleted
└── projects/
    ├── alpha/                 # зарегистрирован и активен: 6 задач (пропуск 5),
    │   └── .ck/               # 120 записей history.log, 5+2 архива,
    │                          # HISTORY.md ровно на лимите ротации
    ├── beta/                  # инициализирован, НЕ зарегистрирован (ck register)
    ├── gamma/                 # обычный каталог без .ck/ (не-ck поведение)
    └── (orphaned-deleted)     # только в реестре → тег [MISSING] / ck prune
```

Поскольку песочная копия реестра существует, dev-чтения (дашборды, `prune`) используют её — фикстурное окружение полностью заменяет продакшен для сессии. Записи setup не покидают `.sandbox/` **по построению**; `~/.config/ck/` не затрагивается.

```bash
./ck-dev sandbox setup
CK_SANDBOX=1 ./ck-dev dashboard   # → alpha + [MISSING] orphaned-deleted
CK_SANDBOX=1 ./ck-dev prune     # → удаляет orphaned-deleted (только в песочнице)
./ck-dev sandbox clean          # → сброс рабочего окружения
```

### Стресс-эмулятор ротации истории (`ck dev emulate`)

`ck dev emulate` — dev-only интерактивная демонстрация механизма истории end-to-end. Команда строго защищена песочницей: без активной dev-сессии или существующего каталога `.sandbox/` она прерывается сообщением `[i] Not in sandbox mode — emulator aborted.` и ничего не создаёт.

Эмулятор работает в отдельном проекте `.sandbox/projects/sandbox_stress/` (изолирован от фикстур `alpha`/`beta`), пишет ЛОКАЛЬНЫЙ переопределяющий конфиг `.ck.json` (`HISTORY_LIMIT=5`, `MAX_BAK_FILES=2`, `COMPRESS_ARCHIVES=true`) и выполняет **3 цикла** с паузами ~1.7s между шагами:

1. Генерация порции задач и записей истории (`[info] Generating task batch for cycle N...`);
2. Логирование превышения лимита (`[info] HISTORY.md threshold reached`) и состояния перед архивацией;
3. Ротация в сжатый архив (`[+] Rotated HISTORY.md → HISTORY_<timestamp>.md.gz`), FIFO-очистка по `MAX_BAK_FILES` (`[fifo] purged …`), пауза, повтор;
4. Итоговая панель: удерживаемые архивы (размеры gz/raw), команда просмотра `ck log --all` и подсказка очистки `ck dev clean`.

```bash
./ck-dev dev emulate                                   # или: ck dev emulate
cd .sandbox/projects/sandbox_stress && ck log --all    # полная объединённая история
./ck-dev dev clean                                     # сброс после проверки
```

### Ручное тестирование (для контрибьюторов)

**Безопасность прежде всего — нулевое воздействие на хост.** С момента начала песочной сессии каждое взаимодействие с глобальным реестром — мутации `register` / `unregister` / `prune` и чтения дашбордов — перенаправляется из `~/.config/ck/projects.json` вашей машины в `.sandbox/config/projects.json`, каждая запись проекта (`PLAN.md`, `state.json`, `HISTORY.md`, lock-файлы, архивы ротации) попадает в `.sandbox/projects/`, а все глобальные пространства (файлы, sidecar-заметки, истории и `spaces_order`) ограничены `.sandbox/config/spaces/` и `.sandbox/config/state.json`. Реестр и пространства хоста никогда не открываются, не читаются и не изменяются — тестовый набор проверяет, что они остаются побайтово идентичными (содержимое и mtime) на протяжении полных dev-сессий. `sandbox setup` пишет только внутри `.sandbox/` по построению, а `sandbox clean` удаляет только сам `.sandbox/`.

Инфраструктура песочницы — самый быстрый способ опробовать `ck` вручную: все команды ниже работают с окружением-макетом, а реальный реестр (`~/.config/ck/`) и ваши проекты остаются нетронутыми. Используйте её перед отправкой изменений, чтобы за секунды воспроизводить макеты дашбордов, сценарии реестра и граничные случаи (отсутствующие проекты, пропуски в ID задач, ротацию истории).

Изолированные команды используют явную форму `CK_SANDBOX=1 ./ck-dev <команда>`: именно переменная окружения активирует перехват записей, поэтому она работает с любой точкой входа (`./ck`, установленным `ck`) и внутри скриптов/CI, а `./ck-dev` остаётся удобной обёрткой для интерактивной работы.

```bash
# 1. Собираем одноразовое окружение-макет (.sandbox/)
./ck-dev sandbox setup

# 2. Глобальный обзор — зарегистрированные и отсутствующие проекты
CK_SANDBOX=1 ./ck-dev dashboard
# | alpha                      | [3] [>] Active focus task | 2/6 (33.3%) | 1h ago |
# | [MISSING] orphaned-deleted | n/a                       | missing     | 12d ago |

# 3. Работа внутри фикстурного проекта — команды правят фикстуры НА МЕСТЕ
#    (они уже внутри .sandbox/, дальнейшего перенаправления не происходит)
cd .sandbox/projects/alpha
CK_SANDBOX=1 ../../ck-dev st        # прогресс + триада WORK CONTEXT
CK_SANDBOX=1 ../../ck-dev done 4    # закрыть ожидающую задачу
CK_SANDBOX=1 ../../ck-dev start 6   # переместить маркер фокуса
CK_SANDBOX=1 ../../ck-dev add "свежая идея из ручного теста"
cd ../../..

# 4. Сценарии реестра — меняется ТОЛЬКО макет реестра
CK_SANDBOX=1 ./ck-dev prune                     # удаляет orphaned-deleted
CK_SANDBOX=1 ./ck-dev register --path .sandbox/projects/beta
CK_SANDBOX=1 ./ck-dev dashboard                   # beta теперь на дашборде

# 5. Не-ck поведение — в gamma нет .ck/ (пустой статус, без падений)
cd .sandbox/projects/gamma
CK_SANDBOX=1 ../../ck-dev st
cd ../../..

# 6. Сброс рабочего окружения — .sandbox/ исчезает, фикстуры пересобираемы в любой момент
./ck-dev sandbox clean
```

Назначение каждой фикстуры:

| Фикстура | Состояние | Что проверять вручную |
| --- | --- | --- |
| `alpha` | зарегистрирован, активен | дашборды, триаду фокуса, детектор пропусков (пропущена `5`), объёмный `history.log` (120 записей), инспекцию архивов, границу лимита ротации |
| `beta` | инициализирован, **не** зарегистрирован | `ck register` без `--register`, standalone-регистрацию |
| `gamma` | обычный каталог без `.ck/` | поведение CLI в не-ck окружениях |
| `orphaned-deleted` | только в реестре, без каталога | теги `[MISSING]`, `ck prune` |

Жёсткие гарантии во время тестирования: setup/clean пишут только внутри `.sandbox/`; dev-записи (включая lock-файлы и архивы ротации) никогда не покидают песочницу; `ck-dev save` пропускает фазу Git-коммита; `ck-dev update` отказывается от обращений к удалённому репозиторию. Если есть сомнения — проверьте с `CK_DEBUG=1`: каждый перехват трассируется в `stderr` и `.sandbox/dev.log`.

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

### Активные / запланированные задачи
- [ ] Написать дополнительные шаблоны prompt.md для разных ролей ИИ
- [ ] Настроить механизмы экспорта/инжекции контекста в сторонние ИИ-инструменты

### Идеи и обсуждения (бэклог v0.5+)
- [ ] `ck diff`: вывод изменений в контексте/плане с момента последнего сохранения
- [ ] `ck undo`: безопасная отмена последнего `ck save` и откат коммита
- [ ] AI-summary (`ck suggest` / `ck summarize`): генерация commit message / заметки через ИИ
- [ ] Interactive Plan: управление задачами в PLAN.md прямо из CLI / TUI

### Завершённые
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
- [x] Система ротации истории: порог `HISTORY_LIMIT`, сжатие `.md.gz` (`COMPRESS_ARCHIVES`), FIFO-очистка (`MAX_BAK_FILES`)
- [x] Сквозной просмотр истории: `ck log --all` с прозрачной декомпрессией
- [x] Изолированный dev-режим (`ck-dev`), песочница (`.sandbox/`) и фикстуры (`ck dev setup`)
- [x] Интерактивный стресс-эмулятор ротации истории (`ck dev emulate`)
- [x] Утилиты установки и обновления (`ck install`, `ck update`, `ck uninstall`)
- [x] Shell Completion (автодополнение) для Bash / Zsh: команды, флаги, зарегистрированные проекты и пространства (`ck completion bash|zsh`, fallback на исполняемый `CK_BIN`)
- [x] Базовое покрытие pytest (`cklib/core.py`, `cklib/history.py` и полный набор тестов CLI/песочницы)
- [x] Строгая изоляция глобальных пространств в песочнице (`.sandbox/config/spaces/`) и аудит формулировок `ck save` про историю и Git (`v0.8.21`)
