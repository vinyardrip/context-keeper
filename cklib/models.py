"""Domain model: tasks, lists, notes, status enums.

The model layer holds both the AST node for a parsed file (with line
numbers and section context) and the operations that mutations
operate on. Mutations never touch raw text — they go through the AST
and a deterministic full-file ``render_plan``.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from . import sequence, notes as note_store

# UUID alphabet: lowercase letters + digits (the spec's example IDs
# such as ``a1b2c3d4`` / ``e5f6g7h8`` are 8-char lowercase
# alphanumeric tokens — base36, not plain hex).
_UUID_ALPHABET = string.ascii_lowercase + string.digits
_UUID_LENGTH = 8


def new_task_uuid() -> str:
    """A fresh 8-char lowercase alphanumeric stable task reference.

    Cryptographically random (``secrets``) so two tasks can never
    collide in practice; the value is written once, as a hidden HTML
    comment on the task line, and never regenerated afterwards.
    """
    return "".join(secrets.choice(_UUID_ALPHABET) for _ in range(_UUID_LENGTH))


def note_stamp(created: str) -> str:
    """Human stamp for an inline note: ``YYYY-MM-DD HH:MM UTC``.

    Unparseable/empty inputs render as-is (or empty), so legacy
    values never crash the renderer.
    """
    if not isinstance(created, str) or not created:
        return ""
    try:
        parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return created
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def history_timestamp(now: Optional[datetime] = None) -> str:
    """The completion timestamp written into HISTORY.md lines."""
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


@dataclass
class HistoryEntry:
    """One completed task parsed from HISTORY.md (v0.13.0).

    ``timestamp`` keeps the ``YYYY-MM-DD HH:MM`` completion stamp,
    ``title`` the task text, and ``uuid`` the stable reference that
    links the entry back to the (long-gone) plan position.
    """

    timestamp: str
    title: str
    uuid: str = ""
    notes: list = field(default_factory=list)

    def to_line(self) -> str:
        """Render as ``- [x] <ts>: <title> <!-- id: <uuid> -->``."""
        line = f"- [x] {self.timestamp}: {self.title}".rstrip()
        if self.uuid:
            line += f" <!-- id: {self.uuid} -->"
        return line


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """The four canonical task states (v0.13.0).

    ``OPEN`` / ``FOCUSED`` / ``PAUSED`` are the ACTIVE states: they
    live in ``PLAN.md``. ``DONE`` is terminal: a completed task moves
    to ``HISTORY.md`` with a timestamp, so PLAN.md holds work only.
    """

    OPEN = "open"
    FOCUSED = "focused"
    PAUSED = "paused"
    DONE = "done"

    @property
    def canonical_marker(self) -> str:
        """Canonical CommonMark marker — ``[ ]`` / ``[>]`` / ``[~]`` / ``[x]``.

        This is what the writer emits; the parser also accepts the
        legacy no-space form ``[]`` for backward compatibility.
        """
        return {
            TaskStatus.OPEN: " ",       # "- [ ] title"
            TaskStatus.FOCUSED: ">",    # "- [>] title"
            TaskStatus.PAUSED: "~",     # "- [~] title"
            TaskStatus.DONE: "x",       # "- [x] title"
        }[self]

    @property
    def legacy_marker(self) -> str:
        """Legacy CommonMark-less marker — empty string for OPEN."""
        return {
            TaskStatus.OPEN: "",        # "- [] title"
            TaskStatus.FOCUSED: ">",    # "- [>] title"
            TaskStatus.PAUSED: "~",     # "- [~] title"
            TaskStatus.DONE: "x",       # "- [x] title"
        }[self]

    @classmethod
    def from_marker(cls, marker: str) -> "TaskStatus":
        m = (marker or "").strip().lower()
        if m in {"", " "}:
            return cls.OPEN
        if m == ">":
            return cls.FOCUSED
        if m == "~":
            return cls.PAUSED
        if m in {"x"}:
            return cls.DONE
        raise ValueError(f"Unknown task marker: {marker!r}")

    @property
    def is_active(self) -> bool:
        """True for the statuses that live in PLAN.md (not ``[x]``)."""
        return self is not TaskStatus.DONE


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------


class CompletedTaskError(PermissionError):
    """A reordering command targeted a COMPLETED (``[x]``) task.

    Reordering is restricted to pending/active work (``[ ]`` /
    ``[>]``); a completed task records history and its position is
    part of that record. Carries ``task_id`` so callers can report the
    exact offender without re-parsing the message.
    """

    def __init__(self, task_id: int) -> None:
        super().__init__(f"Task #{task_id} is completed")
        self.task_id = task_id


@dataclass
class Task:
    """A single AST task node.

    ``status`` is the canonical state. ``marker_raw`` stores the literal
    text found inside ``[...]`` on disk (preserving legacy vs canonical
    distinctions for read-only purposes); ``line_number`` is the 1-based
    source line. ``section`` is the most recent ``##`` header text.

    ``uuid`` is the STABLE task reference (v0.13.0): an 8-character
    lowercase alphanumeric token rendered as a hidden HTML comment
    (``<!-- id: a1b2c3d4 -->``) at the end of the task line. Numeric IDs
    are positional and shift on every deletion/re-index; the UUID never
    changes, so agents and external tooling can address a task across
    renumbering. An empty string means "no UUID yet" (legacy content) —
    the renderer simply omits the comment until one is assigned.
    """

    id: int
    status: TaskStatus
    title: str
    line_number: int
    marker_raw: str
    section: str = ""
    notes: list = field(default_factory=list)
    legacy_syntax: bool = False
    uuid: str = ""
    # Multiline task description (v0.13.4), stored INLINE as indented
    # ``> text`` lines directly beneath the task bullet:
    #
    #     - [ ] title <!-- id: a1b2c3d4 -->
    #       > First description line
    #       > Second line
    #
    # Joined with newlines on the AST node; an empty string means "no
    # description" (the renderer emits nothing).
    description: str = ""
    # 1-based source position where a NEW task should be inserted
    # (end of the ``## Current Sprint`` section). Parsed tasks never
    # carry this hint; it routes the renderer through the INSERT
    # pass instead of substitution / before-Completed append.
    insert_line: Optional[int] = None

    # ---- rendering ----

    def to_line(self) -> str:
        """Render to canonical CommonMark (``- [ ]`` / ``- [>]`` / ...).

        The stable UUID is appended as a hidden HTML comment so the
        reference survives renumbering:
        ``- [ ] title <!-- id: a1b2c3d4 -->``.
        """
        line = f"- [{self.status.canonical_marker}] {self.title}".rstrip()
        if self.uuid:
            line += f" <!-- id: {self.uuid} -->"
        return line

    def to_legacy_line(self) -> str:
        """Render using legacy no-space marker for ``OPEN`` tasks only.

        Used only when explicitly serializing in legacy form. The
        default writer emits canonical CommonMark.
        """
        marker = self.status.legacy_marker
        if self.status == TaskStatus.OPEN:
            return f"- [{marker}] {self.title}".rstrip()
        return self.to_line()

    @property
    def rendered_marker(self) -> str:
        return f"[{self.status.canonical_marker}]"


@dataclass
class Section:
    """A ``## Heading`` in PLAN.md. Preserved across render cycles."""

    title: str
    level: int
    line_number: int


@dataclass
class TaskList:
    """AST root: ordered tasks plus the structural sections around them.

    The model is fully mutable (mutations operate on these nodes) and
    rendering is deterministic — see :func:`cklib.parser.render_plan`.

    ``newline`` records the source document's dominant line ending
    (``"\\r\\n"`` or ``"\\n"``) so the renderer can preserve the
    file's existing format. Empty string means "unspecified" (the
    renderer falls back to ``"\\n"``).
    """

    tasks: list[Task] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    source_text: str = ""
    newline: str = ""
    # ORIGINAL 1-based source line numbers that a user-facing deletion
    # removed. The renderer's substitution pass only rewrites lines
    # that still OWN a task node; without this set a removed bullet's
    # original line would be preserved verbatim. Empty for parsed or
    # add-only ASTs, so existing rendering stays byte-identical.
    removed_lines: set[int] = field(default_factory=set)

    # ---- lookup ----

    def by_id(self, task_id: int) -> Optional[Task]:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    # ---- analytics ----

    @property
    def open(self) -> list[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.OPEN]

    @property
    def focused(self) -> list[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.FOCUSED]

    @property
    def paused(self) -> list[Task]:
        """Tasks whose focus was switched away (``[~]``), in doc order."""
        return [t for t in self.tasks if t.status == TaskStatus.PAUSED]

    @property
    def total(self) -> int:
        return len(self.tasks)

    def completion_pct(self, done_count: int = 0) -> float:
        """Percent of work done given an EXTERNAL completed count.

        v0.13.0 moved completed tasks into HISTORY.md, so the plan
        itself no longer carries ``[x]`` rows and cannot self-report
        its completion ratio. Callers pass the count of completed
        tasks read from HISTORY.md (projects) or counted in-file
        (spaces, which keep their own ``[x]`` rows).
        """
        if not self.tasks:
            return 0.0
        return round(100.0 * done_count / len(self.tasks), 1)

    def gap_ids(self) -> list[int]:
        """Detect literal missing numeric IDs in the task sequence.

        A "gap" is an integer in ``[min_id, max_id]`` that no task in
        this list carries — e.g. a ``1, 2, 4, 5`` sequence reports
        ``[3]``. Task STATUS is deliberately ignored: pending tasks
        that merely sit after a completed one are NOT gaps, and the
        parser assigns IDs sequentially (1..N) so an intact plan
        yields ``[]``.
        """
        if not self.tasks:
            return []
        ids = {t.id for t in self.tasks}
        lo, hi = min(ids), max(ids)
        return [i for i in range(lo, hi + 1) if i not in ids]

    def active(self) -> Optional[Task]:
        """Return the focused task, or the first open task, or None."""
        if self.focused:
            return self.focused[0]
        if self.open:
            return self.open[0]
        return None

    # ---- AST mutations (no string juggling) ----

    def add(self, title: str, status: TaskStatus = TaskStatus.OPEN,
            section: str = "", line_number: int | None = None,
            insert_line: int | None = None,
            uuid: str | None = None) -> Task:
        """Append a new task node and return it.

        New nodes are placed **past the end of the source document**
        so the renderer inserts them (never substitutes an existing
        source line such as a header or prose). Explicit
        ``line_number`` values that would collide with an existing
        task are rejected to guarantee the render is lossless.

        ``insert_line`` overrides that default: it routes the new
        task through the renderer's INSERT pass at the given 1-based
        source position (used by ``ck add`` to place the task at the
        end of ``## Current Sprint`` instead of just before
        ``## Completed``).

        ``uuid`` pins a specific stable reference (the migration
        preserves the task's original UUID); when omitted a fresh
        one is generated so every new task is addressable forever.
        """
        new_id = (max((t.id for t in self.tasks), default=0)) + 1
        if line_number is None:
            # Past EOF by construction: strictly greater than every
            # task's source line AND the total source length, so the
            # renderer routes it through the *insert* path instead of
            # overwriting an arbitrary source line.
            source_len = len(self.source_text.splitlines()) if self.source_text else 0
            line_number = max(
                max((t.line_number for t in self.tasks), default=0),
                source_len,
            ) + 1
        elif any(t.line_number == line_number for t in self.tasks):
            raise ValueError(
                f"line_number {line_number} already used by an "
                "existing task; refusing to create a render collision"
            )
        task = Task(
            id=new_id,
            status=status,
            title=title.strip(),
            line_number=line_number,
            marker_raw=status.canonical_marker.strip(),
            section=section,
            legacy_syntax=False,
            insert_line=insert_line,
            uuid=uuid if uuid is not None else new_task_uuid(),
        )
        self.tasks.append(task)
        return task

    def focus(self, task_id: int) -> Task:
        """Switch focus to ``task_id``; the old focus becomes PAUSED.

        v0.13.0 semantics: a focus SWITCH never discards the previous
        work context — the demoted task is marked ``[~]`` (paused) so
        it stays visible in the plan and can be resumed later.
        ``start 0`` / ``focus 0`` (the explicit reset path) demotes
        back to OPEN instead, which is handled by the callers.
        """
        target = self.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")
        for t in self.tasks:
            if t.status == TaskStatus.FOCUSED and t.id != task_id:
                t.status = TaskStatus.PAUSED
        target.status = TaskStatus.FOCUSED
        return target

    def toggle_done(self, task_ids: list[int]) -> list[int]:
        """Mark each ``task_ids`` as DONE. Returns the IDs transitioned."""
        valid = {t.id for t in self.tasks}
        unknown = [i for i in task_ids if i not in valid]
        if unknown:
            raise KeyError(f"Unknown task IDs: {unknown}")
        transitioned: list[int] = []
        for tid in task_ids:
            t = self.by_id(tid)
            if t is None or t.status == TaskStatus.DONE:
                continue
            t.status = TaskStatus.DONE
            transitioned.append(tid)
        return transitioned

    def edit_note(self, task_id: int, note: str) -> Task:
        """Append a note entry to a task's node (in-memory).

        v0.13.0 notes live INLINE in PLAN.md as timestamped
        ``* Note (...)`` lines. Entries are dicts ``{"text",
        "created"}`` — the same shape every note renderer consumes.
        """
        t = self.by_id(task_id)
        if t is None:
            raise KeyError(f"No task with id {task_id}")
        if note:
            t.notes.append({"text": note, "created": note_store.now_iso()})
        return t

    def remove(self, task_id: int) -> Optional[Task]:
        """Drop the task from the AST (IDs of the others are untouched).

        Raw removal deliberately leaves the remaining IDs as they
        were, which is what lossless byte-level tests rely on. A
        user-facing deletion must close the numeric gap afterwards —
        see :meth:`reindex`.
        """
        for i, t in enumerate(self.tasks):
            if t.id == task_id:
                return self.tasks.pop(i)
        return None

    def delete(self, task_id: int) -> Task:
        """Remove a task for a USER-FACING deletion and re-index.

        Unlike :meth:`remove` (a raw AST pop used by read-only gap
        analyses, which leaves the source line untouched), this also
        records the task's source line so the renderer DROPS it, then
        renumbers the remaining nodes 1..N in document order. Raises
        ``KeyError`` for an unknown ID.
        """
        task = self.remove(task_id)
        if task is None:
            raise KeyError(f"No task with id {task_id}")
        self.removed_lines.add(task.line_number)
        self.reindex()
        return task

    def reindex(self) -> None:
        """Renumber task IDs sequentially (1..N) in DOCUMENT order.

        Task IDs are positional — the parser assigns them 1..N while
        walking the file — so removing a task shifts every ID after
        it. Re-indexing keeps the AST in agreement with what a fresh
        parse of the written document would produce: no broken
        sequences, no duplicate IDs. Ordering is by ``line_number``
        (the rendered position), which is also the parser's order;
        sorting is stable for degenerate equal-line nodes.
        """
        for new_id, task in enumerate(
                sorted(self.tasks, key=lambda t: t.line_number), start=1):
            task.id = new_id

    # ---- reordering (active / pending tasks only) ----

    def reorderable_tasks(self) -> list[Task]:
        """The tasks a reorder may touch, in DOCUMENT order.

        ONLY non-DONE tasks qualify — ``[ ]`` (open) and ``[>]``
        (focused). Completed tasks are history: their position is the
        record of what happened, so the reordering commands refuse to
        move them at all.

        Sorted by ``line_number`` (the rendered position), NOT by
        ``self.tasks`` order: the AST list keeps its original parse
        order, so after one reorder the two diverge. "Position 1" must
        always mean the task the user SEES first, which is only true
        for the document order. A task still pending insertion
        (past-EOF ``line_number``) sorts last, matching where the
        renderer will place it.
        """
        return sorted((t for t in self.tasks if t.status != TaskStatus.DONE),
                      key=lambda t: t.line_number)

    def _slots(self) -> list[int]:
        """The source line numbers owned by the reorderable tasks."""
        return [t.line_number for t in self.reorderable_tasks()]

    def _assign(self, ordered: list[Task], slots: list[int]) -> None:
        """Hand each task in ``ordered`` the slot at its own index."""
        for task, slot in zip(ordered, slots):
            task.line_number = slot

    def _require_reorderable(self, task_id: int) -> Task:
        """Resolve ``task_id`` or raise the domain errors.

        ``KeyError`` for an unknown id; ``PermissionError`` for a
        completed task — reordering completed work is forbidden, not
        merely unsupported.
        """
        task = self.by_id(task_id)
        if task is None:
            raise KeyError(f"No task with id {task_id}")
        if task.status == TaskStatus.DONE:
            raise CompletedTaskError(task_id)
        return task

    def move_task(self, task_id: int, position: int) -> int:
        """Move ``task_id`` to ``position`` (1-based) among the
        reorderable tasks. Returns the resulting 1-based position.

        Every other reorderable task keeps its relative order and
        shifts by one to close the gap, so the operation is a stable
        extraction-and-reinsertion — never a wholesale reshuffle.

        Raises ``KeyError`` (unknown id), ``PermissionError``
        (completed task) and ``ValueError`` (position outside
        ``1..len(active)``).
        """
        task = self._require_reorderable(task_id)
        active = self.reorderable_tasks()
        if not active:
            raise ValueError("No pending tasks to reorder")
        if position == active.index(task) + 1:
            return position  # already there: a no-op, not an error
        # Shared sequence engine (also powers `ck space move`): stable
        # extraction/reinsertion + range validation in ONE place.
        self._assign(sequence.move_item(active, task, position),
                     self._slots())
        return position

    def move_task_to_edge(self, task_id: int, *, top: bool) -> int:
        """Move ``task_id`` to the FIRST (``top``) or LAST reorderable
        slot. Returns the resulting 1-based position."""
        task = self._require_reorderable(task_id)
        active = self.reorderable_tasks()
        if not active:
            raise ValueError("No pending tasks to reorder")
        return self.move_task(task_id, 1 if top else len(active))

    def swap_tasks(self, id_a: int, id_b: int) -> tuple[int, int]:
        """Exchange the positions of two reorderable tasks.

        Returns the (position_a, position_b) pair BEFORE the swap.
        Swapping a task with itself is a no-op.
        """
        task_a = self._require_reorderable(id_a)
        task_b = self._require_reorderable(id_b)
        active = self.reorderable_tasks()
        idx_a = active.index(task_a)
        idx_b = active.index(task_b)
        # Shared sequence engine (also powers `ck space swap`).
        self._assign(sequence.swap_items(active, task_a, task_b),
                     self._slots())
        return (idx_a + 1, idx_b + 1)

    def reorder_tasks(self, ordered_ids: list[int]) -> list[int]:
        """Apply ``ordered_ids`` as the new relative order of the
        reorderable tasks. Returns the IDs in their resulting order.

        STABLE PARTIAL semantics: every listed ID is moved ahead of
        the unlisted ones, preserving the caller's sequence; tasks
        that were not mentioned keep their relative order after them.
        That makes ``ck reorder 3 1`` mean "3 before 1" without
        forcing the user to retype the whole list.

        Raises ``KeyError`` for an unknown id and ``PermissionError``
        when ANY listed task is completed.
        """
        if not ordered_ids:
            raise ValueError("No task IDs given")
        # Validate EVERY id before mutating anything, so a bad list
        # can never leave the AST half-reordered.
        for tid in ordered_ids:
            self._require_reorderable(tid)
        seen: set[int] = set()
        for tid in ordered_ids:
            if tid in seen:
                raise ValueError(f"Duplicate task ID in reorder list: {tid}")
            seen.add(tid)

        active = self.reorderable_tasks()
        by_id = {t.id: t for t in active}
        # Shared normalization primitive (also powers `ck space
        # reorder`): listed IDs first, unlisted keep their order.
        final_ids = sequence.normalize_order(
            ordered_ids, [t.id for t in active])
        self._assign([by_id[tid] for tid in final_ids], self._slots())
        return final_ids


# ---------------------------------------------------------------------------
# Notes (history entries)
# ---------------------------------------------------------------------------


@dataclass
class Notes:
    """A note attached to a task within HISTORY.md."""

    task_id: Optional[int]
    task_title: str
    timestamp: str
    comment: str
    body: str = ""
    note_lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        if self.task_id is not None:
            header = f"### {self.timestamp} | [{self.task_id}] {self.task_title}"
        else:
            header = f"### {self.timestamp} | {self.task_title}"
        if self.note_lines:
            out = f"\n{header}\n"
            for line in self.note_lines:
                out += f"{line}\n"
        else:
            out = f"\n{header}\n- {self.comment}\n"
        if self.body:
            # The body is wrapped in a single fence by this renderer.
            # Strip any fence markers the user included so the emitted
            # markdown cannot nest/break fences (broken nesting would
            # also defeat the fenced-body entry counting in HISTORY.md
            # rotation).
            body = self.body
            for fence in ("```text", "```", "~~~"):
                body = body.replace(fence, "")
            out += f"\n```text\n{body}\n```\n"
        return out


__all__ = [
    "Task",
    "TaskList",
    "TaskStatus",
    "Section",
    "Notes",
    "HistoryEntry",
    "CompletedTaskError",
    "new_task_uuid",
    "note_stamp",
    "history_timestamp",
]