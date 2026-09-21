# Context Keeper (ck) 🧠
Minimalist Unix-way "external memory" for developers
Минималистичная «внешняя память» разработчика в стиле Unix

[![version](https://img.shields.io/badge/version-0.5.0-blue)]()
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
- Self-Updater: ck update (git fetch + `pull --ff-only`, refuses on dirty work tree)
- CLI Task Management: ck add <text>, ck start <ID>, ck done <ID|range|list>
- Process Notes: `ck note <text>` attaches a scratchpad to the active task — shown in `ck st`, archived to HISTORY.md on `ck done`, and carried through focus switches via the Unfocused / Paused Context block
- Global Registry: Cross-project dashboard (ck dashboard) backed by `~/.config/ck/projects.json`; registration is explicit
- Fail-Closed Locking: Cross-process file locking guards registry, PLAN.md, HISTORY.md and state.json writes
- Full Plan View: ck st --all
- Dev Sandbox: `ck-dev` runs any command against real data read-only, redirecting all writes into a git-ignored `.sandbox/` (`ck-clean` resets it); `ck-dev sandbox setup` builds a disposable mock environment with bulk test data

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
./install.sh   # copies ck into ~/.local/bin + cklib snapshot (no sudo)
```

### Option 2 — Built-in installer
```bash
git clone https://github.com/vinyardrip/context-keeper.git
cd context-keeper
chmod +x ck
./ck install   # physical copy to ~/.local/bin/ck + cklib snapshot
```

> Production isolation: `ck install` writes a **physical copy** of the launcher to `~/.local/bin/ck` (regular executable file, 0755 — never a symlink) and snapshots the `cklib/` package to `~/.local/share/ck/cklib`. The installed command is a static snapshot: editing this checkout does not change `~/.local/bin/ck` until you explicitly re-run `ck install` (or `ck update`).
> Note: `ck` is a thin wrapper over the `cklib/` package — install from the repository (or via `pip install .`), not as a standalone single file.

### Verify
```bash
ck -v    # → ck version 0.5.0
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
| ck add \<text\> | Add a new open task (inserted before `## Completed`) |
| ck start \<ID\> | Focus a task (`- [>]`); `ck start 0` resets focus. A noted task that loses focus moves to Unfocused / Paused Context (note preserved until `ck done` archives it); a noteless one gets a soft attach-a-note hint |
| ck done \<ID\|range\|list\> | Mark task(s) done (`- [x]`), e.g. `3`, `2-4`, `1,3,5`; the completed task's process note is archived into HISTORY.md and cleared |
| ck note \<text\> | Attach/update a process note on the active task (shown in `ck st`; archived to HISTORY.md by `ck done`) |
| ck edit | Open PLAN.md in your editor (see [Editor Resolution](#editor-resolution)) |
| ck save | Task completion workflow: optional note + optional **local** commit |
| ck log | Open HISTORY.md in your editor (see [Editor Resolution](#editor-resolution)) |
| ck log --all | View the full history: every rotation archive (`.md.gz` decompressed / legacy `.md.bak`, oldest first) concatenated with the current HISTORY.md |
| ck st | Status overview (tri-state: Previous / Focus / Next); an explicit focus is duplicated at the top as `-> CURRENT FOCUS: [#<id>] <title>` (with its note) |
| ck notes | List all active process notes: `[>] Active Focus:` plus `[!] Unfocused / Paused Context:` with each paused task's bound note (`[i] No active process notes found.` when none) |
| ck st -l / --list | Print the task list to STDOUT (same as `ck list`) |
| ck st -e / --edit | Open PLAN.md in your editor (same as `ck edit`) |
| ck st -g / --global | Cross-project dashboard (same as `ck dashboard`) |
| ck st --all | Full status incl. full PLAN.md |
| ck -l / --list | Top-level shorthand for `ck st -l` (list tasks) |
| ck -e / --edit | Top-level shorthand for `ck st -e` (edit plan) |
| ck -g / --global | Top-level shorthand for `ck st -g` (global dashboard) |
| ck list | Print the task list to STDOUT (pipe-friendly) |
| ck dashboard | Cross-project dashboard (compact table: Project \| Focus Task \| Progress \| Last Active) |
| ck dashboard -v / --verbose | Detailed block view: full PREV/FOCUS/NEXT triad context per project |
| ck register [-n NAME] [--path PATH] | Add a project to the global registry |
| ck unregister [--path PATH \| NAME] | Remove a project from the registry |
| ck prune | Purge registry entries whose folders no longer exist; reports each purged path and a summary count |
| ck info | Installation diagnostics (version, branch, paths) |
| ck install | Copy ck to ~/.local/bin/ck (physical executable file, no symlink) + cklib snapshot to ~/.local/share/ck; no sudo |
| ck uninstall | Remove ~/.local/bin/ck, the cklib snapshot and any legacy ~/.local/bin/ck-dev |
| ck update | Self-update via git fetch + pull --ff-only (refuses if dirty) |
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

### Initialization & Registration

`ck init` is strictly local by default. It creates the `.ck/` project
structure and updates the project `.gitignore`, but it does **not** create or
modify `~/.config/ck/projects.json`.

Use `ck init --register` when the project should be initialized and tracked in
the global registry in one step. An already initialized project can be added
to the registry at any time with the standalone command `ck register` (use
`--path PATH` and/or `-n NAME` when needed).

### Global Registry Hygiene

`ck dashboard` keeps registered projects visible even when a
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
- Auto-archive HISTORY.md when limit reached (gapless rotation, original preserved as a compressed `.md.gz` archive by default)

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
     [!] Skipped: (none)
     [>] Focus:
        - [3] write docs
     >> Upcoming: (none)
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
Local commits only. `ck save` commits if you confirm; `ck` never pushes. `ck update` fast-forwards the installation itself only when the work tree is clean, then refreshes an installed copy (`~/.local/bin/ck`) so production stays in sync with the checkout.

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

- **Reads are real** (with sandbox read-your-writes): dashboards, status, and task lists reflect your actual projects — unless a sandboxed copy exists (seeded by a previous dev-mode write or `sandbox setup`), in which case that copy is read so chained dev commands compose.
- **Writes are intercepted**: ALL write operations — task mutations (`add` / `start` / `done`), `PLAN.md` edits, `state.json` updates, lock files, backups, registry changes — are redirected strictly into `.sandbox/`.

Bare `ck-dev` enters an interactive **session subshell**. On entry the warning banner reports the **active binary**, resolved dynamically at runtime (`command -v ck` semantics — never hardcoded), and after you leave by typing `exit` (or pressing Ctrl+D) the exit handler reports the global binary that takes over:

```text
┌──────────────────────────────────────────────────────────────┐
│ ⚠️  SANDBOX MODE ACTIVE                                      │
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
    ├── alpha/                 # registered & active: 6 tasks (gap at 5),
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

**Safety first — zero host impact.** The moment a sandbox session starts, every global registry interaction — `register` / `unregister` / `prune` mutations and dashboard reads alike — is re-routed from your host machine's `~/.config/ck/projects.json` to `.sandbox/config/projects.json`, and every project write (`PLAN.md`, `state.json`, `HISTORY.md`, lock files, rotation archives) lands under `.sandbox/projects/`. The host registry is never opened for writing, never created, and never quarantined — the test suite verifies it stays byte-identical (content and mtime) across full dev sessions. `sandbox setup` writes only inside `.sandbox/` by construction, and `sandbox clean` removes only `.sandbox/` itself.

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
CK_SANDBOX=1 ../../ck-dev st        # progress + triad, gap shown as "gaps: 4-6"
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
| `alpha` | registered, active | dashboards, focus triad, gap detection (`5` is missing), bulk `history.log` (120 entries), archive inspection, rotation limit boundary |
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
- [ ] Shell Completion: command autocompletion for Bash / Zsh
- [ ] Unit Testing: baseline pytest coverage (`cklib/core.py`, `cklib/history.py`)

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
- Дев-режим с песочницей: `ck-dev` выполняет команды в режиме «только чтение» реальных данных, все записи уходят в git-ignored `.sandbox/` (`ck-clean` очищает её); `ck-dev sandbox setup` строит одноразовое окружение-макет с объёмными тестовыми данными, `ck dev emulate` — интерактивный стресс-эмулятор ротации истории (3 цикла, `.md.gz`-архивы, FIFO-очистка) внутри `.sandbox/projects/sandbox_stress`

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
├── HISTORY_*.md.gz      # сжатые архивы ротации (по умолчанию)
└── HISTORY_*.md.bak     # устаревшие несжатые архивы
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

Команда `ck dashboard` не скрывает зарегистрированные проекты,
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
- Авто-архивация HISTORY.md при превышении лимита (бесшовная ротация, оригинал сохраняется как сжатый `.md.gz`-архив)

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
├── config/              # песочные копии глобального реестра и state
└── dev.log              # отладочный лог
```

- Создаётся автоматически при первой dev-записи; в Git не попадает (`.gitignore`).
- Песочные копии **наследуют реальное состояние** (например, запись в песочный `HISTORY.md` начинается с реальной истории), поэтому dev-сессия отражает продакшен, не изменяя его.

### Безопасный бинарник разработки (`ck-dev`)

`ck-dev` — точка входа в песочницу. Он устанавливает `CK_SANDBOX=1` (аналогично работают флаг `--sandbox` и `CK_DEV=1`) и ведёт себя как `ck` с одним ключевым отличием:

- **Чтение реальное** (с read-your-writes в песочнице): статус, дашборды и списки задач показывают настоящие проекты — если нет песочной копии (созданной предыдущей dev-записью или `sandbox setup`); при её наличии читается копия, чтобы цепочки dev-команд корректно складывались.
- **Записи перехватываются**: ВСЕ операции записи — мутации задач (`add` / `start` / `done`), правки `PLAN.md`, обновления `state.json`, lock-файлы, бэкапы, изменения реестра — строго перенаправляются в `.sandbox/`.

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

**Безопасность прежде всего — нулевое воздействие на хост.** С момента начала песочной сессии каждое взаимодействие с глобальным реестром — мутации `register` / `unregister` / `prune` и чтения дашбордов — перенаправляется из `~/.config/ck/projects.json` вашей машины в `.sandbox/config/projects.json`, а каждая запись проекта (`PLAN.md`, `state.json`, `HISTORY.md`, lock-файлы, архивы ротации) попадает в `.sandbox/projects/`. Реестр хоста никогда не открывается на запись, не создаётся и не помещается в карантин — тестовый набор проверяет, что он остаётся побайтово идентичным (содержимое и mtime) на протяжении полных dev-сессий. `sandbox setup` пишет только внутри `.sandbox/` по построению, а `sandbox clean` удаляет только сам `.sandbox/`.

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
CK_SANDBOX=1 ../../ck-dev st        # прогресс + триада, пропуск виден как "gaps: 4-6"
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
- [ ] Shell Completion: автодополнение команд в Bash / Zsh
- [ ] Unit Testing: базовое покрытие тестами через pytest (`cklib/core.py`, `cklib/history.py`)

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
