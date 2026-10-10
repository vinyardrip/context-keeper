"""Domain model: tasks, lists, notes, status enums.

The model layer holds both the AST node for a parsed file (with line
numbers and section context) and the operations that mutations
operate on. Mutations never touch raw text — they go through the AST
and a deterministic full-file ``render_plan``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """The three canonical task states."""

    OPEN = "open"
    FOCUSED = "focused"
    DONE = "done"

    @property
    def canonical_marker(self) -> str:
        """Canonical CommonMark marker — always ``[ ]`` / ``[>]`` / ``[x]``.

        This is what the writer emits; the parser also accepts the
        legacy no-space form ``[]`` for backward compatibility.
        """
        return {
            TaskStatus.OPEN: " ",       # "- [ ] title"
            TaskStatus.FOCUSED: ">",   # "- [>] title"
            TaskStatus.DONE: "x",      # "- [x] title"
        }[self]

    @property
    def legacy_marker(self) -> str:
        """Legacy CommonMark-less marker — empty string for OPEN."""
        return {
            TaskStatus.OPEN: "",        # "- [] title"
            TaskStatus.FOCUSED: ">",    # "- [>] title"
            TaskStatus.DONE: "x",       # "- [x] title"
        }[self]

    @classmethod
    def from_marker(cls, marker: str) -> "TaskStatus":
        m = (marker or "").strip().lower()
        if m in {"", " "}:
            return cls.OPEN
        if m == ">":
            return cls.FOCUSED
        if m in {"x"}:
            return cls.DONE
        raise ValueError(f"Unknown task marker: {marker!r}")


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
    """

    id: int
    status: TaskStatus
    title: str
    line_number: int
    marker_raw: str
    section: str = ""
    notes: list[str] = field(default_factory=list)
    legacy_syntax: bool = False
    # 1-based source position where a NEW task should be inserted
    # (end of the ``## Current Sprint`` section). Parsed tasks never
    # carry this hint; it routes the renderer through the INSERT
    # pass instead of substitution / before-Completed append.
    insert_line: Optional[int] = None

    # ---- rendering ----

    def to_line(self) -> str:
        """Render to canonical CommonMark (``- [ ]`` / ``- [>]`` / ``- [x]``)."""
        return f"- [{self.status.canonical_marker}] {self.title}".rstrip()

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
    def done(self) -> list[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.DONE]

    @property
    def total(self) -> int:
        return len(self.tasks)

    @property
    def completion_pct(self) -> float:
        if not self.tasks:
            return 0.0
        return round(100.0 * len(self.done) / len(self.tasks), 1)

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
            insert_line: int | None = None) -> Task:
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
        )
        self.tasks.append(task)
        return task

    def focus(self, task_id: int) -> Task:
        """Mark ``task_id`` as focused; demote any other focused task to OPEN."""
        target = self.by_id(task_id)
        if target is None:
            raise KeyError(f"No task with id {task_id}")
        for t in self.tasks:
            if t.status == TaskStatus.FOCUSED and t.id != task_id:
                t.status = TaskStatus.OPEN
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
        """Append a note string to a task's node (in-memory; HISTORY.md is separate)."""
        t = self.by_id(task_id)
        if t is None:
            raise KeyError(f"No task with id {task_id}")
        if note:
            t.notes.append(note)
        return t

    def remove(self, task_id: int) -> Optional[Task]:
        for i, t in enumerate(self.tasks):
            if t.id == task_id:
                return self.tasks.pop(i)
        return None

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
        if position < 1 or position > len(active):
            raise ValueError(
                f"Position {position} is out of range (1..{len(active)})")
        if position == active.index(task) + 1:
            return position  # already there: a no-op, not an error
        others = [t for t in active if t.id != task_id]
        others.insert(position - 1, task)
        self._assign(others, self._slots())
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
        if idx_a == idx_b:
            return (idx_a + 1, idx_b + 1)
        active[idx_a], active[idx_b] = active[idx_b], active[idx_a]
        self._assign(active, self._slots())
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
        listed = [by_id[tid] for tid in ordered_ids]
        rest = [t for t in active if t.id not in seen]
        self._assign(listed + rest, self._slots())
        return [t.id for t in listed + rest]


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

    def render(self) -> str:
        if self.task_id is not None:
            header = f"### {self.timestamp} | [{self.task_id}] {self.task_title}"
        else:
            header = f"### {self.timestamp} | {self.task_title}"
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
    "CompletedTaskError",
]