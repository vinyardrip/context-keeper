"""Generic, reusable sequence mutation primitives.

Both user-facing sequencing engines in ``ck`` shuffle an ordered list of
unique keys:

- **tasks** — :class:`cklib.models.TaskList` reorders pending/active
  tasks over their source-line slots (``ck move`` / ``swap`` /
  ``reorder``);
- **spaces** — the global space order persisted in
  ``~/.config/ck/state.json`` (``spaces_order``) drives ``ck space
  move`` / ``swap`` / ``reorder`` and the row order of ``ck space
  list`` and the dashboard.

This module owns the algorithm — stable extraction/reinsertion,
exchange and normalization — so the two engines cannot drift apart.
The functions are PURE: they never mutate their input and never touch
the filesystem. Callers persist the returned list through their own
storage layer (line-number slots for tasks, the state file for spaces).

Items must be hashable and unique within one sequence (task objects /
space names). Ordering is 1-based at the API boundary, matching every
``ck`` command users type.
"""

from __future__ import annotations

from typing import Hashable, List, Sequence, TypeVar

T = TypeVar("T", bound=Hashable)


def move_item(items: Sequence[T], item: T, position: int) -> List[T]:
    """Return a NEW list with ``item`` at 1-based ``position``.

    Every other element keeps its relative order and shifts by one to
    close/extend the gap — a stable extraction-and-reinsertion, never
    a wholesale reshuffle. Moving an item to the slot it already
    occupies returns an equivalent order.

    Raises ``KeyError`` when ``item`` is not in ``items`` and
    ``ValueError`` when ``position`` is outside ``1..len(items)``.
    """
    ordered = list(items)
    if item not in ordered:
        raise KeyError(item)
    if not 1 <= position <= len(ordered):
        raise ValueError(
            f"Position {position} is out of range (1..{len(ordered)})")
    rest = [entry for entry in ordered if entry != item]
    rest.insert(position - 1, item)
    return rest


def swap_items(items: Sequence[T], item_a: T, item_b: T) -> List[T]:
    """Return a NEW list with ``item_a`` and ``item_b`` exchanged.

    Swapping an item with itself is a no-op. Raises ``KeyError`` when
    either item is missing (nothing is half-applied).
    """
    ordered = list(items)
    for item in (item_a, item_b):
        if item not in ordered:
            raise KeyError(item)
    index_a, index_b = ordered.index(item_a), ordered.index(item_b)
    ordered[index_a], ordered[index_b] = ordered[index_b], ordered[index_a]
    return ordered


def normalize_order(requested: Sequence[T],
                    available: Sequence[T]) -> List[T]:
    """Reconcile ``requested`` with the set of ``available`` items.

    Listed items come first, in the caller's order; duplicates and
    names that are no longer available are dropped; every remaining
    available item follows in its original ``available`` order. This
    is the "re-index / normalize" primitive: it can never lose a live
    item and never resurrect a stale one.

    Both inputs are read-only; the returned list is new.
    """
    available_list = list(available)
    available_set = set(available_list)
    seen: set = set()
    out: List[T] = []
    for item in requested:
        if item in available_set and item not in seen:
            out.append(item)
            seen.add(item)
    for item in available_list:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def without(items: Sequence[T], item: T) -> List[T]:
    """Return a NEW list with every occurrence of ``item`` removed."""
    return [entry for entry in items if entry != item]


__all__ = [
    "move_item",
    "swap_items",
    "normalize_order",
    "without",
]
