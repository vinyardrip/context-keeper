"""Pytest session setup for the Context Keeper test suite.

``NO_COLOR`` is pinned for the whole session so ANSI-related tests
and byte-exact output assertions never depend on the ambient
environment of the machine running the suite. Tests that exercise
the color system itself patch ``os.environ`` explicitly (mock's
``patch.dict`` restores this baseline on exit).

Config isolation: the autouse :func:`_isolate_ck_config` fixture
redirects EVERY test's global config directory (the registry,
``state.json`` and the spaces tree — normally ``~/.config/ck/``) into
a per-test OS-temp directory via ``CK_CONFIG_DIR``. Without it the
suite registered its own tmp project dirs into the developer's REAL
registry, which then surfaced as hundreds of ``[MISSING]`` rows in
``ck dashboard``.

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

# Modules that captured the global config paths BY VALUE
# (``from .config import GLOBAL_REGISTRY_FILE``) instead of reading
# them through ``cklib.config`` on every call. Rebinding the attributes
# keeps in-process tests isolated too — not just subprocesses, which
# inherit ``CK_CONFIG_DIR`` from the environment.
_CONFIG_PATH_MODULES = ("cklib.config", "cklib.registry", "cklib.core")


def _global_config_paths(cfg):
    return {
        "GLOBAL_CONFIG_DIR": cfg,
        "GLOBAL_REGISTRY_FILE": cfg / "projects.json",
        "GLOBAL_STATE_FILE": cfg / "state.json",
        # The pre-0.8 migration source: ``Registry.load`` unlinks it
        # after migrating, so it must never point at the real ``~/.ckrc``.
        "LEGACY_GLOBAL_CONFIG_FILE": cfg.parent / ".ckrc",
    }


@pytest.fixture(autouse=True)
def _isolate_ck_config(tmp_path, monkeypatch):
    """Every test gets a throwaway ``CK_CONFIG_DIR``.

    No test may read or write the real ``~/.config/ck/`` (registry,
    ``state.json``, spaces, locks). Subprocesses inherit the variable
    from the environment; modules that imported the paths by value are
    rebound explicitly. ``CK_SANDBOX`` is deliberately NOT touched
    here — tests that exercise sandbox behaviour control it themselves
    through :func:`_isolated_sandbox_root`.
    """
    import importlib
    import sys

    cfg = tmp_path / "ck-config"
    cfg.mkdir()
    monkeypatch.setenv("CK_CONFIG_DIR", str(cfg))

    paths = _global_config_paths(cfg)
    for module_name in _CONFIG_PATH_MODULES:
        module = sys.modules.get(module_name) or importlib.import_module(
            module_name)
        for attr, value in paths.items():
            if hasattr(module, attr):
                monkeypatch.setattr(module, attr, value)
    yield cfg


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
    that exercise dev mode set them explicitly. The session markers
    (``CK_SANDBOX_SHELL``/``CK_SANDBOX_ACTIVE``) are pinned OFF too:
    running the suite from inside a sandbox session would otherwise
    force strict binary routing and pin a mirror project, producing
    environment-dependent output.
    """
    anchor = tmp_path / ".sandbox"
    saved = {
        k: os.environ.get(k)
        for k in ("CK_SANDBOX_ROOT", "CK_SANDBOX", "CK_DEV",
                  "CK_SANDBOX_ACTIVE", "CK_SANDBOX_SHELL")
    }
    os.environ["CK_SANDBOX_ROOT"] = str(anchor)
    os.environ.pop("CK_SANDBOX", None)
    os.environ.pop("CK_DEV", None)
    # Session markers also leak from an ambient sandbox session:
    # ``CK_SANDBOX_SHELL`` forces strict routing inside
    # ``active_binary_path`` and ``CK_SANDBOX_ACTIVE`` pins a mirror
    # project, either of which would make resolution/output differ
    # from a clean production environment. Pin both off; tests that
    # exercise them set them explicitly.
    os.environ.pop("CK_SANDBOX_ACTIVE", None)
    os.environ.pop("CK_SANDBOX_SHELL", None)
    try:
        yield anchor
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
