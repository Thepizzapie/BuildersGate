"""`GET /api/systems` — the resolution ladders, and what a session did to them.

The gameplay seat could not read its own systems without an agent to fetch them:
`causal_specs` / `causal_chains` were MCP-only. These tests pin the route and,
above all, pin that `order_verified` survives the trip — an unverified ladder's
passed gates are inferences, and a UI that cannot tell them apart from
observations is worse than one with no systems tab at all.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import db, project
from bgate_ui.app import app

SPEC = {
    "specs": {
        "attack": {
            "opens_on": ["attack_started"],
            "terminals": ["attack_failed", "attack_landed"],
            "landed": "attack_landed",
            "actor_key": "who",
            "ladder": [
                {"gate": "range", "fails_with_reason": "out_of_range"},
                {"gate": "facing", "fails_with_reason": "wrong_way"},
                {"gate": "damage", "fails_with_reason": None},
            ],
            "order_verified": True,
        }
    }
}


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    project.init(tmp_path, "Systems")
    yield tmp_path
    db.close_all()


def _spec(root, verified=True):
    body = json.loads(json.dumps(SPEC))
    body["specs"]["attack"]["order_verified"] = verified
    (root / ".bgate").mkdir(exist_ok=True)
    (root / ".bgate" / "causal_specs.json").write_text(json.dumps(body), encoding="utf-8")


def _session(root, events):
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_session (name, slug, status, started_at, duration_s) "
            "VALUES ('run', 'run', 'ready', '2026-01-01 10:00:00', 30)")
        sid = int(cur.lastrowid)
        for i, (kind, data) in enumerate(events):
            conn.execute(
                "INSERT INTO playtest_event (session_id, t, kind, data) VALUES (?,?,?,?)",
                (sid, float(i), kind, json.dumps(data)))
    return sid


def data(response) -> dict:
    body = response.json()
    assert body["ok"] is True, body
    return body["data"]


class TestTheIndex:
    def test_no_specs_answers_empty_and_says_what_would_make_one(self, root):
        body = data(TestClient(app).get("/api/systems"))
        assert body["systems"] == []
        # A project with no specs is the normal start, not an error — so the
        # answer carries the contract that produces one.
        assert "reason" in json.dumps(body["next"]).lower()

    def test_a_declared_system_reports_its_ladder_in_order(self, root):
        _spec(root)
        sys = data(TestClient(app).get("/api/systems"))["systems"][0]
        assert sys["name"] == "attack"
        assert [g["gate"] for g in sys["ladder"]] == ["range", "facing", "damage"]

    def test_the_latest_session_is_folded_through_the_ladder(self, root):
        _spec(root)
        _session(root, [
            ("attack_started", {"who": "player"}),
            ("attack_failed", {"who": "player", "reason": "wrong_way"}),
            ("attack_started", {"who": "player"}),
            ("attack_landed", {"who": "player"}),
        ])
        m = data(TestClient(app).get("/api/systems"))["systems"][0]["measured"]
        assert m["attempts"] == 2
        assert m["success_rate"] == 0.5
        # The gate that fails most IS the tuning question this module exists for.
        assert m["worst_gate"] == "facing"


class TestOrderVerifiedSurvivesTheTrip:
    def test_an_unverified_ladder_is_reported_as_such(self, root):
        """Passed gates from an unverified ladder are ASSUMPTIONS. A UI that
        renders them like observations is how you get plausible-and-wrong."""
        _spec(root, verified=False)
        _session(root, [
            ("attack_started", {"who": "p"}),
            ("attack_failed", {"who": "p", "reason": "wrong_way"}),
        ])
        sys = data(TestClient(app).get("/api/systems"))["systems"][0]
        assert sys["order_verified"] is False
        assert sys["measured"]["order_verified"] is False
        assert "unverified" in (sys["measured"]["warning"] or "").lower()

    def test_a_verified_ladder_carries_no_warning(self, root):
        _spec(root, verified=True)
        _session(root, [("attack_started", {"who": "p"}),
                        ("attack_landed", {"who": "p"})])
        m = data(TestClient(app).get("/api/systems"))["systems"][0]["measured"]
        assert m["order_verified"] is True and m["warning"] is None


class TestWithNoTelemetry:
    def test_the_ladder_still_reads_with_nothing_measured(self, root):
        """The spec is the DESIGN. It is worth reading before anyone has played,
        and reporting zero attempts is not the same as reporting no system."""
        _spec(root)
        body = data(TestClient(app).get("/api/systems"))
        assert body["session"] is None and body["events"] == 0
        assert body["systems"][0]["measured"]["attempts"] == 0
        assert body["systems"][0]["measured"]["worst_gate"] is None
