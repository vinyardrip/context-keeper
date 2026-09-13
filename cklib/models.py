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
        """Detect open tasks that appear *after* a done task.

        Detection is POSITIONAL (order within the task list) rather
        than line-number based, so the result is immune to source
        line renumbering — auto-repair dropping artifact lines, or
        newly added tasks whose provisional line numbers point past
        EOF — and never miscounts. Returns the IDs of the offending
        open tasks that follow the last done task.
        """
        last_done_pos = -1
        for pos, t in enumerate(self.tasks):
            if t.status == TaskStatus.DONE:
                last_done_pos = pos
        if last_done_pos == -1:
            return []
        return [
            t.id for pos, t in enumerate(self.tasks)
            if pos > last_done_pos and t.status == TaskStatus.OPEN
        ]

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
]