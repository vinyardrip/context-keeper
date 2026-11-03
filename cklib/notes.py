"""Append-only process-note helpers shared by projects and spaces.

Notes are stored as ordered arrays of ``{"text", "created"}`` entries.
Older state files used a single string per task; :func:`migrate_notes`
converts those values in memory without touching the file, so read-only
commands never rewrite ``state.json`` (or a space sidecar).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


def now_iso() -> str:
    """Current UTC timestamp in ISO-8601 form with a ``+00:00`` offset."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_mtime_iso(path: Optional[Path]) -> str:
    """Fallback timestamp for legacy notes: the file's mtime (UTC)."""
    if path is not None:
        try:
            mtime = path.stat().st_mtime
            return datetime.fromtimestamp(
                mtime, tz=timezone.utc).isoformat(timespec="seconds")
        except OSError:
            pass
    return now_iso()


def new_entry(text: str, created: Optional[str] = None) -> dict:
    """Build one note entry (sanitization happens in the caller)."""
    return {"text": text, "created": created or now_iso()}


def append_entry(entries: list, text: str) -> dict:
    """Append one entry, guaranteeing a strictly newer ``created``.

    Two notes can be appended within the same wall-clock second; the
    stored timestamps must still be distinct (and ordered). When the
    clock has not advanced past the previous entry, bump it by one
    second while keeping the second-precision ISO shape.
    """
    created = now_iso()
    if entries:
        previous = entries[-1].get("created") if isinstance(
            entries[-1], dict) else None
        if isinstance(previous, str) and previous:
            if created <= previous:
                try:
                    parsed = datetime.fromisoformat(
                        previous.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    created = (parsed + timedelta(seconds=1)).isoformat(
                        timespec="seconds")
                except (TypeError, ValueError):
                    created = previous
    entry = {"text": text, "created": created}
    entries.append(entry)
    return entry


def _normalize_entry(value: Any, fallback_created: str) -> Optional[dict]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return {"text": text, "created": fallback_created}
    if isinstance(value, dict):
        text = value.get("text")
        created = value.get("created")
        if not isinstance(text, str) or not text.strip():
            return None
        if not isinstance(created, str) or not created.strip():
            created = fallback_created
        return {"text": text, "created": created}
    return None


def normalize_entries(value: Any, fallback_created: str) -> list[dict]:
    """Normalize a stored note value into an ordered list of entries."""
    if isinstance(value, str):
        entry = _normalize_entry(value, fallback_created)
        return [entry] if entry is not None else []
    if isinstance(value, list):
        out: list[dict] = []
        for item in value:
            entry = _normalize_entry(item, fallback_created)
            if entry is not None:
                out.append(entry)
        return out
    return []


def migrate_notes(data: dict, fallback_created: Optional[str] = None) -> dict:
    """Return ``data`` with every note value normalized to an entry array.

    Handles both the historical single-string shape and the current
    array shape. Active/paused legacy note strings are also seeded into
    the ``notes`` ledger when the ledger has no entry for that task, so
    every renderer and archiver can read one canonical map. The input
    dict is mutated in place for convenience; callers still own the
    decision whether to persist it (reads must not).
    """
    if not isinstance(data, dict):
        return data
    fallback = fallback_created or now_iso()
    raw = data.get("notes")
    normalized: dict[str, list[dict]] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            entries = normalize_entries(value, fallback)
            if entries:
                normalized[str(key)] = entries

    active = data.get("active_task")
    if isinstance(active, dict):
        tid = active.get("id")
        note = active.get("note")
        if isinstance(tid, int) and not isinstance(tid, bool) \
                and isinstance(note, str) and note.strip() \
                and str(tid) not in normalized:
            normalized[str(tid)] = [{
                "text": note,
                "created": fallback,
            }]
        # The legacy scalar is deprecated. Drop it from the in-memory
        # view in every case: the array ledger wins when present, and
        # the next write must never persist the stale key.
        active.pop("note", None)

    paused = data.get("paused_tasks")
    if isinstance(paused, list):
        for item in paused:
            if not isinstance(item, dict):
                continue
            tid = item.get("id")
            note = item.get("note")
            if isinstance(tid, int) and not isinstance(tid, bool) \
                    and isinstance(note, str) and note.strip() \
                    and str(tid) not in normalized:
                normalized[str(tid)] = [{
                    "text": note,
                    "created": fallback,
                }]

    if normalized:
        data["notes"] = normalized
    elif "notes" in data:
        data["notes"] = {}
    return data


# The public spelling used by the state loaders; kept as an alias so
# callers can mirror the requested ``_migrate_notes`` name.
_migrate_notes = migrate_notes
