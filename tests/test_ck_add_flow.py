"""End-to-end integration test for the `ck add` default-task flow.

This exercises the REAL, file-persisted CLI workflow: `ck init` followed
by consecutive `ck add` invocations on a fresh on-disk project. Every
call reads and writes ``.ck/PLAN.md``, so the test fails if the
placeholder-replacement guard keeps matching a single-task list after
the placeholder has already been replaced (the v0.6.2 regression).

The replacement rule is strict and structural: task #1 is replaced IF
AND ONLY IF the plan holds exactly ONE task, that task is still open
(``[ ]``), and its RAW Markdown line contains the
``<!-- ck:placeholder -->`` marker. Otherwise new tasks are appended
with incrementing IDs.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from cklib.cli import main
from cklib.core import ContextKeeper


def _cli(cwd: Path, argv, monkeypatch) -> str:
    """Run ``ck <argv>`` with ``cwd`` as the working directory.

    Each invocation is a fresh, file-persisted CLI call — context is
    carried on disk, never in memory between calls.
    """
    monkeypatch.chdir(cwd)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(list(argv))
    out = buf.getvalue()
    assert code == 0, f"ck {' '.join(argv)} failed (exit {code}):\n{out}"
    return out


def _task_count(listing: str) -> int:
    return sum(1 for line in listing.splitlines() if line.startswith("["))


def test_init_then_consecutive_add_replaces_then_appends(tmp_path, monkeypatch):
    plan = tmp_path / ".ck" / "PLAN.md"

    # 1. ck init -> a single default placeholder task (#1) carrying
    # the structural marker in its raw PLAN.md line.
    _cli(tmp_path, ["init"], monkeypatch)
    assert "<!-- ck:placeholder -->" in plan.read_text(encoding="utf-8")
    listing = _cli(tmp_path, ["list"], monkeypatch)
    assert "[ ] 1. Describe the first task" in listing
    assert _task_count(listing) == 1

    # 2. First add REPLACES the untouched placeholder in place (still 1 task).
    _cli(tmp_path, ["add", "First User Task"], monkeypatch)
    listing = _cli(tmp_path, ["list"], monkeypatch)
    assert "[ ] 1. First User Task" in listing
    assert _task_count(listing) == 1
    text = plan.read_text(encoding="utf-8")
    assert "Describe the first task" not in text
    assert "<!-- ck:placeholder -->" not in text

    # 3. Second add APPENDS as #2; #1 must be preserved untouched.
    _cli(tmp_path, ["add", "Second User Task"], monkeypatch)
    listing = _cli(tmp_path, ["list"], monkeypatch)
    assert "[ ] 1. First User Task" in listing
    assert "[ ] 2. Second User Task" in listing
    assert _task_count(listing) == 2

    # 4. Third add APPENDS as #3; #1 and #2 preserved with their IDs.
    _cli(tmp_path, ["add", "Third User Task"], monkeypatch)
    listing = _cli(tmp_path, ["list"], monkeypatch)
    assert "[ ] 1. First User Task" in listing
    assert "[ ] 2. Second User Task" in listing
    assert "[ ] 3. Third User Task" in listing
    assert _task_count(listing) == 3
    assert plan.read_text(encoding="utf-8").count("- [ ] ") == 3


def test_single_non_default_task_is_appended_not_replaced(tmp_path):
    """A lone task whose title differs from the placeholder is kept."""
    (tmp_path / ".ck").mkdir(parents=True)
    (tmp_path / ".ck" / "PLAN.md").write_text(
        "# project\n\n## Current Sprint\n- [ ] My custom task\n\n"
        "## Completed\n",
        encoding="utf-8",
    )

    ck = ContextKeeper(root=tmp_path)
    new_id = ck.add_task("Second task")

    assert new_id == 2
    text = (tmp_path / ".ck" / "PLAN.md").read_text(encoding="utf-8")
    assert "- [ ] My custom task" in text
    assert "- [ ] Second task" in text
