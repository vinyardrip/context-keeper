"""Core orchestrator: ties together config, models, parser, registry, and git.

All PLAN.md mutations are AST-driven: read → mutate the in-memory
:class:`TaskList` → call :func:`render_plan` → atomic write. No
string-based mutations, no regex rewrites.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import git as gith
from . import registry
from .config import (
    CK_DIR_NAME,
    DEFAULT_CK_GITIGNORE,
    DEFAULT_PLAN,
    DEFAULT_PROMPT,
    DEFAULT_REPO_BRANCH,
    DEFAULT_REPO_URL,
    GITIGNORE_FILENAME,
    GLOBAL_REGISTRY_FILE,
    GLOBAL_STATE_FILE,
    HISTORY_FILENAME,
    HISTORY_LIMIT,
    LEGACY_GLOBAL_CONFIG_FILE,
    LOCAL_GITIGNORE_ENTRIES,
    PLAN_FILENAME,
    PROMPT_FILENAME,
    README_FILENAME,
    STATE_FILENAME,
    TRACKED_GITIGNORE_PROTECTIONS,
    UPDATE_CHECK_INTERVAL_HOURS,
    USER_INSTALL_PATH,
    VERSION,
    file_lock,
    find_project_root,
    get_editor,
    warn_if_sensitive_root,
)
from .models import Notes, TaskList, TaskStatus
from .parser import (
    _atomic_write_text,
    _find_completed_line,
    _find_section_end,
    load_repaired_plan,
    normalize_plan,
    parse_plan_file,
    render_plan,
    sanitize_task_text,
)


# Patterns that, if found at the top of a root ``.gitignore``, blanket-
# ignore everything. We append ``!``-negations to keep tracked files
# (PLAN.md, prompt.md, README.md) explicitly visible to Git.
_BLANKET_IGNORE_PATTERNS = (
    re.compile(r"^\s*\*\s*$"),                  # "*" (universal catch-all)
    re.compile(r"^\s*\.\s*$"),                  # "." (also catches everything)
    re.compile(r"^\s*\?\*\s*$"),                # "?*" (any single file)
    re.compile(r"^\s*\.ck/?\s*$"),              # ".ck" or ".ck/" → blanket on .ck
)

# A HISTORY.md *entry heading* is exactly a column-0 "### " followed
# by an ISO date prefix (e.g. "### 2026-09-07 16:04 | [3] task").
# Note bodies (which may contain arbitrary "### " text) are always
# wrapped in ``` fences by ``Notes.render``; the date anchor plus
# fence-state tracking excludes those false matches.
_HISTORY_ENTRY_RE = re.compile(r"^### \d{4}-\d{2}-\d{2}.*$")


def _iter_history_lines(content: str):
    """Yield ``(is_entry_heading, line_with_ends)`` per line.

    Lines inside ```/~~~ code fences are never entry headings. Fence
    state toggles on each fence marker, matching CommonMark behaviour
    closely enough for HISTORY.md (fences written by ``Notes.render``
    and ordinary hand edits).
    """
    in_fence = False
    for line in content.splitlines(keepends=True):
        stripped = line.lstrip()[:3]
        if stripped in ("```", "~~~"):
            in_fence = not in_fence
            yield False, line
            continue
        yield (not in_fence and bool(_HISTORY_ENTRY_RE.match(line))), line


def _count_history_entries(content: str) -> int:
    """Count real history entries (anchored headings, not body text)."""
    return sum(1 for is_entry, _ in _iter_history_lines(content) if is_entry)


def _last_history_entry(content: str) -> str:
    """Return the text from the last anchored entry heading to EOF.

    Falls back to "" when no anchored entry exists. This is stricter
    than ``content.rfind("### ")`` which could land inside a note
    body or code fence.
    """
    lines = list(_iter_history_lines(content))
    last_idx = -1
    for i, (is_entry, _) in enumerate(lines):
        if is_entry:
            last_idx = i
    if last_idx == -1:
        return ""
    return "".join(line for _, line in lines[last_idx:])


@dataclass
class FocusResult:
    """Result of a focus-mutating operation."""

    task_id: int
    title: str
    path: Path


class ContextKeeper:
    """High-level orchestrator. One instance per CLI invocation."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root: Path = root or find_project_root()
        self.ck_path: Path = self.root / CK_DIR_NAME
        self.state_file: Path = self.ck_path / STATE_FILENAME
        self.plan_file: Path = self.ck_path / PLAN_FILENAME
        self.history_file: Path = self.ck_path / HISTORY_FILENAME
        self.prompt_file: Path = self.ck_path / PROMPT_FILENAME
        self.readme_file: Path = self.ck_path / README_FILENAME

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _ensure_ck_dir(self) -> None:
        self.ck_path.mkdir(parents=True, exist_ok=True)

    def _write_if_missing(self, path: Path, content: str, label: str,
                          *, echo: bool = True, printer=print) -> bool:
        if not path.exists():
            # Atomic even on first creation so a crash mid-init can
            # never leave a torn template file.
            _atomic_write_text(path, content)
            if echo:
                printer(f"\U0001f4dd Created: {label}")
            return True
        if echo:
            printer(f"\u2139\ufe0f Exists: {label}")
        return False

    def _check_tools(self) -> dict[str, bool]:
        py_ok = sys.version_info >= (3, 8)
        tools = {
            "python3.8+": py_ok,
            "git": shutil.which("git") is not None,
            "fzf": shutil.which("fzf") is not None,
            "jq": shutil.which("jq") is not None,
        }
        if not py_ok:
            v = sys.version_info
            print(
                f"\u26a0\ufe0f  Warning: Python {v.major}.{v.minor} detected. "
                "Python 3.8+ recommended."
            )
        return tools

    def _rotate_history(self) -> None:
        """Archive HISTORY.md when it exceeds the entry limit.

        The rotation is performed under ``file_lock`` on the history
        file and is *gapless*: the new (trimmed) content is written to
        a temp file FIRST, then the old file is renamed to the
        timestamped archive, then the temp file is moved into place.
        A crash at any point leaves either the old or the new file
        present — never a missing HISTORY.md.

        Entry counting and tail extraction match only column-0
        ``### <date>`` headings, so ``###`` text inside note bodies
        or code fences can never trigger a false rotation or corrupt
        the preserved tail.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive = self.ck_path / f"HISTORY_{ts}.md.bak"
        with file_lock(self.history_file):
            content = self.history_file.read_text(encoding="utf-8")
            last_entry = _last_history_entry(content)
            header = f"# History {self.root.name}\nArchive: {archive.name}\n\n"
            new_text = header + last_entry
            # Write the replacement content to a temp file first so
            # there is never a window where HISTORY.md is absent.
            # fsync before the renames so a crash cannot leave an
            # empty renamed file.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.ck_path), prefix=".ck-history-", suffix=".tmp"
            )
            try:
                try:
                    mode = os.stat(self.history_file).st_mode & 0o777
                except OSError:
                    mode = 0o644
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new_text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp_name, mode)
                self.history_file.rename(archive)
                os.replace(tmp_name, self.history_file)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        print(
            f"\U0001f5c4 History reached limit ({HISTORY_LIMIT} entries) "
            "and was archived. Context preserved."
        )

    def _read_state(self) -> dict:
        if not self.state_file.exists():
            return {}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, state: dict) -> None:
        """Atomic-replace write of ``state.json`` under ``file_lock``.

        Concurrent ``ck`` invocations in the same project serialise on
        ``.ck/state.json.lock``; the tmp+rename writer guarantees
        readers never observe a torn file.
        """
        self.ck_path.mkdir(parents=True, exist_ok=True)
        text = json.dumps(state, indent=2, ensure_ascii=False)
        with file_lock(self.state_file):
            _atomic_write_text(self.state_file, text)

    # ------------------------------------------------------------------ #
    # Gitignore helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _detect_blanket_ignore(gitignore_text: str) -> bool:
        """Return True if the file contains a blanket ignore rule."""
        for line in gitignore_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for pat in _BLANKET_IGNORE_PATTERNS:
                if pat.match(line):
                    return True
        return False

    @staticmethod
    def _ensure_root_gitignore(root: Path) -> list[str]:
        """Append local-only entries and tracked-file negations.

        If the existing root ``.gitignore`` contains a blanket ignore
        rule (``*``, ``.``, ``?*``), this method *additionally*
        appends ``!``-negations for tracked Context Keeper files
        (PLAN.md, prompt.md, README.md) so they remain visible to Git.

        Existing entries are detected by EXACT LINE match — a
        substring check would false-suppress when the entry text
        merely appears inside a comment (e.g. ``# .ck/state.json``)
        or as part of a longer pattern.
        """
        gi = root / ".gitignore"
        additions: list[str] = []
        if gi.exists():
            current = gi.read_text(encoding="utf-8")
        else:
            current = ""
            gi.touch()

        # Exact-line set: strip trailing whitespace only (gitignore
        # semantics), keep the line content verbatim.
        existing_lines = {ln.rstrip() for ln in current.splitlines()}

        for entry in LOCAL_GITIGNORE_ENTRIES:
            if entry not in existing_lines:
                additions.append(entry)

        # Negations: required when a blanket ignore rule is present.
        blanket = ContextKeeper._detect_blanket_ignore(current)
        negations: list[str] = []
        if blanket:
            for tracked in TRACKED_GITIGNORE_PROTECTIONS:
                negation = f"!{tracked}"
                if negation not in existing_lines:
                    negations.append(negation)

        if additions or negations:
            block_lines: list[str] = ["", "# Context Keeper"]
            block_lines.extend(additions)
            block_lines.extend(negations)
            block = "\n".join(block_lines) + "\n"
            with gi.open("a", encoding="utf-8") as fh:
                fh.write(block)
        return additions + negations

    @staticmethod
    def _ensure_ck_gitignore(ck_path: Path) -> None:
        """Ensure ``.ck/.gitignore`` exists with the default content."""
        gi = ck_path / GITIGNORE_FILENAME
        if not gi.exists():
            gi.write_text(DEFAULT_CK_GITIGNORE, encoding="utf-8")

    # ------------------------------------------------------------------ #
    # AST-based mutations
    # ------------------------------------------------------------------ #

    def _load_plan(self) -> TaskList:
        """Parse PLAN.md with AUTO-REPAIR.

        Corrupted content (pasted ANSI noise, orphaned control
        sequences) is cleaned out and atomically persisted back to
        disk on load — every mutating command self-heals the file as
        part of its read-modify-write pass. Returns an empty
        TaskList if the file is missing.
        """
        tl, _repaired = load_repaired_plan(self.plan_file)
        return tl

    def _commit_plan(self, task_list: TaskList) -> None:
        """Normalize (opt-in), then atomically write PLAN.md under lock.

        The lock serialises concurrent read-modify-write cycles (two
        ``ck add`` invocations in different terminals) so one task
        can't be silently lost. Note we take the lock only for the
        write; the caller's read+mutate is short-lived enough that
        the fail-closed lock prevents torn interleavings.
        """
        normalize_plan(task_list)
        text = render_plan(task_list)
        with file_lock(self.plan_file):
            _atomic_write_text(self.plan_file, text)

    def add_task(self, task_text: str) -> int:
        """Insert a new ``[ ] title`` task into PLAN.md and return its ID.

        The title is sanitized (ANSI escapes, control characters,
        and whitespace runs are stripped) so terminal output pasted
        into the CLI can never corrupt PLAN.md. The task is inserted
        at the end of ``## Current Sprint`` when that section exists,
        otherwise directly before ``## Completed``.
        """
        title = sanitize_task_text(task_text)
        if not title:
            raise ValueError("Task text is empty")

        self._ensure_ck_dir()

        if not self.plan_file.exists():
            tl = TaskList(source_text="")
            tl.add(title, status=TaskStatus.OPEN)
            self._commit_plan(tl)
            return 1

        tl = self._load_plan()
        section = _section_for_new_task(tl)
        insert_line = _sprint_insert_line(tl)
        new_task = tl.add(
            title, status=TaskStatus.OPEN, section=section,
            insert_line=insert_line,
        )

        # ``render_plan`` will place any task whose ``line_number``
        # exceeds the original source length just before the
        # ``## Completed`` header (or at EOF if none). We don't need
        # to manipulate line numbers manually.
        self._commit_plan(tl)
        # Adding the first task to an empty plan changes the active
        # task — keep the registry pointer consistent.
        self._sync_active_task(tl)
        return new_task.id

    def start(self, task_id: int) -> FocusResult:
        """Mark the given task as focused; demote any other focused task."""
        tl = self._load_plan()
        target = tl.focus(task_id)
        self._commit_plan(tl)
        self._sync_active_task(tl)
        return FocusResult(task_id=target.id, title=target.title, path=self.plan_file)

    def done(self, spec: str) -> list[int]:
        """Mark one or more tasks as DONE.

        ``spec`` accepts single IDs (``3``), ranges (``2-4``), and
        comma-separated lists (``1,3,5``).
        """
        ids = _parse_id_spec(spec)
        if not ids:
            raise ValueError(f"No valid task IDs in spec: {spec!r}")

        tl = self._load_plan()
        transitioned = tl.toggle_done(ids)
        if transitioned:
            self._commit_plan(tl)
            # Completing the focused task changes which task is
            # active; the registry pointer must follow (task IDs are
            # positional and can shift, so re-derive from the AST).
            self._sync_active_task(tl)
        return transitioned

    def _sync_active_task(self, tl: TaskList) -> None:
        """Reconcile the registry's active-task pointer with the AST.

        Task IDs are positional: they shift when tasks are removed,
        completed, or the file is edited externally. A stale
        ``active_task_id`` in the registry would then point at the
        WRONG task. Re-derive the active task from the AST (focused
        first, else first open) and update the registry whenever it
        differs from what is stored.
        """
        active = tl.active()
        registry.update_active_task(
            self.root,
            task_id=active.id if active is not None else None,
            task_title=active.title if active is not None else "",
        )

    def edit_note(self, task_id: int, note: str) -> None:
        """Append a note to HISTORY.md for ``task_id``.

        The note is durably persisted to HISTORY.md (as a dated
        entry) rather than mutating an in-memory AST that was
        silently discarded — the old behaviour was a no-op that
        lost the caller's data.
        """
        tl = self._load_plan()
        target = tl.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = Notes(
            task_id=task_id,
            task_title=target.title,
            timestamp=ts,
            comment=note,
            body="",
        )
        self._ensure_ck_dir()
        with file_lock(self.history_file):
            with self.history_file.open("a", encoding="utf-8") as fh:
                fh.write(entry.render())

    # ------------------------------------------------------------------ #
    # COMMAND: init
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        """Initialize the project structure, registry, and gitignore rules."""
        # Boundary guard: initializing in $HOME, /tmp, or the
        # filesystem root affects every command run beneath it.
        if not self.ck_path.exists():
            warn_if_sensitive_root(self.root)
        self._ensure_ck_dir()

        state = self._read_state()
        state.update({
            "version": VERSION,
            "project_name": self.root.name,
            "created_at": state.get("created_at") or datetime.now().isoformat(),
            "last_update": datetime.now().isoformat(),
            "current_step": state.get("current_step") or "Init",
            "tools": self._check_tools(),
        })
        self._write_state(state)

        added = self._ensure_root_gitignore(self.root)
        if added:
            print(f"\U0001f4dd Root .gitignore updated: +{', +'.join(added)}")

        self._write_if_missing(
            self.plan_file,
            DEFAULT_PLAN.format(project_name=self.root.name),
            PLAN_FILENAME,
        )
        self._write_if_missing(
            self.prompt_file,
            DEFAULT_PROMPT,
            PROMPT_FILENAME,
        )
        self._write_if_missing(
            self.readme_file,
            _default_readme(self.root.name),
            README_FILENAME,
        )
        self._ensure_ck_gitignore(self.ck_path)

        registry.register_project(self.root, name=self.root.name)
        print(f"\u2705 Context Keeper v{VERSION} initialized at {self.root}")

    # ------------------------------------------------------------------ #
    # COMMAND: status / dashboard
    # ------------------------------------------------------------------ #

    def status(self) -> str:
        """Return the single-project status block as a string."""
        tl = parse_plan_file(self.plan_file)
        return _render_local_status(self, tl, gith)

    def tasks(self) -> str:
        """Return the current project's task list for STDOUT.

        A clean, pipe-friendly rendering (no editor, no decorations
        that would confuse grep/cut): one line per task as
        ``[<status marker>] <id>. <title>``, grouped by section with
        ``##``-style headers. Suitable for ``ck tasks | grep ...``.
        """
        tl = load_repaired_plan(self.plan_file)[0]
        return _render_tasks_listing(tl)

    def dashboard(self) -> str:
        """Return the cross-project dashboard as a string."""
        return _render_dashboard(
            self,
            list_projects=registry.list_projects,
            parse_plan_file=_safe_parse_plan,
        )

    # ------------------------------------------------------------------ #
    # COMMAND: register / unregister / prune (global registry)
    # ------------------------------------------------------------------ #

    def register(self, path: Optional[Path] = None,
                 name: Optional[str] = None) -> "registry.ProjectEntry":
        """Register a project in the global registry.

        ``path`` defaults to ``self.root`` (the current project). The
        project's last-seen timestamp is refreshed.
        """
        target = Path(path).resolve() if path is not None else self.root.resolve()
        project_name = name or target.name
        return registry.register_project(target, name=project_name)

    def unregister(self, path: Optional[Path] = None,
                   name: Optional[str] = None) -> bool:
        """Remove a project from the global registry.

        Resolution order:
        1. ``path`` (if given)
        2. ``name`` (matches the registry ``name`` field)
        3. ``self.root`` (default: current working project)

        Returns True if anything was removed. Succeeds gracefully
        when the path is not in the registry or the folder is
        missing from disk.
        """
        if path is not None:
            return registry.remove_project(Path(path))
        if name is not None:
            return registry.remove_project_by_name(name)
        return registry.remove_project(self.root)

    def prune(self) -> list[str]:
        """Drop registry entries whose folders are gone from disk."""
        return registry.prune_missing()

    # ------------------------------------------------------------------ #
    # COMMAND: save (two-step Notes + local commit)
    # ------------------------------------------------------------------ #

    def save(self, *, input_fn=input, stdin_read=sys.stdin.read,
             printer=print) -> Optional[str]:
        """Two-step save: optional local-history note, then optional Git commit.

        Step 1 appends a dated entry to ``.ck/HISTORY.md`` — Context
        Keeper's own plain-text context log. This is LOCAL history
        and involves no Git whatsoever. Step 2 optionally creates a
        LOCAL Git commit of the ck artifacts. Prompts and
        confirmations explicitly label which step is which so a user
        can never mistake a local history entry for a Git commit (or
        vice versa).

        If Git is unavailable and the user declines initialization,
        the commit phase is bypassed gracefully — no subprocess
        errors, no prompts past that point.
        """
        if not self.state_file.exists():
            printer("\u274c Run `ck init` first.")
            return None

        tl = parse_plan_file(self.plan_file)
        active = tl.active()
        if active is None:
            printer("\u26a0\ufe0f  No active task to save.")
            return None

        task_id, task_title = active.id, active.title
        history_rel = f"{CK_DIR_NAME}/{HISTORY_FILENAME}"
        printer(f"\U0001f4cd Active task: [{task_id}] {task_title}")

        note_recorded = False
        try:
            # ---- Step 1: LOCAL HISTORY (HISTORY.md, no Git) ------ #
            printer("")
            printer(
                f"\U0001f4cb Step 1: local history \u2014 append entry to "
                f"{history_rel} (not a Git commit)"
            )
            body = self._prompt_note(input_fn, printer)

            comment = input_fn(
                "Short summary (saved with the history entry; "
                "reused as default commit subject): "
            ).strip() or "update"

            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            note = Notes(
                task_id=task_id,
                task_title=task_title,
                timestamp=ts,
                comment=comment,
                body=body,
            )
            # Append the note and evaluate rotation under the same lock
            # so concurrent ``ck save`` invocations serialise (no
            # interleaved partial note blocks, no double rotation).
            # Entry counting matches only anchored "### <date>" headings —
            # "### " inside note bodies/code fences is not an entry.
            with file_lock(self.history_file):
                with self.history_file.open("a", encoding="utf-8") as fh:
                    fh.write(note.render())
                history_text = self.history_file.read_text(encoding="utf-8")
                needs_rotation = (
                    _count_history_entries(history_text) > HISTORY_LIMIT
                )
            note_recorded = True
            printer(
                f"\u2705 Saved entry to {history_rel} "
                "(local context history \u2014 not a Git commit)."
            )

            state = self._read_state()
            state["last_update"] = datetime.now().isoformat()
            state["current_step"] = task_title
            self._write_state(state)

            if needs_rotation:
                self._rotate_history()

            # ---- Step 2: LOCAL GIT COMMIT (separate from history) - #
            printer("")
            printer(
                "\U0001f4e6 Step 2: Git commit \u2014 optional, "
                "separate from local history"
            )
            if not gith.is_git_repo(self.root):
                ans = input_fn(
                    "\u2049\ufe0f  Not a Git repo. Initialize one for "
                    "local commits? (y/N): "
                ).strip().lower()
                if ans != "y":
                    printer(
                        f"\u2139\ufe0f  No commit created. Your entry is "
                        f"saved in {history_rel} (local history only)."
                    )
                    return None
                if not gith.init_repo(self.root):
                    printer(
                        f"\u274c Git init failed; commit skipped "
                        f"(entry stays saved in {history_rel})."
                    )
                    return None

            default_msg = f"{task_title}: {comment}".strip(": ")
            custom = input_fn(
                "\U0001f680 Git commit message [Enter=accept / type custom]: "
            ).strip()
            msg = custom or default_msg

            if input_fn(
                f"\u2705 Create LOCAL Git commit \"{msg}\"? (y/N): "
            ).strip().lower() != "y":
                printer(
                    f"\u2139\ufe0f  No commit created. Your entry is saved "
                    f"in {history_rel} (local history only)."
                )
                return None
        except EOFError:
            # Non-interactive stdin ran out mid-flow. Abort cleanly:
            # whatever was durably written (note) stays; the commit
            # phase simply never runs.
            if note_recorded:
                printer(
                    f"\u274c Input closed \u2014 entry saved to "
                    f"{history_rel}, no Git commit created."
                )
            else:
                printer("\u274c Input closed \u2014 save aborted (nothing written).")
            return None

        # Stage ONLY the Context Keeper artifacts (PLAN.md, HISTORY.md,
        # prompt.md, README.md, .ck/.gitignore). Never ``git add .`` —
        # that would sweep unrelated untracked files (potentially
        # secrets) into the commit — and never stage the whole .ck/
        # directory, which would include transient *.lock files.
        # Pathspecs are cwd-relative, so they resolve correctly even
        # when the Git work tree root is a parent of the project.
        ck_artifacts = [
            f"{CK_DIR_NAME}/{PLAN_FILENAME}",
            f"{CK_DIR_NAME}/{HISTORY_FILENAME}",
            f"{CK_DIR_NAME}/{PROMPT_FILENAME}",
            f"{CK_DIR_NAME}/{README_FILENAME}",
            f"{CK_DIR_NAME}/{GITIGNORE_FILENAME}",
        ]
        stage = [p for p in ck_artifacts if (self.root / p).exists()]
        if gith.local_commit(msg, path=self.root, stage=stage):
            printer("\U0001f4e4 Git commit created (local only \u2014 no push).")
            return msg
        printer(
            f"\u26a0\ufe0f  Commit failed (entry remains saved in {history_rel})."
        )
        return None

    def _prompt_note(self, input_fn: Callable[[str], str],
                     printer=print) -> str:
        """Prompt for a note body. Returns stripped body or ""."""
        skip = input_fn(
            "\U0001f4dd Add a note for this task? [s=skip / e=editor / Enter=type]: "
        ).strip().lower()
        if skip == "s" or skip == "skip":
            return ""
        if skip == "e" or skip == "editor":
            tmp = self.ck_path / ".ck_note.tmp"
            tmp.write_text("", encoding="utf-8")
            try:
                subprocess.run([get_editor(self.root), str(tmp)], check=False)
                return tmp.read_text(encoding="utf-8").strip()
            finally:
                tmp.unlink(missing_ok=True)

        printer("Enter note text (empty line to finish):")
        lines: list[str] = []
        try:
            while True:
                line = input_fn("> ")
                if not line:
                    break
                lines.append(line)
        except EOFError:
            pass
        return "\n".join(lines).strip()

    # ------------------------------------------------------------------ #
    # COMMAND: edit / log (open in editor)
    # ------------------------------------------------------------------ #

    def edit_plan(self) -> None:
        subprocess.run([get_editor(self.root), str(self.plan_file)], check=False)

    def edit_log(self) -> None:
        subprocess.run([get_editor(self.root), str(self.history_file)], check=False)

    # ------------------------------------------------------------------ #
    # COMMAND: update (git pull --ff-only)                                #
    # ------------------------------------------------------------------ #

    @property
    def install_dir(self) -> Path:
        """Directory containing the ``cklib`` package — the one to ``git pull``."""
        return Path(__file__).resolve().parent.parent

    def update(self, *, remote: str = "origin",
               branch: str = DEFAULT_REPO_BRANCH,
               timeout: float = 30.0) -> "UpdateResult":
        """Self-update via ``git fetch`` + ``git pull --ff-only``.

        Returns an :class:`UpdateResult` describing what happened.
        Never raises for routine update outcomes; only for programmer
        errors (invalid remote/branch).
        """
        repo_dir = self.install_dir
        if not gith.is_git_repo(repo_dir):
            return UpdateResult(
                ok=False,
                message="Error: ContextKeeper was not installed via Git. "
                        "Updates require a git clone setup.",
                action="abort",
            )

        if gith.is_dirty(repo_dir):
            return UpdateResult(
                ok=False,
                message="Local changes detected. Please stash or commit "
                        "your changes before updating.",
                action="abort",
            )

        if not gith.fetch(remote, branch, path=repo_dir, timeout=timeout):
            return UpdateResult(
                ok=False,
                message=f"Failed to fetch {remote}/{branch}.",
                action="fetch-failed",
            )

        ok, msg = gith.pull_ff_only(remote, branch, path=repo_dir,
                                    timeout=timeout)
        if not ok:
            return UpdateResult(
                ok=False,
                message=msg or f"git pull --ff-only {remote}/{branch} failed.",
                action="pull-failed",
            )

        # Update the global state with the latest execution timestamp.
        try:
            _write_global_state_timestamp("last_update_check")
            _write_global_state_timestamp("last_update_success")
        except OSError:
            pass

        return UpdateResult(
            ok=True,
            message=msg or f"Updated to {remote}/{branch}.",
            action="updated",
        )

    # ------------------------------------------------------------------ #
    # Non-blocking daily update notifier                                   #
    # ------------------------------------------------------------------ #

    def maybe_notify_update(self, *, now: Optional[datetime] = None,
                            fetch_fn=None,
                            stderr=None) -> bool:
        """Print a one-line notice to ``stderr`` when an update is
        available, at most once every ``UPDATE_CHECK_INTERVAL_HOURS``.

        - Reads ``~/.config/ck/state.json`` to find the previous
          check timestamp.
        - If the interval has elapsed, performs a fast non-blocking
          ``ls-remote`` to find ``origin/main``'s tip.
        - Always refreshes ``last_update_check`` regardless of
          network outcome.
        - Returns True if a notice was emitted.

        The function never raises; failures are swallowed and the
        timestamp is still updated. ``stderr`` defaults to the
        current ``sys.stderr`` at call time (lazy).
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if stderr is None:
            stderr = sys.stderr
        state = _read_global_state()
        last_iso = state.get("last_update_check", "")
        due = True
        if last_iso:
            try:
                last_dt = datetime.fromisoformat(last_iso)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                due = (now - last_dt) > timedelta(
                    hours=UPDATE_CHECK_INTERVAL_HOURS
                )
            except (ValueError, TypeError):
                due = True

        if not due:
            return False

        # Refresh timestamp regardless of network outcome.
        _write_global_state_timestamp("last_update_check", now=now)

        # Lazy import of fetch_fn to keep tests easy.
        check = fetch_fn or _default_remote_head_check
        try:
            result = check(self.install_dir)
        except Exception:
            return False

        if result is None:
            return False
        local_sha, remote_sha = result
        if local_sha and remote_sha and local_sha != remote_sha:
            try:
                print(
                    "Notice: A new version of ck is available. "
                    "Run 'ck update' to upgrade.",
                    file=stderr,
                )
            except OSError:
                pass
            return True
        return False

    def info(self) -> str:
        """Diagnostic information about the installation."""
        lines: list[str] = []
        lines.append(f"ck version: {VERSION}")
        lines.append(f"install_dir: {self.install_dir}")
        in_repo = gith.is_git_repo(self.install_dir)
        lines.append(f"in_git_repo: {in_repo}")
        if in_repo:
            br = gith.current_branch(self.install_dir) or "?"
            sha = gith.head_sha(self.install_dir) or "?"
            lines.append(f"branch: {br}")
            lines.append(f"head: {sha[:12]}")
        lines.append(f"user_install_path: {USER_INSTALL_PATH}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- #
# Update notifier helpers
# ---------------------------------------------------------------------- #


def _read_global_state() -> dict:
    """Best-effort read of ``~/.config/ck/state.json``."""
    try:
        if GLOBAL_STATE_FILE.exists():
            return json.loads(GLOBAL_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_global_state_timestamp(key: str,
                                  *, now: Optional[datetime] = None) -> None:
    """Write a single timestamp key to ``~/.config/ck/state.json``.

    Atomic-replace; creates the parent directory if needed. Data is
    fsync'ed before rename; existing permissions are preserved; a
    symlinked state file is resolved so the link survives.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    real = GLOBAL_STATE_FILE
    try:
        if GLOBAL_STATE_FILE.is_symlink():
            real = GLOBAL_STATE_FILE.resolve()
    except OSError:
        real = GLOBAL_STATE_FILE
    real.parent.mkdir(parents=True, exist_ok=True)
    state: dict = {}
    if real.exists():
        try:
            state = json.loads(real.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    if not isinstance(state, dict):
        state = {}
    state[key] = now.isoformat()
    # Atomic write
    fd, tmp = tempfile.mkstemp(
        dir=str(real.parent), prefix=".ck-state-", suffix=".tmp"
    )
    try:
        try:
            mode = os.stat(real).st_mode & 0o777
        except OSError:
            mode = 0o644
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _default_remote_head_check(repo_dir: Path
                               ) -> Optional[Tuple[str, str]]:
    """Read-only check: compare local HEAD with its tracked upstream.

    Resolves the CURRENT branch's upstream (``@{upstream}``) rather
    than assuming ``origin/main`` — fork/feature-branch installs no
    longer produce perpetual false "update available" notices. Falls
    back to ``origin/<current-branch>`` when no upstream is tracked.

    The network call is capped at a short deadline so a slow network
    cannot stall interactive commands for the full 5s.
    """
    local = gith.head_sha(repo_dir)
    if not local:
        return None
    branch = gith.current_branch(repo_dir)
    if not branch or branch.startswith("-"):
        return None
    if not shutil.which("git"):
        return None

    # Prefer the branch's configured upstream (e.g. fork/feature).
    remote_ref: Optional[str] = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--symbolic-full-name",
             f"{branch}@{{upstream}}"],
            capture_output=True, text=True, timeout=5.0, check=False,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            remote_ref = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        remote_ref = None
    if not remote_ref or remote_ref.startswith("-"):
        remote_ref = f"refs/remotes/origin/{branch}"

    # Map the tracking ref to (remote, branch) for ls-remote. The
    # tracking ref looks like refs/remotes/<remote>/<branch>.
    remote_name, ls_branch = "origin", branch
    if remote_ref.startswith("refs/remotes/"):
        parts = remote_ref[len("refs/remotes/"):].split("/", 1)
        if len(parts) == 2 and parts[0] and not parts[0].startswith("-") \
                and parts[1] and not parts[1].startswith("-"):
            remote_name, ls_branch = parts[0], parts[1]

    try:
        result = subprocess.run(
            ["git", "ls-remote", remote_name, ls_branch],
            capture_output=True, text=True, timeout=2.0, check=False,
            cwd=str(repo_dir),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return None
    parts = line[0].split()
    if len(parts) < 1:
        return None
    remote = parts[0].strip()
    return (local, remote)


@dataclass
class UpdateResult:
    """Outcome of a :meth:`ContextKeeper.update` invocation."""

    ok: bool
    message: str
    action: str  # "abort" | "fetch-failed" | "pull-failed" | "updated"

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------- #
# AST placement helpers (no string juggling)
# ---------------------------------------------------------------------- #

def _section_for_new_task(tl: TaskList) -> str:
    """Pick a sensible section for a brand-new OPEN task.

    Strategy: hang the new task under the most recent section that
    is not the ``Completed`` section.
    """
    for sec in reversed(tl.sections):
        if sec.title.lower() == "completed":
            continue
        return sec.title
    return ""


def _sprint_insert_line(tl: TaskList) -> Optional[int]:
    """1-based source line where ``ck add`` should insert a new task.

    Resolution order (mirrors the spec for clean insertion):

    1. End of the ``## Current Sprint`` section (after its last
       content line, before the next header).
    2. Directly before the ``## Completed`` header.
    3. ``None`` → the renderer's default: before ``## Completed``
       or at EOF.
    """
    src_lines = tl.source_text.splitlines()
    # Fast path: no document → let the renderer default handle it.
    if not src_lines:
        return None
    sprint_end = _find_section_end(src_lines, "Current Sprint")
    if sprint_end is not None:
        return sprint_end + 1  # 0-based → 1-based
    completed = _find_completed_line(src_lines)
    if completed is not None:
        return completed + 1  # insert at (before) the Completed header
    return None


# ---------------------------------------------------------------------- #
# Presentation helpers
# ---------------------------------------------------------------------- #

def _render_local_status(ck: ContextKeeper, tl: TaskList,
                         gith_mod) -> str:
    lines: list[str] = []
    bar = "\u2550" * 45
    lines.append("")
    lines.append(bar)
    lines.append(f" \U0001f680 PROJECT: {ck.root.name} [v{VERSION}]")

    # Tri-state task view: Previous / Focus / Next.
    _append_tri_state(lines, tl)

    if gith_mod.is_git_repo(ck.root):
        br = gith_mod.current_branch(ck.root) or "unknown"
        lines.append(f" \U0001f33f BRANCH: {br}")

    pct = tl.completion_pct
    total, done_count = tl.total, len(tl.done)
    left = total - done_count
    lines.append(
        f" \U0001f4ca PROGRESS: {done_count}/{total} done "
        f"({pct}%) - left: {left}"
    )

    gap_ids = tl.gap_ids()
    if gap_ids:
        lines.append(
            f" \u26a0\ufe0f  GAPS detected: {_collapse_ids(gap_ids)}"
        )

    tools = [t for t, ok in (ck._check_tools()).items() if ok]
    if tools:
        lines.append(f" \U0001f6e0\ufe0f  TOOLS: {', '.join(tools)}")

    lines.append(bar)

    if ck.history_file.exists():
        recent = _recent_history(ck.history_file, count=3)
        if recent:
            lines.append("")
            lines.append(" \U0001f4dc RECENT NOTES:")
            for entry in recent:
                lines.extend(_format_history_entry(entry))

    lines.append("\u2550" * 25 + " [end] " + "\u2550" * 11 + "\n")
    return "\n".join(lines)


def _render_tasks_listing(tl: TaskList) -> str:
    """Render the local task list for ``ck tasks`` (STDOUT output).

    Format (grep/cut-friendly, no editor involved):

    ::

        # <project tasks>            (omitted when no sections exist)
        ## Current Sprint
        [ ] 1. first open task
        [>] 2. focused task
        [x] 3. done task
        ## Completed
        [x] 4. old done task

    Status markers mirror PLAN.md syntax: ``[ ]`` open, ``[>]``
    focused, ``[x]`` done. Empty plans render a single hint line.
    """
    if not tl.tasks:
        return "No tasks. Add one with `ck add <text>`."

    lines: list[str] = []
    current_section = object()  # sentinel: "no section yet"
    for t in tl.tasks:
        if t.section and t.section != current_section:
            lines.append(f"## {t.section}")
            current_section = t.section
        marker = t.status.canonical_marker
        lines.append(f"[{marker}] {t.id}. {t.title}")
    return "\n".join(lines)


def _append_tri_state(lines: list, tl: TaskList) -> None:
    """Previous / Focus / Next tri-state view of the task list.

    - PREVIOUS: the most recently completed task (last ``[x]``).
    - FOCUS: the focused task (``[>]``), else the first open task.
    - NEXT: the first open task that follows the FOCUS task.
    """
    previous = tl.done[-1] if tl.done else None
    focus = tl.active()
    next_task = None
    if focus is not None:
        for i, t in enumerate(tl.tasks):
            if t.id == focus.id:
                next_task = next(
                    (u for u in tl.tasks[i + 1:]
                     if u.status == TaskStatus.OPEN),
                    None,
                )
                break

    if previous is not None:
        lines.append(f" \u25c0 PREVIOUS: [{previous.id}] {previous.title}")
    else:
        lines.append(" \u25c0 PREVIOUS: --")
    if focus is not None:
        marker = "[>] " if focus.status == TaskStatus.FOCUSED else ""
        lines.append(f" \U0001f3af FOCUS:    [{focus.id}] {marker}{focus.title}")
    else:
        lines.append(" \U0001f3af FOCUS:    --")
    if next_task is not None:
        lines.append(f" \u25b6 NEXT:      [{next_task.id}] {next_task.title}")
    else:
        lines.append(" \u25b6 NEXT:      --")


def _collapse_ids(ids: list) -> str:
    """Collapse consecutive ID runs into ranges.

    ``[3, 4, 5, 6, 7, 8]`` → ``"3-8"``; ``[3, 4, 8]`` → ``"3-4, 8"``;
    single IDs stay as-is. Input is deduplicated and sorted
    defensively.
    """
    ordered = sorted(set(ids))
    if not ordered:
        return ""
    runs: list = []
    start = prev = ordered[0]
    for i in ordered[1:]:
        if i == prev + 1:
            prev = i
        else:
            runs.append((start, prev))
            start = prev = i
    runs.append((start, prev))
    return ", ".join(
        str(a) if a == b else f"{a}-{b}" for a, b in runs
    )


def _safe_parse_plan(plan_path: Path) -> "TaskList | None":
    """Defensive wrapper around ``parse_plan_file`` for the dashboard.

    Returns ``None`` on any I/O / parse error so the dashboard never
    crashes for a single corrupt project.
    """
    try:
        if not plan_path.exists():
            return None
        return parse_plan_file(plan_path)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _truncate(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` columns with an ellipsis."""
    if width <= 1:
        return text[:width]
    if len(text) <= width:
        return text
    return "..." + text[-(width - 3):]


def _truncate_ellipsis(text: str, width: int) -> str:
    """Fit ``text`` into ``width`` columns with a middle ellipsis.

    Used by the dashboard table: long paths and focus-task titles
    keep both their start and their end (the informative parts) —
    e.g. ``/very/long/path/to/some/project`` → ``/very/lo…oject``.
    Never returns a string longer than ``width``.
    """
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"[:width] if width >= 0 else text
    half = (width - 1) // 2
    keep_end = width - 1 - half
    return f"{text[:half]}…{text[len(text) - keep_end:]}"


def _relative_time(iso: str, *, now: Optional[datetime] = None) -> str:
    """Format ``iso`` as a human-friendly relative timestamp.

    Examples: "just now", "5 minutes ago", "2 hours ago", "3 days ago",
    "2025-12-04 11:30" (for > 30 days). Returns "unknown" if the
    input is unparseable.
    """
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "unknown"
    if dt.tzinfo is None:
        # Treat naive as UTC (registry convention).
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    delta = ref - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        # Future timestamp (clock skew) — display absolute.
        return dt.strftime("%Y-%m-%d %H:%M")
    if secs < 5:
        return "just now"
    if secs < 60:
        return f"{secs} seconds ago"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return dt.strftime("%Y-%m-%d %H:%M")


def _render_dashboard(ck: Optional[ContextKeeper], *, list_projects,
                      parse_plan_file) -> str:
    """Render the global dashboard as an ASCII table.

    Columns: Project, Path, Last Active, Focus Task, Status.
    """
    entries = list_projects()
    if not entries:
        return (
            "\U0001f4ed Global Dashboard\n"
            "\u2139\ufe0f  No registered projects. Run `ck register` "
            "in a project directory to begin.\n"
        )

    cwd = None
    if ck is not None:
        try:
            cwd = str(Path(ck.root).resolve())
        except (OSError, ValueError):
            cwd = None

    # ---- collect rows -------------------------------------------------
    rows: list[dict] = []
    for entry in entries:
        path = Path(entry.path)
        on_disk = path.exists()
        plan_path = path / CK_DIR_NAME / PLAN_FILENAME
        tl_local = parse_plan_file(plan_path) if on_disk else None

        if not on_disk:
            project_display = entry.name
            path_display = entry.path
            last_active = _relative_time(entry.last_seen)
            focus_display = "n/a"
            status_display = "missing"
        elif tl_local is None:
            # Folder exists but PLAN.md is missing or unparseable.
            project_display = entry.name
            if cwd and entry.path == cwd:
                project_display = f"{entry.name}  \u2190 active"
            path_display = entry.path
            last_active = _relative_time(entry.last_seen)
            focus_display = "n/a"
            status_display = "corrupt"
        else:
            project_display = entry.name
            if cwd and entry.path == cwd:
                project_display = f"{entry.name}  \u2190 active"
            path_display = entry.path
            last_active = _relative_time(entry.last_seen)
            if tl_local.focused:
                t = tl_local.focused[0]
                focus_display = f"[{t.id}] [>] {t.title}"
            elif tl_local.active() is not None:
                t = tl_local.active()
                focus_display = f"[{t.id}] {t.title}"
            else:
                focus_display = "none"
            open_count = len(tl_local.open)
            done_count = len(tl_local.done)
            status_display = f"{open_count} open, {done_count} done"

        rows.append({
            "project": project_display,
            "path": path_display,
            "last": last_active,
            "focus": focus_display,
            "status": status_display,
        })

    # ---- column widths -----------------------------------------------
    # Hard total-width cap (~85 chars) so the table never wraps in a
    # standard terminal. Fixed-width columns keep their natural size;
    # the flexible columns (Path, Focus Task) absorb the squeeze via
    # middle-ellipsis truncation, never below their header length.
    headers = ("Project", "Path", "Last Active", "Focus Task", "Status")
    col_keys = ("project", "path", "last", "focus", "status")
    _MAX_TABLE_WIDTH = 85
    _CHROME = 2 * 5 + 6  # 2-space padding per column + '|' separators

    natural: dict[str, int] = {}
    for h, k in zip(headers, col_keys):
        natural[k] = max(len(h), *(len(r[k]) for r in rows))

    widths: dict[str, int] = dict(natural)
    overflow = sum(widths.values()) + _CHROME - _MAX_TABLE_WIDTH
    if overflow > 0:
        # Phase 1: squeeze the flexible columns (Path, Focus Task)
        # down to their header lengths.
        for k in ("path", "focus"):
            give = min(overflow, widths[k] - len(headers[col_keys.index(k)]))
            if give > 0:
                widths[k] -= give
                overflow -= give
    if overflow > 0:
        # Phase 2: extreme data (very long project names) — squeeze
        # the widest columns further, floor of 3 chars each, so the
        # hard width cap always holds.
        for k in sorted(col_keys, key=lambda k: -widths[k]):
            give = min(overflow, widths[k] - 3)
            if give > 0:
                widths[k] -= give
                overflow -= give

    def _hr() -> str:
        line = "+" + "+".join("-" * (widths[k] + 2) for k in col_keys) + "+"
        return line

    def _row(values: tuple[str, ...]) -> str:
        cells = [
            f" {_truncate_ellipsis(values[i], widths[col_keys[i]]).ljust(widths[col_keys[i]])} "
            for i in range(5)
        ]
        return "|" + "|".join(cells) + "|"

    out: list[str] = []
    out.append("\U0001f4ed GLOBAL DASHBOARD")
    out.append(_hr())
    out.append(_row(headers))
    out.append(_hr())
    for r in rows:
        out.append(_row((
            r["project"], r["path"], r["last"], r["focus"], r["status"],
        )))
    out.append(_hr())
    return "\n".join(out)


def _recent_history(path: Path, *, count: int) -> list[str]:
    content = path.read_text(encoding="utf-8")
    parts = re.split(r"\n(?=### )", content)
    if parts and not parts[0].strip().startswith("###"):
        parts.pop(0)
    return [p.strip() for p in parts[-count:]]


def _format_history_entry(entry: str) -> list[str]:
    out: list[str] = []
    lines = entry.splitlines()
    if not lines:
        return out
    out.append(f"  {lines[0].replace('### ', '\U0001f539 ')}")
    in_code = False
    for line in lines[1:]:
        clean = line.strip()
        if clean.startswith("```"):
            in_code = not in_code
            out.append(f"    {'\u250c' if in_code else '\u2514'}{'\u2500' * 40}")
            continue
        if in_code:
            out.append(f"    \u2502 {clean}")
        elif clean:
            out.append(f"    {clean}")
    out.append("")
    return out


# ---------------------------------------------------------------------- #
# ID spec parsing
# ---------------------------------------------------------------------- #

def _parse_id_spec(spec: str) -> list[int]:
    """Parse IDs/ranges/lists: '3', '2-4', '1,3,5', '1-2,4,6-7'."""
    ids: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                continue
            step = 1 if lo_i <= hi_i else -1
            ids.extend(range(lo_i, hi_i + step, step))
        else:
            try:
                ids.append(int(chunk))
            except ValueError:
                continue
    seen: set[int] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


# ---------------------------------------------------------------------- #
# Default README
# ---------------------------------------------------------------------- #

def _default_readme(project_name: str) -> str:
    return (
        f"# {project_name}\n\n"
        "Context Keeper project directory. See [README.md](../../README.md) "
        "for full documentation.\n"
    )


__all__ = [
    "ContextKeeper",
    "FocusResult",
    "UpdateResult",
    "_parse_id_spec",
    "TRACKED_GITIGNORE_PROTECTIONS",
    "GLOBAL_REGISTRY_FILE",
    "GLOBAL_STATE_FILE",
    "LEGACY_GLOBAL_CONFIG_FILE",
    "_detect_blanket_ignore",
]