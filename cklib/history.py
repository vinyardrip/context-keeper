"""History archiving: rotation, compression and FIFO cleanup.

Pure path/text helpers shared by :meth:`ContextKeeper._rotate_history`
and the ``ck log --all`` reader. Everything here uses Python's
built-in ``gzip`` module so archive compression is 100%
cross-platform (Linux / macOS / Windows) without requiring any
external ``gzip`` / ``tar`` binaries.
"""

from __future__ import annotations

import gzip
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# Timestamp inside a rotation archive name:
#   HISTORY_<YYYYMMDD>_<HHMMSS>.md.gz       (compressed, new style)
#   HISTORY_<YYYYMMDD>_<HHMMSS>.md.bak      (uncompressed, legacy style)
#   HISTORY_<YYYYMMDD>_<HHMMSS>_N.md.gz     (same-second collision dedupe)
#   HISTORY_<YYYYMMDD>_<HHMMSS>_N.md.bak
_HISTORY_TS_RE = re.compile(
    r"^HISTORY_(\d{8})_(\d{6})(?:_(\d+))?\.(?:md\.gz|md\.bak)$"
)


def archive_name(ts: datetime, *, compressed: bool, seq: int = 0) -> str:
    """Rotation archive filename for ``ts``.

    ``HISTORY_<YYYYMMDD>_<HHMMSS>.md.gz`` when ``compressed`` else the
    legacy ``HISTORY_<YYYYMMDD>_<HHMMSS>.md.bak``. ``seq`` (1-based)
    disambiguates multiple rotations within the same second:
    ``HISTORY_<YYYYMMDD>_<HHMMSS>_<seq>.md.gz``.
    """
    suffix = ".md.gz" if compressed else ".md.bak"
    stamp = ts.strftime("%Y%m%d_%H%M%S")
    if seq:
        stamp = f"{stamp}_{seq}"
    return f"HISTORY_{stamp}{suffix}"


def is_history_archive(path: Path) -> bool:
    """True when ``path.name`` matches the rotation archive pattern.

    Both compression generations qualify: ``.md.gz`` (gzip) and
    legacy ``.md.bak`` (plain text).
    """
    return bool(_HISTORY_TS_RE.match(path.name))


def read_archive(path: Path) -> Tuple[str, str]:
    """Read one archive file, transparently decompressing ``.md.gz``.

    Returns ``(text, warning_or_empty)``: the decoded content and an
    optional human-readable warning when the file could not be read
    (corrupted gzip stream, vanished mid-read, undecodable bytes).
    Never raises — a corrupted archive must not take the whole log
    view down; the caller renders the warning in place instead.
    """
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return fh.read(), ""
        return path.read_text(encoding="utf-8"), ""
    except (OSError, EOFError, UnicodeDecodeError, gzip.BadGzipFile) as e:
        return "", f"[!] Skipped unreadable archive {path.name}: {e}"


def list_history_archives(ck_dir: Path) -> List[Path]:
    """All rotation archives in ``ck_dir``, oldest first.

    Accepts both ``.md.gz`` and legacy ``.md.bak``; ordering is by the
    timestamp embedded in the filename (falling back to mtime for
    names that do not parse — impossible for matcher-passing names,
    but keeps the sort total for exotic filesystem entries).
    """
    try:
        candidates = [p for p in ck_dir.iterdir() if is_history_archive(p)]
    except OSError:
        return []

    def sort_key(p: Path) -> Tuple[str, float]:
        m = _HISTORY_TS_RE.match(p.name)
        if m:
            # Zero-padded seq keeps same-second archives ordered.
            seq = f"{int(m.group(3)):04d}" if m.group(3) else "0000"
            return (m.group(1) + m.group(2) + seq, 0.0)
        try:
            return ("", p.stat().st_mtime)
        except OSError:
            return ("", 0.0)

    return sorted(candidates, key=sort_key)


def parse_archive_stamp(path: Path) -> Optional[datetime]:
    """Extract the rotation timestamp from an archive filename.

    Returns ``None`` for names outside the rotation pattern.
    """
    m = _HISTORY_TS_RE.match(path.name)
    if m is None:
        return None
    try:
        return datetime.strptime(
            f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S"
        )
    except ValueError:
        return None


def fifo_cleanup(ck_dir: Path, max_files: int) -> List[str]:
    """Enforce ``max_files`` retention over rotation archives (FIFO).

    Scans ``ck_dir`` for ``HISTORY_*.md.gz`` and ``HISTORY_*.md.bak``,
    sorts chronologically (embedded timestamp), and deletes the
    oldest excess files. Returns the deleted file names (empty when
    nothing needed purging). Individual deletion failures are
    skipped silently — retention is best-effort, never fatal.
    """
    if max_files < 0:
        max_files = 0
    archives = list_history_archives(ck_dir)
    if max_files == 0:
        # ``archives[:-0]`` would be ``[]`` — keep-nothing must
        # purge everything instead.
        excess = list(archives)
    elif len(archives) > max_files:
        excess = archives[:-max_files]
    else:
        excess = []
    deleted: List[str] = []
    for p in excess:
        try:
            p.unlink()
            deleted.append(p.name)
        except OSError:
            continue
    return deleted


def concat_archives(paths: Iterable[Path]) -> Tuple[str, List[str]]:
    """Concatenate archives chronologically (input order preserved).

    Returns ``(text, warnings)``. Unreadable archives contribute a
    warning and no content, so one corrupted file cannot hide the
    rest of the history.
    """
    chunks: List[str] = []
    warnings: List[str] = []
    for p in paths:
        text, warning = read_archive(p)
        if warning:
            warnings.append(warning)
        if text:
            chunks.append(text)
    return "\n".join(chunks), warnings


def compress_to(source: Path, target: Path) -> None:
    """Gzip-compress ``source`` into ``target`` (both Path objects).

    Uses ``mtime=0`` in the gzip header so archives stay
    byte-reproducible for tests. Caller is responsible for removing
    ``source`` after a successful call.
    """
    with source.open("rb") as fin, \
            gzip.GzipFile(filename="", mode="wb", fileobj=target.open("wb"),
                          mtime=0) as fout:
        while True:
            block = fin.read(1 << 20)
            if not block:
                break
            fout.write(block)
