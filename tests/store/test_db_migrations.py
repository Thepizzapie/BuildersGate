"""Migrations must survive being interrupted, and being replayed.

THE FAILURE THESE ARE WRITTEN FROM, found in this repository's own
``.bgate/game.db``: ``PRAGMA user_version`` said 15 while the ``event`` table
that migration 16 creates was already there. Every ``connect()`` replayed 16,
raised ``table event already exists``, and took down the dashboard and every
agent's MCP server — permanently, since nothing about that state heals itself.
``GET /api/state`` answered 500 on a database that was, in substance, fully
migrated.

Two things put a database into that state, and both are covered here:

* ``executescript()`` COMMITS before it runs, so the old code committed a step's
  DDL and then wrote ``user_version`` separately. A crash, a ``bgate panic`` or
  a taskkill anywhere in that gap wedged the file for good.
* ``_migrate`` documents an exclusive lock that it does not actually hold across
  the loop, so two processes starting together really do replay the same step.

The fix is per-step atomicity plus a replay that is a no-op, so neither is fatal.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import db, project
from bgate_ui.app import app


def _version(path) -> int:
    with sqlite3.connect(str(path)) as raw:
        return raw.execute("PRAGMA user_version").fetchone()[0]


def _set_version(path, value: int) -> None:
    with sqlite3.connect(str(path)) as raw:
        raw.execute(f"PRAGMA user_version = {value}")


@pytest.fixture()
def wedged(tmp_path):
    """A fully-migrated project whose user_version has been walked backwards.

    That is exactly the on-disk state the old code produced when it was
    interrupted between a step's DDL and its version bump: every object the
    remaining steps create is already present, and the pragma says otherwise.
    """
    project.init(tmp_path, "Wedged", pitch="a game with a half-recorded schema")
    path = db.db_path(tmp_path)
    assert _version(path) == len(db._MIGRATIONS)
    db.close_all()
    _set_version(path, len(db._MIGRATIONS) - 1)
    yield tmp_path
    db.close_all()


class TestUnrecordedMigration:
    def test_connect_repairs_instead_of_raising(self, wedged):
        path = db.db_path(wedged)
        conn = db.connect(wedged)                       # used to raise
        assert _version(path) == len(db._MIGRATIONS)
        # The replay must not have disturbed what was already there.
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name='event'").fetchone()[0] == 1

    def test_the_dashboard_answers_on_a_repaired_database(self, wedged,
                                                          monkeypatch):
        """The user-visible half: /api/state 500'd on a database like this."""
        monkeypatch.setenv("BGATE_ROOT", str(wedged))
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/api/state").status_code == 200

    def test_a_second_replay_is_still_a_no_op(self, wedged):
        """The concurrent-startup race: the loser replays a step that is done.

        Nothing about it should raise, and it should finish by recording the
        version the winner recorded.
        """
        path = db.db_path(wedged)
        db.connect(wedged)
        db.close_all()
        _set_version(path, len(db._MIGRATIONS) - 1)
        db.connect(wedged)
        assert _version(path) == len(db._MIGRATIONS)


class TestStepAtomicity:
    def test_a_step_that_fails_partway_leaves_nothing_behind(self, tmp_path,
                                                             monkeypatch):
        """DDL and the version bump are one commit, or the wedge comes back.

        The step below creates a table and then references a column that does
        not exist. If the CREATE were to survive the failure — which is what
        executescript() without an explicit transaction does — the next connect
        would replay the step, hit 'already exists', and the database would be
        in precisely the state this module is named for.
        """
        project.init(tmp_path, "Atomic", pitch="a game that gets interrupted")
        db.close_all()
        path = db.db_path(tmp_path)
        before = _version(path)

        bad = """
        CREATE TABLE half_applied (id INTEGER PRIMARY KEY);
        CREATE INDEX idx_half ON half_applied(no_such_column);
        """
        monkeypatch.setattr(db, "_MIGRATIONS", [*db._MIGRATIONS, bad])

        with pytest.raises(sqlite3.OperationalError):
            db.connect(tmp_path)
        db.close_all()

        with sqlite3.connect(str(path)) as raw:
            present = raw.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='half_applied'"
            ).fetchone()[0]
        assert present == 0, "the failed step's CREATE TABLE was committed"
        assert _version(path) == before, "user_version moved on a failed step"

    def test_an_unrelated_error_is_not_swallowed_as_a_repair(self, tmp_path,
                                                             monkeypatch):
        """Only 'already exists' is repairable. Everything else must surface."""
        project.init(tmp_path, "Loud", pitch="a game with a broken migration")
        db.close_all()
        monkeypatch.setattr(
            db, "_MIGRATIONS",
            [*db._MIGRATIONS, "CREATE TABLE oops (id INTEGER REFERENCES);"])
        with pytest.raises(sqlite3.OperationalError):
            db.connect(tmp_path)
        db.close_all()

    def test_a_collision_it_cannot_skip_raises_the_real_error(self, tmp_path):
        """'already exists' from something with no IF NOT EXISTS rewrite.

        The repair only knows how to skip CREATE TABLE/INDEX/VIEW. Anything else
        has to come back as the OperationalError that says what collided — not
        as the RuntimeError a bare re-raise outside the handler would produce.
        """
        project.init(tmp_path, "Trigger", pitch="a game with a trigger clash")
        conn = db.connect(tmp_path)
        step = ("CREATE TRIGGER t_dupe AFTER INSERT ON project "
                "BEGIN SELECT 1; END;")
        db._apply_sql_step(conn, step, len(db._MIGRATIONS))
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            db._apply_sql_step(conn, step, len(db._MIGRATIONS))
        db.close_all()
