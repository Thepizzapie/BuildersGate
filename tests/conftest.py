from __future__ import annotations

import pytest

from bgate_core import db, project


@pytest.fixture(autouse=True)
def _no_dashboard_auth(monkeypatch):
    """Turn off the same-origin/token guard for the suite.

    The guard is an origin-level concern; making all ~350 tests plumb a bearer
    token would test the fixture, not the feature. The guard itself is exercised
    for real in tests/test_auth_guard.py — that is the only place it should be.
    """
    monkeypatch.setenv("BGATE_NO_AUTH", "1")


@pytest.fixture(autouse=True)
def _isolated_user_dir(tmp_path_factory, monkeypatch):
    """Keep the suite out of the developer's real ~/.bgate.

    project.init() registers every project it makes, and `bgate use` writes an
    active-project pointer that require_root() falls back to. Without this, a
    test run rewrites the machine's registry, and — worse — a stale pointer left
    by an earlier run silently satisfies a require_root() the test expected to
    fail. BGATE_HOME exists precisely so that redirect is one env var.
    """
    monkeypatch.setenv("BGATE_HOME",
                       str(tmp_path_factory.mktemp("bgate_home")))


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def root(tmp_path):
    """A fresh initialized project per test. Connections are per-path, so the
    cache is dropped afterward to keep tmp dirs from leaking handles on Windows."""
    project.init(tmp_path, "Test Game", pitch="a game for tests")
    yield tmp_path
    db.close_all()
