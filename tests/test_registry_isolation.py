"""Regression guard: ``pytest`` never writes the real ``~/.config/ck/``.

Historically the suite registered its own throwaway project dirs
(``/tmp*/alpha``, ``/tmp*/beta``, ...) into the developer's REAL
registry: ``~/.config/ck/projects.json`` accumulated 624 stale entries
whose folders no longer existed, and ``ck dashboard`` rendered every
one of them as ``[MISSING]`` — scaring users into running ``ck prune``
to clean up a mess the tests had created.

The autouse ``_isolate_ck_config`` fixture in ``tests/conftest.py``
redirects every test to a throwaway ``CK_CONFIG_DIR``. These tests
pin that invariant from both sides: the fixture really is in effect,
and a subprocess that deliberately runs WITHOUT ``CK_CONFIG_DIR``
still cannot modify the real registry.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG_DIR = Path.home() / ".config" / "ck"
REAL_REGISTRY = REAL_CONFIG_DIR / "projects.json"


def _snapshot(path: Path):
    """Content + mtime fingerprint, or ``None`` when absent."""
    if not path.exists():
        return None
    return hashlib.md5(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_isolate_ck_config_fixture_is_active():
    """Every test runs against a throwaway config dir, not the real one."""
    from cklib import config as ckconfig
    from cklib import core as ckcore
    from cklib import registry as ckregistry

    isolated = Path(os.environ["CK_CONFIG_DIR"]).resolve()
    assert isolated != REAL_CONFIG_DIR
    for module in (ckconfig, ckregistry, ckcore):
        assert Path(module.GLOBAL_REGISTRY_FILE) == isolated / "projects.json"
    assert ckconfig.GLOBAL_STATE_FILE == isolated / "state.json"


def test_in_process_registration_stays_inside_the_fixture(tmp_path):
    """A mutating command (registration) writes the throwaway registry,
    never the real one. Without the autouse fixture this test registers
    its tmp project into ``~/.config/ck/projects.json`` — the exact
    leak that produced the 624 ``[MISSING]`` rows."""
    from cklib import registry as ckregistry

    project = tmp_path / "isolated-project"
    project.mkdir()
    before = _snapshot(REAL_REGISTRY)

    ckregistry.register_project(project, name="isolated-project")

    assert _snapshot(REAL_REGISTRY) == before
    isolated = Path(os.environ["CK_CONFIG_DIR"]) / "projects.json"
    assert "isolated-project" in isolated.read_text(encoding="utf-8")


def test_pytest_does_not_touch_real_registry():
    """A ``ck`` run without ``CK_CONFIG_DIR`` must not mutate the real
    registry — no new entries, no refreshed ``last_seen``, no new file."""
    before = _snapshot(REAL_REGISTRY)
    env = {k: v for k, v in os.environ.items() if k != "CK_CONFIG_DIR"}
    # The update notifier would rewrite ~/.config/ck/state.json on a
    # git checkout; keep this probe strictly read-only.
    env["CK_DISABLE_UPDATE_CHECK"] = "1"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "ck"), "list"],
        capture_output=True, text=True, env=env,
        cwd=str(REPO_ROOT), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    after = _snapshot(REAL_REGISTRY)
    assert before == after, (
        "ck list must not modify the real registry "
        f"({REAL_REGISTRY}): {before} -> {after}"
    )
