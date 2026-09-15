"""Automated sandbox fixture generation (``ck-dev sandbox setup``).

Builds a fully isolated mock environment under
``<repo_root>/.sandbox/`` so dev sessions and upcoming features
(history limits, archive inspection, auto-rotation / cleanup
routines, ``[MISSING]`` hygiene, ``ck prune``) can be exercised
against bulk, deterministic data — without ever touching the host's
real Context Keeper state (``~/.config/ck/projects.json`` or real
project trees).

Isolation is guaranteed BY CONSTRUCTION: every path written here is
derived from :func:`cklib.sandbox.sandbox_root` — no write can
escape ``.sandbox/``. Re-running ``setup`` rebuilds the fixtures
from scratch (the sandbox is disposable by design); ``sandbox
clean`` removes the whole tree.

Fixture layout (Step 4.1 spec)::

    .sandbox/
    ├── config/
    │   └── projects.json        # registry mock
    └── projects/
        ├── alpha/               # active, REGISTERED, bulk data target
        │   └── .ck/
        │       ├── PLAN.md      # tasks 1,2,3,4,6,7 (gap at 5)
        │       ├── HISTORY.md   # exactly HISTORY_LIMIT entries
        │       ├── history.log  # 120 archived entries (bulk)
        │       ├── archive/     # 5 mock archive files
        │       ├── HISTORY_*.md.bak   (2 legacy rotation artifacts)
        │       └── … templates (prompt.md, README.md, .gitignore,
        │                       state.json)
        ├── beta/                # initialized but NOT registered
        │   └── .ck/…
        └── gamma/               # plain directory, no .ck/

``projects/orphaned-deleted`` is INTENTIONALLY absent from disk but
present in the registry mock — exercising ``[MISSING]`` status tags
and ``ck prune``.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import (
    CK_DIR_NAME,
    DEFAULT_CK_GITIGNORE,
    DEFAULT_PROMPT,
    GITIGNORE_FILENAME,
    HISTORY_FILENAME,
    HISTORY_LIMIT,
    PLAN_FILENAME,
    PROMPT_FILENAME,
    README_FILENAME,
    STATE_FILENAME,
    VERSION,
)
from .sandbox import (
    CONFIG_SUBDIR,
    PROJECTS_SUBDIR,
    clean_sandbox,
    sandbox_root,
)

# --------------------------------------------------------------------------- #
# Fixture constants
# --------------------------------------------------------------------------- #

ALPHA_PROJECT = "alpha"
BETA_PROJECT = "beta"
GAMMA_PROJECT = "gamma"
ORPHANED_PROJECT = "orphaned-deleted"

# Bulk data volumes (Step 4.1: "100+ archived history entries").
BULK_HISTORY_LOG_ENTRIES = 120
ARCHIVE_FILE_COUNT = 5
LEGACY_ARCHIVE_COUNT = 2

# New-style archive home and the consolidated bulk log file.
ARCHIVE_SUBDIR = "archive"
HISTORY_LOG_FILENAME = "history.log"

# Alpha task numbering: the deliberate GAP at 5 exercises gap
# detection (both the positional done→open detector and any routine
# correlating task IDs with title numbering).
ALPHA_TASK_REFS = (1, 2, 3, 4, 6, 7)
ALPHA_TITLES = {
    1: "Completed task 1",
    2: "Completed task 2",
    3: "Active focus task",
    4: "Pending task 4",
    6: "Pending task 6",
    7: "Pending task 7",
}

ALPHA_PLAN = f"""# {ALPHA_PROJECT}

## Current Sprint
- [x] {ALPHA_TITLES[1]}
- [x] {ALPHA_TITLES[2]}
- [>] {ALPHA_TITLES[3]}
- [ ] {ALPHA_TITLES[4]}
- [ ] {ALPHA_TITLES[6]}
- [ ] {ALPHA_TITLES[7]}

## Completed
"""

BETA_PLAN = f"""# {BETA_PROJECT}

## Current Sprint
- [ ] Beta standalone task one
- [ ] Beta standalone task two

## Completed
"""


# --------------------------------------------------------------------------- #
# Entry rendering (HISTORY.md / Notes.render-compatible)
# --------------------------------------------------------------------------- #


def _render_entry(stamp: str, task_id: int, title: str, comment: str,
                  body: str = "") -> str:
    """One HISTORY.md-style entry (same shape as ``Notes.render``)."""
    out = f"\n### {stamp} | [{task_id}] {title}\n- {comment}\n"
    if body:
        out += f"\n```text\n{body}\n```\n"
    return out


def _render_history_md(now: datetime) -> str:
    """Current HISTORY.md for alpha — EXACTLY at the rotation limit.

    With ``HISTORY_LIMIT`` entries, the very next ``ck save`` append
    pushes the count over the limit and must trigger the
    auto-rotation routine — the boundary the fixtures exist to test.
    """
    parts = [f"# History {ALPHA_PROJECT}\n"]
    comments = (
        "Refactored parser hot path",
        "Fixed dashboard column widths",
        "Wrote regression tests for rotation",
        "Reviewed archive cleanup routine",
        "Prepared bulk fixture generation",
    )
    for i, comment in enumerate(comments):
        stamp = (now - timedelta(hours=2 * (len(comments) - i))
                 ).strftime("%Y-%m-%d %H:%M")
        task_id = ALPHA_TASK_REFS[i % len(ALPHA_TASK_REFS)]
        parts.append(_render_entry(
            stamp, task_id, ALPHA_TITLES[task_id], comment,
        ))
    return "".join(parts)


def _render_history_log(now: datetime) -> str:
    """``history.log`` — the consolidated bulk archive log (120 lines).

    One entry per line, oldest first (chronological append order):
    ``<timestamp> | [<task_id>] <title> | <comment>``. Task refs
    cycle over the alpha plan (1,2,3,4,6,7 — the gap at 5 never
    appears), so log-parsing routines can be validated against the
    plan fixture.
    """
    total_span = timedelta(days=45)
    step = total_span / BULK_HISTORY_LOG_ENTRIES
    start = now - total_span
    lines = [
        f"# {ALPHA_PROJECT} — archived history log "
        "(generated by `ck sandbox setup`)",
        "# format: <timestamp> | [<task_id>] <task_title> | <comment>",
    ]
    for i in range(1, BULK_HISTORY_LOG_ENTRIES + 1):
        stamp = (start + step * i).strftime("%Y-%m-%d %H:%M")
        task_id = ALPHA_TASK_REFS[i % len(ALPHA_TASK_REFS)]
        lines.append(
            f"{stamp} | [{task_id}] {ALPHA_TITLES[task_id]} "
            f"| bulk archived entry #{i:03d}"
        )
    return "\n".join(lines) + "\n"


def _render_archive(now: datetime, *, days_ago: int, entry_count: int,
                    clock: str = "104112") -> tuple[str, str]:
    """One mock rotation archive: ``(filename, content)``.

    Filenames mirror the rotation pattern
    (``HISTORY_<YYYYMMDD>_<HHMMSS>.md.bak``); content is a full
    HISTORY.md-style document. ``entry_count`` deliberately straddles
    the history limit (below / at / above) so limit checks and
    cleanup routines have boundary data.
    """
    ts = (now - timedelta(days=days_ago)).strftime(f"%Y%m%d_{clock}")
    name = f"HISTORY_{ts}.md.bak"
    parts = [f"# History {ALPHA_PROJECT}\nArchive: {name}\n"]
    for i in range(entry_count):
        stamp = (now - timedelta(days=days_ago, hours=i + 1)
                 ).strftime("%Y-%m-%d %H:%M")
        task_id = ALPHA_TASK_REFS[i % len(ALPHA_TASK_REFS)]
        parts.append(_render_entry(
            stamp, task_id, ALPHA_TITLES[task_id],
            f"archived rotation entry {i + 1}/{entry_count}",
        ))
    return name, "".join(parts)


def _archive_specs() -> list[tuple[int, int]]:
    """(days_ago, entry_count) per archive file — 5 files in
    ``.ck/archive/`` with counts below/at/above ``HISTORY_LIMIT``."""
    return [
        (33, 3),   # below limit
        (26, HISTORY_LIMIT),   # at limit
        (19, HISTORY_LIMIT),   # at limit
        (12, 8),   # above limit
        (5, 12),   # well above limit
    ]


def _legacy_archive_specs() -> list[tuple[int, int]]:
    """Legacy rotation artifacts stored directly in ``.ck/`` (the
    current rotation layout) — data for cleanup routines that must
    handle both layouts."""
    return [(47, 6), (40, HISTORY_LIMIT)]


def _mock_state(project_name: str, now: datetime) -> dict:
    """A plausible ``state.json`` payload (mirrors ``ck init``)."""
    return {
        "version": VERSION,
        "project_name": project_name,
        "created_at": (now - timedelta(days=45)).isoformat(),
        "last_update": (now - timedelta(hours=1)).isoformat(),
        "current_step": (
            ALPHA_TITLES[3] if project_name == ALPHA_PROJECT else "Init"
        ),
        "tools": {
            "python3.8+": sys.version_info >= (3, 8),
            "git": shutil.which("git") is not None,
            "fzf": shutil.which("fzf") is not None,
            "jq": shutil.which("jq") is not None,
        },
    }


def _project_readme(project_name: str) -> str:
    return (
        f"# {project_name}\n\n"
        "Sandbox fixture project generated by `ck sandbox setup`.\n"
        "Safe to mutate: everything lives under `.sandbox/`.\n"
    )


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def _build_registry_mock(config_dir: Path, projects_dir: Path,
                         now: datetime) -> Path:
    """``.sandbox/config/projects.json`` — the global registry mock.

    Registers ``alpha`` (exists on disk) and ``orphaned-deleted``
    (deliberately NOT created on disk). ``beta`` and ``gamma`` stay
    unregistered. Written as plain JSON — the file lives inside the
    sandbox, so no interception is involved (or possible).
    """
    entries = [
        {
            "name": ALPHA_PROJECT,
            "path": str((projects_dir / ALPHA_PROJECT).resolve()),
            "registered_at": (now - timedelta(days=21)).isoformat(),
            "last_seen": (now - timedelta(hours=1)).isoformat(),
            "active_task_id": 3,
            "active_task_title": ALPHA_TITLES[3],
        },
        {
            "name": ORPHANED_PROJECT,
            "path": str((projects_dir / ORPHANED_PROJECT).resolve()),
            "registered_at": (now - timedelta(days=35)).isoformat(),
            "last_seen": (now - timedelta(days=12)).isoformat(),
            "active_task_id": None,
            "active_task_title": "",
        },
    ]
    target = config_dir / "projects.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"projects": entries}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def _build_project_alpha(projects_dir: Path, now: datetime) -> Path:
    """``projects/alpha`` — registered, active, bulk data target."""
    root = projects_dir / ALPHA_PROJECT
    ck = root / CK_DIR_NAME
    ck.mkdir(parents=True, exist_ok=True)

    (ck / PLAN_FILENAME).write_text(ALPHA_PLAN, encoding="utf-8")
    (ck / HISTORY_FILENAME).write_text(
        _render_history_md(now), encoding="utf-8")
    (ck / HISTORY_LOG_FILENAME).write_text(
        _render_history_log(now), encoding="utf-8")

    archive_dir = ck / ARCHIVE_SUBDIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    for days_ago, count in _archive_specs():
        name, content = _render_archive(
            now, days_ago=days_ago, entry_count=count)
        (archive_dir / name).write_text(content, encoding="utf-8")
    for days_ago, count in _legacy_archive_specs():
        name, content = _render_archive(
            now, days_ago=days_ago, entry_count=count, clock="173045")
        (ck / name).write_text(content, encoding="utf-8")

    (ck / PROMPT_FILENAME).write_text(DEFAULT_PROMPT, encoding="utf-8")
    (ck / README_FILENAME).write_text(
        _project_readme(ALPHA_PROJECT), encoding="utf-8")
    (ck / GITIGNORE_FILENAME).write_text(
        DEFAULT_CK_GITIGNORE, encoding="utf-8")
    (ck / STATE_FILENAME).write_text(
        json.dumps(_mock_state(ALPHA_PROJECT, now), indent=2,
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def _build_project_beta(projects_dir: Path, now: datetime) -> Path:
    """``projects/beta`` — initialized but NOT registered (tests
    ``ck init`` without ``--register`` and standalone ``ck register``).
    """
    root = projects_dir / BETA_PROJECT
    ck = root / CK_DIR_NAME
    ck.mkdir(parents=True, exist_ok=True)

    (ck / PLAN_FILENAME).write_text(BETA_PLAN, encoding="utf-8")
    (ck / PROMPT_FILENAME).write_text(DEFAULT_PROMPT, encoding="utf-8")
    (ck / README_FILENAME).write_text(
        _project_readme(BETA_PROJECT), encoding="utf-8")
    (ck / GITIGNORE_FILENAME).write_text(
        DEFAULT_CK_GITIGNORE, encoding="utf-8")
    (ck / STATE_FILENAME).write_text(
        json.dumps(_mock_state(BETA_PROJECT, now), indent=2,
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def _build_project_gamma(projects_dir: Path) -> Path:
    """``projects/gamma`` — plain directory with NO ``.ck/`` (tests
    CLI behavior in non-ck environments)."""
    root = projects_dir / GAMMA_PROJECT
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def setup_sandbox(*, printer: Callable[[str], None] = print) -> bool:
    """Build the isolated mock environment in ``.sandbox/``.

    Rebuilds the sandbox from scratch (any previous sandbox content
    is removed first — it is disposable by design). All writes stay
    inside ``sandbox_root()``; the host's real registry and project
    trees are never read or modified.

    Returns True on success; OSError failures are reported to stderr
    and return False.
    """
    root = sandbox_root()
    now = datetime.now(timezone.utc)
    try:
        clean_sandbox(quiet=True)
        config_dir = root / CONFIG_SUBDIR
        projects_dir = root / PROJECTS_SUBDIR
        projects_dir.mkdir(parents=True, exist_ok=True)

        _build_registry_mock(config_dir, projects_dir, now)
        _build_project_alpha(projects_dir, now)
        _build_project_beta(projects_dir, now)
        _build_project_gamma(projects_dir)
    except OSError as e:
        print(
            f"[sandbox setup] Failed to build sandbox at {root}: {e}",
            file=sys.stderr,
        )
        return False

    printer(f"✅ Sandbox mock environment built at {root}")
    printer(
        f"   ├─ config/projects.json — registered: {ALPHA_PROJECT}, "
        f"{ORPHANED_PROJECT} (registry-only)"
    )
    printer(
        f"   ├─ projects/{ALPHA_PROJECT} — active: 6 tasks (gap at 5), "
        f"{BULK_HISTORY_LOG_ENTRIES} history.log entries, "
        f"{ARCHIVE_FILE_COUNT}+{LEGACY_ARCHIVE_COUNT} archives"
    )
    printer(f"   ├─ projects/{BETA_PROJECT} — initialized, NOT registered")
    printer(f"   ├─ projects/{GAMMA_PROJECT} — plain dir (no .ck/)")
    printer(
        f"   └─ projects/{ORPHANED_PROJECT} — missing on disk → "
        "[MISSING] tag / ck prune target"
    )
    printer("Next: CK_SANDBOX=1 ./ck-dev list -g")
    return True


__all__ = [
    "ALPHA_PROJECT",
    "BETA_PROJECT",
    "GAMMA_PROJECT",
    "ORPHANED_PROJECT",
    "BULK_HISTORY_LOG_ENTRIES",
    "ARCHIVE_FILE_COUNT",
    "LEGACY_ARCHIVE_COUNT",
    "ARCHIVE_SUBDIR",
    "HISTORY_LOG_FILENAME",
    "ALPHA_PLAN",
    "BETA_PLAN",
    "setup_sandbox",
]
