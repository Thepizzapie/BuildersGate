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
def _isolated_user_dir_session(tmp_path_factory):
    """Keep SESSION-scoped fixtures out of the developer's real ~/.bgate.

    project.init() registers every project it makes, and `bgate use` writes an
    active-project pointer that require_root() falls back to. The per-test
    fixture below handles the ordinary case — but it cannot help a
    session-scoped fixture, because monkeypatch is function-scoped and a
    session fixture is built before any function fixture applies. `_seed_project`
    is session-scoped and calls project.init(), so every full run of this suite
    used to register `test-game` and `smoke-test` in the MACHINE's registry,
    pointing into pytest-of-<user>/pytest-NNNN/. They appeared in the app's
    project switcher as things you could open and stopped resolving the moment
    pytest recycled the directory.

    os.environ directly, because there is no session-scoped monkeypatch; the
    previous value is restored on the way out.
    """
    import os

    previous = os.environ.get("BGATE_HOME")
    os.environ["BGATE_HOME"] = str(tmp_path_factory.mktemp("bgate_home_session"))
    yield
    if previous is None:
        os.environ.pop("BGATE_HOME", None)
    else:
        os.environ["BGATE_HOME"] = previous


@pytest.fixture(autouse=True)
def _isolated_user_dir(tmp_path_factory, monkeypatch):
    """And give every TEST its own, so they cannot leak into each other.

    The session fixture above is not enough on its own, and the failure is
    subtle: with one home shared by the whole run, a registry entry or an
    active pointer written by one test is still there for the next. Tests that
    assert on the ABSENCE of a project — "no pointer and no project still
    raises", "projects with none known says what to do" — then pass alone and
    fail in a full run, depending on collection order.

    Redirected per test, so each starts with an empty ~/.bgate.
    """
    monkeypatch.setenv("BGATE_HOME",
                       str(tmp_path_factory.mktemp("bgate_home")))


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch):
    """No test sees a real provider key, or another test's leaked one.

    envfile.load_env loads a project's .env into process-global os.environ
    with never-overwrite semantics, so one test writing a key into its
    project's .env used to leak it into every LATER test in the run — which
    made provider-selection behaviour (tiers' keyless-rung substitution,
    provider_for's probe order) pass in isolation and fail in the full sweep,
    in whichever order the leak landed. It is also the billing guard: a test
    that accidentally reaches a paid API should find no key, whatever is set
    on the developer's machine. Tests that need a key set their own with
    monkeypatch.setenv, which layers on top of this cleanly.
    """
    for var in ("OPENAI_API_KEY", "KREA_API_KEY", "KIE_API_KEY",
                "DEEPGRAM_API_KEY"):
        monkeypatch.delenv(var, raising=False)


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
