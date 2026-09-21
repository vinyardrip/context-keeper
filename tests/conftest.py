"""Pytest session setup for the Context Keeper test suite.

``NO_COLOR`` is pinned for the whole session so ANSI-related tests
and byte-exact output assertions never depend on the ambient
environment of the machine running the suite. Tests that exercise
the color system itself patch ``os.environ`` explicitly (mock's
``patch.dict`` restores this baseline on exit).

Sandbox isolation: the autouse :func:`_isolated_sandbox_root`
fixture pins EVERY test's sandbox anchor into the OS temporary
directory, so the automated suite can never create, mutate, or
delete the repository's own ``.sandbox/`` tree — that directory
belongs to the developer's MANUAL ``ck-dev sandbox setup``
environment (see tests/_isolated_checkout.py for subprocess-level
isolation of ``ck-dev`` wrapper tests).
"""

import os

import pytest

os.environ.setdefault("NO_COLOR", "1")


@pytest.fixture(autouse=True)
def _isolated_sandbox_root(tmp_path):
    """Pin the sandbox anchor of every test to a per-test OS-temp dir.

    ``CK_SANDBOX_ROOT`` redirects
    :func:`cklib.sandbox.sandbox_root` — and every operation derived
    from it (``clean_sandbox``, ``ensure_sandbox_dir``,
    ``setup_sandbox``, dev-mode write interception, lock placement) —
    to ``<pytest tmp>/.sandbox``. The repository's own ``.sandbox/``
    is therefore never touched by the suite, no matter what a test
    (or library code under test) does.

    Tests that explicitly verify the DEFAULT anchoring pop the
    variable themselves; that is a pure path computation and never
    touches the filesystem.

    DEV-TOGGLE ISOLATION: ambient ``CK_SANDBOX``/``CK_DEV`` exported
    in the developer's shell would silently activate dev mode inside
    tests (sandboxed writes then land in the wrong tree and
    subprocess e2e tests inherit the flags). They are pinned OFF for
    every test the same way ``CK_SANDBOX_ROOT`` is pinned; tests
    that exercise dev mode set them explicitly.
    """
    anchor = tmp_path / ".sandbox"
    saved = {
        k: os.environ.get(k)
        for k in ("CK_SANDBOX_ROOT", "CK_SANDBOX", "CK_DEV")
    }
    os.environ["CK_SANDBOX_ROOT"] = str(anchor)
    os.environ.pop("CK_SANDBOX", None)
    os.environ.pop("CK_DEV", None)
    try:
        yield anchor
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
