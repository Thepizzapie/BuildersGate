from __future__ import annotations

import shutil

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


@pytest.fixture(scope="session", autouse=True)
def _isolated_user_dir(tmp_path_factory):
    """Keep the suite out of the developer's real ~/.bgate.

    project.init() registers every project it makes, and `bgate use` writes an
    active-project pointer that require_root() falls back to. Without this, a
    test run rewrites the machine's registry, and — worse — a stale pointer left
    by an earlier run silently satisfies a require_root() the test expected to
    fail. BGATE_HOME exists precisely so that redirect is one env var.

    SESSION-SCOPED, AND THAT MATTERS. This was function-scoped, using
    monkeypatch — which is itself function-scoped — so it could not possibly be
    in effect while a SESSION-scoped fixture ran. `_seed_project` below is
    session-scoped and calls project.init(), so every full run of this suite
    registered its seed projects in the developer's real registry: `test-game`
    and `smoke-test`, pointing into pytest-of-<user>/pytest-NNNN/, appeared in
    the app's project switcher as things you could open, and stopped resolving
    as soon as pytest recycled the directory.

    os.environ directly rather than monkeypatch, because there is no
    session-scoped monkeypatch; the value is restored on the way out.
    """
    import os

    previous = os.environ.get("BGATE_HOME")
    os.environ["BGATE_HOME"] = str(tmp_path_factory.mktemp("bgate_home"))
    yield
    if previous is None:
        os.environ.pop("BGATE_HOME", None)
    else:
        os.environ["BGATE_HOME"] = previous


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def _seed_project(tmp_path_factory):
    """One initialised project, built once for the whole run.

    A project is a single SQLite file, and almost all of the 56 ms `init` costs
    is applying the migration list to it. Every test that asks for `root` used
    to pay that again: measured at ~5,300 tests, a quarter of the suite's
    wall-clock was rebuilding the same schema.

    Copying the finished file is 2 ms. This fixture is the file; `root` below is
    the copy, so each test still gets its own project on its own path and
    nothing is shared between them.
    """
    seed = tmp_path_factory.mktemp("seed") / "project"
    project.init(seed, "Test Game", pitch="a game for tests")
    # Closed before anything copies it, or Windows hands out a half-flushed
    # database and the first query in a test reads a schema that is still being
    # written.
    db.close_all()
    return seed


@pytest.fixture()
def root(tmp_path, _seed_project):
    """A fresh project per test, copied from the session's seed.

    Same shape as building one from scratch: its own directory, its own file, no
    state carried from another test. Connections are per-path, so the cache is
    dropped afterward to keep tmp dirs from leaking handles on Windows.
    """
    shutil.copytree(_seed_project, tmp_path, dirs_exist_ok=True)
    yield tmp_path
    db.close_all()
