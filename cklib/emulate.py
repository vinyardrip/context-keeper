"""Interactive history-rotation stress emulator (``ck dev emulate``).

A developer-only, sandbox-guarded demonstration that stress-tests the
history machinery end to end inside a dedicated
``.sandbox/projects/sandbox_stress`` project:

- forced rotation via a LOCAL ``.ck.json`` config override
  (``HISTORY_LIMIT`` / ``MAX_BAK_FILES`` / ``COMPRESS_ARCHIVES``),
- gzip archive creation (``HISTORY_*.md.gz``),
- FIFO retention (``MAX_BAK_FILES``) purging the oldest archives,
- full-history viewing via ``ck log --all``.

Everything is written under ``sandbox_root()`` — never outside it —
so the emulator can never touch real user projects.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from . import sandbox as _sandbox_mod
from .config import file_lock
from .history import (
    archive_name,
    compress_to,
    fifo_cleanup,
    list_history_archives,
)

# The dedicated emulation project (isolated from the alpha/beta
# fixtures and from real user projects).
STRESS_PROJECT = "sandbox_stress"

# Emulation profile: tiny limits so every cycle rotates.
STRESS_HISTORY_LIMIT = 5
STRESS_MAX_BAK_FILES = 2
STRESS_COMPRESS_ARCHIVES = True

# Pacing between the demo steps (seconds).
STEP_PAUSE = 1.7

# Number of full generate → rotate cycles and entries per batch.
CYCLES = 3
TASKS_PER_CYCLE = 3

Printer = Callable[[str], None]


def stress_project_dir() -> Path:
    """Root of the dedicated emulation project under the sandbox.

    Resolves ``sandbox_root()`` dynamically (module attribute lookup)
    so test overrides of :func:`cklib.sandbox.sandbox_root` apply to
    the guard AND the emulator alike — they can never disagree about
    where the sandbox is.
    """
    return _sandbox_mod.sandbox_root() / "projects" / STRESS_PROJECT


# --------------------------------------------------------------------------- #
# Local config override (.ck.json)
# --------------------------------------------------------------------------- #

def _write_local_config(ck_dir: Path, printer: Printer) -> None:
    """Write the LOCAL ``.ck.json`` override for the stress project."""
    config_path = ck_dir / ".ck.json"
    config = {
        "HISTORY_LIMIT": STRESS_HISTORY_LIMIT,
        "MAX_BAK_FILES": STRESS_MAX_BAK_FILES,
        "COMPRESS_ARCHIVES": STRESS_COMPRESS_ARCHIVES,
    }
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    printer(
        f"[config] {config_path}: HISTORY_LIMIT="
        f"{STRESS_HISTORY_LIMIT}, MAX_BAK_FILES="
        f"{STRESS_MAX_BAK_FILES}, COMPRESS_ARCHIVES="
        f"{STRESS_COMPRESS_ARCHIVES}"
    )


# --------------------------------------------------------------------------- #
# Cycle steps
# --------------------------------------------------------------------------- #

def _generate_batch(
    ck_dir: Path, cycle: int, batch: int, printer: Printer,
) -> int:
    """Append ``TASKS_PER_CYCLE`` entries to the stress HISTORY.md.

    Same locking protocol and entry shape as production history
    writes. Returns the entry count AFTER the append.
    """
    history = ck_dir / "HISTORY.md"
    with file_lock(history):
        existing = (
            history.read_text(encoding="utf-8")
            if history.exists()
            else f"# History {STRESS_PROJECT}\n"
        )
        count = sum(
            1 for ln in existing.splitlines() if ln.startswith("### ")
        )
        addition = ""
        for i in range(TASKS_PER_CYCLE):
            ordinal = (cycle - 1) * TASKS_PER_CYCLE + i + 1
            task_id = ordinal % 6 + 1
            stamp = time.strftime("%Y-%m-%d %H:%M")
            addition += (
                f"\n### {stamp} | [{task_id}] stress task {ordinal}\n"
                f"- cycle {cycle} batch {batch} entry "
                f"{i + 1}/{TASKS_PER_CYCLE}\n"
            )
        history.write_text(
            existing.rstrip("\n") + "\n" + addition, encoding="utf-8"
        )
    return count + TASKS_PER_CYCLE


def _rotate_into_archive(
    ck_dir: Path, printer: Printer,
) -> Optional[Path]:
    """Rotate HISTORY.md into a compressed archive (production rules).

    Preserves the newest entry as the fresh-file tail, gzips the rest
    via Python's built-in ``gzip`` module (staged rename — gapless),
    then enforces FIFO retention at ``MAX_BAK_FILES``. Returns the
    created archive path, or ``None`` when there is nothing to archive.
    """
    history = ck_dir / "HISTORY.md"
    now = datetime.now()
    with file_lock(history):
        content = (
            history.read_text(encoding="utf-8")
            if history.exists() else ""
        )
        tail_start = content.rfind("\n### ") + 1
        if tail_start == 0 and not content.lstrip().startswith("### "):
            printer("[warn] no entries to archive — skipping rotation")
            return None
        # Same-second collision dedupe (matches production naming).
        seq = 0
        while (
            (ck_dir / archive_name(now, compressed=False, seq=seq)).exists()
            or (ck_dir / archive_name(now, compressed=True, seq=seq)).exists()
        ):
            seq += 1
        plain = ck_dir / archive_name(now, compressed=False, seq=seq)
        gz = ck_dir / archive_name(now, compressed=True, seq=seq)

        tail = content[tail_start:].rstrip("\n")
        archive_text = content[:tail_start].rstrip("\n")
        header_line = content.split("\n", 1)[0] if content.strip() \
            else f"# History {STRESS_PROJECT}"
        fresh = f"{header_line}\nArchive: {gz.name}\n\n{tail}\n"

        # Stage the fresh file first, then swap — never a window
        # where HISTORY.md is missing (same contract as production).
        tmp = ck_dir / ".ck-emulate-fresh.tmp"
        try:
            tmp.write_text(fresh, encoding="utf-8")
            plain.write_text(archive_text + "\n", encoding="utf-8")
            compress_to(plain, gz)
            plain.unlink()
            os.replace(tmp, history)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        pruned = fifo_cleanup(ck_dir, STRESS_MAX_BAK_FILES)
    if pruned:
        printer(
            f"[fifo] purged {len(pruned)} oldest archive(s): "
            + ", ".join(pruned)
        )
    return gz


# --------------------------------------------------------------------------- #
# The interactive scenario
# --------------------------------------------------------------------------- #

def run_emulation(
    *, cycles: int = CYCLES, pause: float = STEP_PAUSE,
    sleep: Optional[Callable[[float], None]] = None,
    printer: Printer = print,
) -> int:
    """Run the interactive stress scenario. Returns a process exit code.

    ``sleep`` defaults to :func:`time.sleep` resolved at CALL time
    (not import time), so patching ``cklib.emulate.time.sleep`` in
    tests takes effect on the CLI dispatch path too.
    """
    if sleep is None:
        sleep = time.sleep
    project_dir = stress_project_dir()
    ck_dir = project_dir / ".ck"
    ck_dir.mkdir(parents=True, exist_ok=True)
    history = ck_dir / "HISTORY.md"
    if not history.exists():
        history.write_text(
            f"# History {STRESS_PROJECT}\n", encoding="utf-8"
        )
    _write_local_config(ck_dir, printer)

    printer("")
    printer("╔════════════════════════════════════════════════════╗")
    printer("║   History Rotation Stress Emulator (dev only)      ║")
    printer("╚════════════════════════════════════════════════════╝")
    printer(f"[info] Target project : {project_dir}")
    printer("")

    for cycle in range(1, cycles + 1):
        printer(f"━━━ Cycle {cycle}/{cycles} " + "━" * 34)

        # -- Step A: generate tasks + history entries ---------------- #
        printer(f"[info] Generating task batch for cycle {cycle}...")
        count = _generate_batch(ck_dir, cycle, cycle, printer)
        printer(f"[info] HISTORY.md now holds {count} entries")

        # -- Step B: pause before the rotation phase ----------------- #
        sleep(pause)
        printer("[info] HISTORY.md threshold reached "
                f"(> {STRESS_HISTORY_LIMIT} entries)")

        # -- Step C: pre-archive state, rotate, report --------------- #
        printer("[info] Pre-archive state:")
        pre = sorted(
            p for p in ck_dir.glob("HISTORY_*") if p.name != "HISTORY.md"
        )
        if pre:
            for p in pre:
                printer(f"         {p.name} ({p.stat().st_size} B)")
        else:
            printer("         (no archives yet)")
        gz = _rotate_into_archive(ck_dir, printer)
        if gz is not None:
            printer(
                f"[+] Rotated HISTORY.md -> {gz.name} "
                f"({gz.stat().st_size} B gzipped)"
            )

        # -- Step D: pause before the next cycle --------------------- #
        sleep(pause)

    # -- Final panel -------------------------------------------------- #
    archives: List[Path] = list_history_archives(ck_dir)
    printer("")
    printer("╔════════════════════════════════════════════════════╗")
    printer("║   Emulation complete — how to inspect the results  ║")
    printer("╚════════════════════════════════════════════════════╝")
    printer(f"[info] Archives retained in {ck_dir}:")
    for p in archives:
        size = p.stat().st_size
        if p.name.endswith(".gz"):
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                raw = len(fh.read().encode("utf-8"))
            printer(f"         {p.name}  ({size} B gz, {raw} B raw)")
        else:
            printer(f"         {p.name}  ({size} B)")
    printer("")
    printer("Next steps:")
    printer("  1. View the FULL merged history (archives + live file):")
    printer(f"       cd {project_dir} && ck log --all")
    printer(f"  2. Inspect the .md.gz archives directly in {ck_dir}")
    printer("  3. Clean up when done: ck dev clean")
    printer("")
    return 0


__all__ = [
    "STRESS_PROJECT",
    "STRESS_HISTORY_LIMIT",
    "STRESS_MAX_BAK_FILES",
    "STRESS_COMPRESS_ARCHIVES",
    "stress_project_dir",
    "run_emulation",
]
