"""Real-CLI integration test for the ``ck add`` placeholder flow.

Unlike the in-process tests, this drives the ACTUAL ``ck`` entrypoint as
a child process (``subprocess.run``) against a real on-disk project, so
the marker-based replacement is exercised end to end: fresh interpreter,
fresh parse of ``PLAN.md``, real file writes.

Flow: ``ck init`` -> assert the seed carries the
``<!-- ck:placeholder -->`` marker -> ``ck add`` twice -> assert both
tasks persist with IDs 1 and 2.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CK_SCRIPT = REPO_ROOT / "ck"
TIMEOUT = 60


def _run(args, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    """Run a real ``ck <args>`` child process and return the result."""
    return subprocess.run(
        [sys.executable, str(CK_SCRIPT), *args],
        cwd=str(cwd), env=env, capture_output=True, text=True,
        timeout=TIMEOUT,
    )


def _clean_env(home: Path) -> dict:
    """Production-like env: no CK toggles, isolated HOME, repo sources."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CK_")}
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["NO_COLOR"] = "1"
    return env


def test_cli_add_flow_with_placeholder_marker(tmp_path):
    if not CK_SCRIPT.is_file():
        pytest.skip(f"ck entrypoint not found at {CK_SCRIPT}")

    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = _clean_env(home)

    # 1. ck init in a clean temporary project.
    init = _run(["init"], project, env)
    assert init.returncode == 0, init.stderr

    # 2. The seed task carries the structural placeholder marker.
    plan = project / ".ck" / "PLAN.md"
    seed_text = plan.read_text(encoding="utf-8")
    assert "<!-- ck:placeholder -->" in seed_text

    # 3. First real CLI add REPLACES the marked seed (still one task).
    first = _run(["add", "First User Task"], project, env)
    assert first.returncode == 0, first.stderr
    text = plan.read_text(encoding="utf-8")
    assert "- [ ] First User Task" in text
    assert "<!-- ck:placeholder -->" not in text

    # 4. Second real CLI add APPENDS as #2.
    second = _run(["add", "Second User Task"], project, env)
    assert second.returncode == 0, second.stderr
    text = plan.read_text(encoding="utf-8")
    assert "- [ ] First User Task" in text
    assert "- [ ] Second User Task" in text
    assert "<!-- ck:placeholder -->" not in text

    # 5. ck list reports both tasks with IDs 1 and 2 (count == 2).
    listing = _run(["list"], project, env)
    assert listing.returncode == 0, listing.stderr
    out = listing.stdout
    assert "[ ] 1. First User Task" in out
    assert "[ ] 2. Second User Task" in out
    task_lines = [l for l in out.splitlines() if l.startswith("[ ] ")]
    assert len(task_lines) == 2, f"expected 2 tasks, got:\n{out}"

    # PLAN.md itself holds exactly two open task bullets.
    assert text.count("- [ ] ") == 2
