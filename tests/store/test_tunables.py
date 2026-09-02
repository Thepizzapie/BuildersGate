"""Tunables joined to what the playtests measured.

The gameplay seat's rule is *the measured number sits next to the knob — read it
before you turn it*. Nothing new is recorded to make that true: iterations
already snapshot every tunable, playtest sessions already carry a start time,
and telemetry is already stored per session. These tests pin the JOIN, and — as
importantly — pin what it refuses to claim.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import db, project
from bgate_core.design import tunables
from bgate_ui.app import app


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    project.init(tmp_path, "Tunables")
    yield tmp_path
    db.close_all()


def _iteration(root, goal, when, snapshot):
    with db.tx(root) as conn:
        conn.execute(
            "INSERT INTO iteration (goal, created_at, tunables_json) VALUES (?, ?, ?)",
            (goal, when, json.dumps(snapshot)))


def _session(root, name, when, events=()):
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_session (name, slug, status, started_at, duration_s) "
            "VALUES (?, ?, 'ready', ?, 60)", (name, name.lower(), when))
        sid = int(cur.lastrowid)
        for i, kind in enumerate(events):
            conn.execute("INSERT INTO playtest_event (session_id, t, kind) VALUES (?, ?, ?)",
                         (sid, float(i), kind))
    return sid


class TestTheJoin:
    def test_a_value_carries_the_sessions_played_at_it(self, root):
        _iteration(root, "faster jump", "2026-01-01 10:00:00",
                   {"game/player.gd": {"JUMP": "0.35"}})
        _session(root, "first", "2026-01-01 11:00:00", ["death", "death", "retry"])
        got = tunables.measured(root)["tunables"]
        assert len(got) == 1
        knob = got[0]
        assert knob["name"] == "JUMP" and knob["file"] == "game/player.gd"
        assert knob["history"][0]["value"] == "0.35"
        assert [s["name"] for s in knob["history"][0]["sessions"]] == ["first"]
        # the telemetry the game itself emitted, by ITS OWN kinds
        assert knob["history"][0]["events"] == {"death": 2, "retry": 1}

    def test_a_session_lands_in_the_iteration_that_was_open(self, root):
        _iteration(root, "old", "2026-01-01 10:00:00", {"game/p.gd": {"SPEED": "1"}})
        _iteration(root, "new", "2026-02-01 10:00:00", {"game/p.gd": {"SPEED": "2"}})
        _session(root, "under-old", "2026-01-15 10:00:00")
        _session(root, "under-new", "2026-02-15 10:00:00")
        hist = {h["value"]: h for h in tunables.measured(root)["tunables"][0]["history"]}
        assert [s["name"] for s in hist["1"]["sessions"]] == ["under-old"]
        assert [s["name"] for s in hist["2"]["sessions"]] == ["under-new"]

    def test_a_session_older_than_every_iteration_belongs_to_none(self, root):
        """Playtests predate the iteration feature on real projects. That is a
        state, not an error, and it must not be silently attributed."""
        _iteration(root, "only", "2026-02-01 10:00:00", {"game/p.gd": {"SPEED": "2"}})
        _session(root, "ancient", "2025-06-01 10:00:00")
        knob = tunables.measured(root)["tunables"][0]
        assert knob["history"][0]["sessions"] == []
        assert knob["verdict"] == "not measured"

    def test_the_same_name_in_two_files_stays_two_knobs(self, root):
        """Collapsing them would report one script's history against the
        other's knob, which is worse than reporting nothing."""
        _iteration(root, "both", "2026-01-01 10:00:00",
                   {"game/player.gd": {"SPEED": "1"}, "game/enemy.gd": {"SPEED": "9"}})
        got = tunables.measured(root)["tunables"]
        assert {t["key"] for t in got} == {"game/player.gd::SPEED", "game/enemy.gd::SPEED"}


class TestWhatItRefusesToClaim:
    def test_one_value_with_sessions_is_one_sample_not_measured(self, root):
        """Two sessions at ONE setting is not an experiment. The verdict is
        about evidence, never about which number to pick."""
        _iteration(root, "only", "2026-01-01 10:00:00", {"game/p.gd": {"SPEED": "1"}})
        _session(root, "a", "2026-01-02 10:00:00")
        _session(root, "b", "2026-01-03 10:00:00")
        assert tunables.measured(root)["tunables"][0]["verdict"] == "one sample"

    def test_two_values_actually_played_are_measured(self, root):
        _iteration(root, "before", "2026-01-01 10:00:00", {"game/p.gd": {"SPEED": "1"}})
        _session(root, "a", "2026-01-02 10:00:00")
        _iteration(root, "after", "2026-02-01 10:00:00", {"game/p.gd": {"SPEED": "2"}})
        _session(root, "b", "2026-02-02 10:00:00")
        assert tunables.measured(root)["tunables"][0]["verdict"] == "measured"

    def test_nothing_recommends_a_value(self, root):
        _iteration(root, "x", "2026-01-01 10:00:00", {"game/p.gd": {"SPEED": "1"}})
        _session(root, "a", "2026-01-02 10:00:00", ["death"] * 40)
        knob = tunables.measured(root)["tunables"][0]
        # No "better", no "recommended", no delta — a difference of means across
        # two playthroughs is not a result, and printing one as if it were is
        # the invented number this codebase keeps deleting.
        assert set(knob) == {"key", "file", "name", "current", "history",
                             "sessions", "verdict"}


class TestTheRoute:
    def test_it_answers_and_can_hide_the_unmeasured(self, root):
        _iteration(root, "x", "2026-01-01 10:00:00",
                   {"game/p.gd": {"SPEED": "1", "UNTOUCHED": "7"}})
        _session(root, "a", "2026-01-02 10:00:00")
        client = TestClient(app)
        body = client.get("/api/tunables").json()
        assert body["ok"] is True
        assert len(body["data"]["tunables"]) == 2
        lean = client.get("/api/tunables?measured_only=true").json()["data"]["tunables"]
        # both knobs were in force during that session, so both have it — the
        # filter drops knobs with NO sessions, which is the honest cut.
        assert all(t["sessions"] for t in lean)
