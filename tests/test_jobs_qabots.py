"""Two audit findings: engine calls that block a request, and gates that cannot fail.

The Godot endpoints took their timeout straight from the request body and held
the HTTP request open for it, so the first half of this file pins the clamp and
the job model that replaced the blocking wait. The QA bot runner always
"succeeded" — it produced samples and no verdict — so the second half pins the
expectation engine, and in particular that a bot asserting nothing reports
``unknown`` rather than a green pass.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from bgate_adapters import godot as _real_godot
from bgate_core import db, jobs
from bgate_ui import api
from bgate_ui.app import app
from bgate_ui.routes import godot_ws, jobs as jobs_api, qa_bots


@pytest.fixture()
def client(root, monkeypatch):
    # The dashboard guard is read once at import time, so the token goes on the
    # client rather than relying on an env var set after the app was built.
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app, headers={"x-bgate-token": api.ensure_token(root)})


@pytest.fixture()
def game(root):
    """A minimal Godot project dir, so _project()/_probe() get past their guards."""
    game_dir = root / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "project.godot").write_text("[application]\n", encoding="utf-8")
    return game_dir


def _wait_for(predicate, timeout: float = 10.0):
    """Poll a background job's observable state. Jobs are threads, not futures."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the job to get there")


# ---------------------------------------------------------------------------
# The clamp
# ---------------------------------------------------------------------------

class TestTimeoutClamp:
    @pytest.mark.parametrize("raw,expected", [
        (0, 5), (-30, 5), (1, 5),          # below the floor
        (900, 600), (10 ** 9, 600),        # above the ceiling
        (45, 45), ("60", 60),              # inside, including the string form
    ])
    def test_clamps_into_range(self, raw, expected):
        assert godot_ws.clamp_timeout(raw, 180) == expected

    @pytest.mark.parametrize("raw", ["forever", None, {}, [], "12s", float("nan")])
    def test_non_numeric_falls_back_to_the_endpoint_default(self, raw):
        assert godot_ws.clamp_timeout(raw, 180) == 180

    def test_the_endpoint_never_passes_the_raw_body_value_to_the_engine(
            self, client, game, monkeypatch):
        seen = {}

        def fake_check(project_dir, timeout=180):
            seen["timeout"] = timeout
            return {"ok": True, "errors": []}

        monkeypatch.setattr(godot_ws._godot, "check_project", fake_check)
        got = client.post("/api/godot/check", json={"timeout": 86400})
        assert got.status_code == 200
        assert seen["timeout"] == 600

    def test_the_probe_timeout_is_clamped_too(self, client, game, monkeypatch):
        seen = {}

        def fake_run(script, project_dir=None, timeout=120):
            seen["timeout"] = timeout
            return {"ok": True, "stdout": "", "stderr": "", "errors": []}

        monkeypatch.setattr(qa_bots._godot, "run_script", fake_run)
        client.post("/api/qa-bots/run", json={"actions": [], "timeout": 3})
        assert seen["timeout"] == 5


# ---------------------------------------------------------------------------
# The job model
# ---------------------------------------------------------------------------

class TestJobs:
    def test_a_job_runs_to_done_and_reports_progress_on_the_way(self, client, root):
        gate = threading.Event()

        def work(job_id):
            jobs.progress(root, job_id, fraction=0.4, stage="halfway")
            gate.wait(10)
            return {"ok": True, "answer": 42}

        job_id = jobs.run_in_background(root, "test.slow", work)

        def midway():
            body = client.get(f"/api/jobs/{job_id}").json()["data"]
            return body if body["stage"] == "halfway" else None

        running = _wait_for(midway)
        assert running["state"] == "running"
        assert running["progress"] == pytest.approx(0.4)
        assert running["terminal"] is False

        gate.set()
        done = _wait_for(lambda: (lambda b: b if b["terminal"] else None)(
            client.get(f"/api/jobs/{job_id}").json()["data"]))
        assert done["state"] == "done"
        assert done["progress"] == pytest.approx(1.0)
        assert done["result"]["answer"] == 42
        assert done["error"] == ""

    def test_a_raising_job_captures_the_error_instead_of_vanishing(self, client, root):
        def work(_job_id):
            raise RuntimeError("the import ate itself")

        job_id = jobs.run_in_background(root, "test.doomed", work)
        body = _wait_for(lambda: (lambda b: b if b["terminal"] else None)(
            client.get(f"/api/jobs/{job_id}").json()["data"]))
        assert body["state"] == "failed"
        assert "the import ate itself" in body["error"]
        assert body["result"] == {}

    def test_unknown_job_is_a_404_in_the_envelope(self, client):
        got = client.get("/api/jobs/424242")
        assert got.status_code == 404
        assert got.json()["error"]["code"] == "not_found"

    def test_the_list_is_paginated_and_filterable(self, client, root):
        for _ in range(3):
            jobs.run_in_background(root, "test.listed", lambda _i: {"ok": True})
        jobs.run_in_background(root, "test.other", lambda _i: {"ok": True})
        _wait_for(lambda: all(
            j["status"] in jobs.TERMINAL for j in jobs.list_jobs(root)))

        page = client.get("/api/jobs?kind=test.listed&limit=2").json()
        assert page["page"]["total"] == 3
        assert len(page["data"]) == 2
        assert page["page"]["next_offset"] == 2
        assert {j["kind"] for j in page["data"]} == {"test.listed"}

    def test_cancel_stops_a_cooperative_job_and_persists_the_state(self, client, root):
        def work(job_id):
            for _ in range(500):
                if jobs_api.is_cancelled(job_id):
                    return jobs_api.cancelled_result("the loop")
                time.sleep(0.01)
            return {"ok": True, "note": "never cancelled"}

        job_id = jobs.run_in_background(root, "test.cancellable", work)
        _wait_for(lambda: jobs.get(root, job_id)["status"] == "running")

        got = client.post(f"/api/jobs/{job_id}/cancel")
        assert got.status_code == 200
        # Already inside the work function, so it stops at its own next check.
        assert got.json()["data"]["stopped"] is False

        body = _wait_for(lambda: (lambda b: b if b["terminal"] else None)(
            client.get(f"/api/jobs/{job_id}").json()["data"]))
        assert body["state"] == "cancelled"
        # And the reconciliation is written down, not recomputed per request.
        assert jobs.get(root, job_id)["status"] == "cancelled"

    def test_cancelling_a_finished_job_conflicts(self, client, root):
        job_id = jobs.run_in_background(root, "test.quick", lambda _i: {"ok": True})
        _wait_for(lambda: jobs.get(root, job_id)["status"] in jobs.TERMINAL)
        got = client.post(f"/api/jobs/{job_id}/cancel")
        assert got.status_code == 409
        assert got.json()["error"]["code"] == "conflict"


class TestAsyncGodot:
    def test_async_check_returns_202_and_the_job_carries_the_result(
            self, client, game, monkeypatch):
        monkeypatch.setattr(godot_ws._godot, "check_project",
                            lambda project_dir, timeout=180: {"ok": True, "errors": []})
        started = client.post("/api/godot/check?async=1", json={})
        assert started.status_code == 202
        job_id = started.json()["job_id"]
        assert started.json()["poll"] == f"/api/jobs/{job_id}"

        body = _wait_for(lambda: (lambda b: b if b["terminal"] else None)(
            client.get(f"/api/jobs/{job_id}").json()["data"]))
        assert body["state"] == "done"
        assert body["result"]["ok"] is True
        assert body["kind"] == "godot.check"

    def test_the_sync_shape_is_unchanged_for_the_existing_frontend(
            self, client, game, monkeypatch):
        monkeypatch.setattr(godot_ws._godot, "check_project",
                            lambda project_dir, timeout=180: {"ok": True, "errors": []})
        got = client.post("/api/godot/check", json={})
        assert got.status_code == 200
        assert got.json() == {"ok": True, "errors": []}  # flat, no envelope

    def test_a_missing_engine_degrades_instead_of_raising(self, client, game,
                                                          monkeypatch):
        def no_godot(*_a, **_kw):
            raise _real_godot.GodotNotFound("no Godot binary found; set BGATE_GODOT")

        monkeypatch.setattr(godot_ws._godot, "check_project", no_godot)
        got = client.post("/api/godot/check", json={})
        assert got.status_code == 200
        assert got.json()["ok"] is False
        assert "BGATE_GODOT" in got.json()["error"]


# ---------------------------------------------------------------------------
# QA bot expectations
# ---------------------------------------------------------------------------

def _samples(*rows: dict) -> list[dict]:
    base = {"player_x": 0.0, "opponent_x": 100.0, "distance": 100.0,
            "player_hp": 100, "opponent_hp": 100, "player_stamina": 100}
    return [{**base, "tick": i * 10, **row} for i, row in enumerate(rows)]


def _summary(samples: list[dict], *, has_fight: bool = True) -> dict:
    return {"ticks": 240, "requested_ticks": 240, "sample_count": len(samples),
            "samples": samples, "final": samples[-1] if samples else {},
            "notes": [], "has_fight": has_fight}


def _stub_probe(monkeypatch, summary, *, ok: bool = True):
    """Stand in for the headless engine, printing the one line the parser reads."""
    stdout = "Godot Engine v4.3\n"
    if summary is not None:
        stdout += "PROBE_JSON:" + json.dumps(summary) + "\n"

    def fake_run(script, project_dir=None, timeout=90):
        return {"ok": ok, "stdout": stdout, "stderr": "", "errors": [],
                "seconds": 0.4, "exit_code": 0 if ok else 1}

    monkeypatch.setattr(qa_bots._godot, "run_script", fake_run)


class TestComparators:
    @pytest.mark.parametrize("comparator,value,actual,expected", [
        ("eq", 100, 100, True), ("eq", 100, 99, False), ("eq", "left", "left", True),
        ("ne", 100, 99, True), ("ne", 100, 100, False),
        ("lt", 100, 99, True), ("lt", 100, 100, False),
        ("lte", 100, 100, True), ("lte", 100, 101, False),
        ("gt", 100, 101, True), ("gt", 100, 100, False),
        ("gte", 100, 100, True), ("gte", 100, 99, False),
        ("between", [10, 50], 30, True), ("between", [10, 50], 51, False),
        ("between", [10, 50], 10, True),
        ("contains", "left", "moved_left", True),
        ("contains", "right", "moved_left", False),
    ])
    def test_each_comparator(self, comparator, value, actual, expected):
        summary = _summary(_samples({"distance": actual}))
        results = qa_bots.evaluate(summary, qa_bots.normalise_expectations(
            [{"property": "distance", "comparator": comparator, "value": value}]))
        assert results[0]["ok"] is expected
        assert results[0]["actual"] == actual

    def test_contains_looks_inside_a_list_as_well_as_a_string(self):
        assert qa_bots.COMPARATORS["contains"](["jab", "hook"], "hook") is True
        assert qa_bots.COMPARATORS["contains"](["jab", "hook"], "kick") is False

    def test_the_comparator_set_is_exactly_the_documented_one(self):
        assert set(qa_bots.COMPARATORS) == {
            "eq", "ne", "lt", "lte", "gt", "gte", "between", "contains"}

    def test_an_unknown_comparator_is_rejected_not_skipped(self, client, game):
        got = client.post("/api/qa-bots/run", json={
            "actions": [],
            "expect": [{"property": "distance", "comparator": "approximately",
                        "value": 5}]})
        assert got.status_code == 400
        assert "approximately" in got.json()["error"]["message"]

    def test_at_tick_picks_the_nearest_sample_not_the_last(self):
        samples = _samples({"distance": 100}, {"distance": 50}, {"distance": 10})
        summary = _summary(samples)
        results = qa_bots.evaluate(summary, qa_bots.normalise_expectations(
            [{"property": "distance", "comparator": "eq", "value": 50,
              "at_tick": 11}]))
        assert results[0]["ok"] is True

    def test_a_property_the_probe_never_sampled_fails_with_a_reason(self):
        results = qa_bots.evaluate(_summary(_samples({})), [
            {"property": "combo_meter", "comparator": "gt", "value": 0,
             "at_tick": None, "label": "combo"}])
        assert results[0]["ok"] is False
        assert "combo_meter" in results[0]["reason"]


class TestVerdicts:
    def test_a_failing_expectation_attaches_the_offending_sample(self, client,
                                                                 game, monkeypatch):
        samples = _samples({"opponent_hp": 100}, {"opponent_hp": 100})
        _stub_probe(monkeypatch, _summary(samples))
        got = client.post("/api/qa-bots/run", json={
            "bot": "puncher", "actions": [{"action": "jab", "at_tick": 5}],
            "expect": [{"property": "opponent_hp", "comparator": "lt", "value": 100,
                        "label": "jab hurts the opponent"}]})
        body = got.json()
        assert body["verdict"] == "fail"
        failure = body["failures"][0]
        assert failure["label"] == "jab hurts the opponent"
        assert failure["actual"] == 100
        assert failure["sample"]["opponent_hp"] == 100     # the evidence
        assert failure["sample"]["tick"] == samples[-1]["tick"]
        assert "expected lt 100" in failure["reason"]

    def test_a_met_expectation_passes(self, client, game, monkeypatch):
        _stub_probe(monkeypatch, _summary(_samples({"opponent_hp": 88})))
        body = client.post("/api/qa-bots/run", json={
            "bot": "puncher", "actions": [],
            "expect": [{"property": "opponent_hp", "comparator": "lt",
                        "value": 100}]}).json()
        assert body["verdict"] == "pass"
        assert body["failures"] == []

    def test_a_bot_with_no_expectations_is_unknown_never_pass(self, client, game,
                                                              monkeypatch):
        _stub_probe(monkeypatch, _summary(_samples({})))
        body = client.post("/api/qa-bots/run",
                           json={"bot": "tourist", "actions": []}).json()
        assert body["ok"] is True          # the probe did drive the game...
        assert body["verdict"] == "unknown"  # ...and that proves nothing
        assert body["expectations"] == []

    def test_a_run_that_never_reached_the_game_is_error(self, client, game,
                                                        monkeypatch):
        _stub_probe(monkeypatch, None, ok=False)
        body = client.post("/api/qa-bots/run", json={
            "bot": "broken", "actions": [],
            "expect": [{"property": "distance", "comparator": "lt",
                        "value": 50}]}).json()
        assert body["verdict"] == "error"
        assert body["summary"] is None

    @pytest.mark.parametrize("verdicts,expected", [
        (["pass", "pass"], "pass"),
        (["pass", "fail"], "fail"),
        (["pass", "unknown"], "unknown"),
        (["unknown", "error"], "error"),
        (["fail", "error"], "fail"),
        ([], "unknown"),
    ])
    def test_the_aggregate_is_pessimistic(self, verdicts, expected):
        assert qa_bots.aggregate_verdict(verdicts) == expected

    def test_run_all_returns_one_verdict_a_gate_can_read(self, client, game,
                                                         monkeypatch):
        _stub_probe(monkeypatch, _summary(_samples({"opponent_hp": 90})))
        body = client.post("/api/qa-bots/run-all", json={"bots": [
            {"name": "hitter", "actions": [],
             "expect": [{"property": "opponent_hp", "comparator": "lt", "value": 100}]},
            {"name": "misser", "actions": [],
             "expect": [{"property": "opponent_hp", "comparator": "eq", "value": 0}]},
        ]}).json()["data"]
        assert body["verdict"] == "fail"
        assert body["counts"] == {"pass": 1, "fail": 1, "error": 0, "unknown": 0}
        assert [r["bot"] for r in body["runs"]] == ["hitter", "misser"]

    def test_run_all_with_an_unproven_bot_is_not_green(self, client, game,
                                                       monkeypatch):
        _stub_probe(monkeypatch, _summary(_samples({"opponent_hp": 90})))
        body = client.post("/api/qa-bots/run-all", json={"bots": [
            {"name": "hitter", "actions": [],
             "expect": [{"property": "opponent_hp", "comparator": "lt", "value": 100}]},
            {"name": "tourist", "actions": []},
        ]}).json()["data"]
        assert body["verdict"] == "unknown"

    def test_run_all_needs_bots(self, client, game):
        assert client.post("/api/qa-bots/run-all", json={"bots": []}).status_code == 400


class TestBaselines:
    def test_the_last_run_becomes_the_baseline_and_the_next_diffs_it(
            self, client, game, monkeypatch):
        expect = [{"property": "opponent_hp", "comparator": "lt", "value": 100,
                   "label": "the jab connects"}]

        _stub_probe(monkeypatch, _summary(_samples({"opponent_hp": 80})))
        first = client.post("/api/qa-bots/run", json={
            "bot": "puncher", "actions": [], "expect": expect}).json()
        assert first["verdict"] == "pass"
        assert first["baseline_diff"] is None      # nothing to compare against

        # The damage regressed to nothing: same bot, different game.
        _stub_probe(monkeypatch, _summary(_samples({"opponent_hp": 100})))
        second = client.post("/api/qa-bots/run", json={
            "bot": "puncher", "actions": [], "expect": expect}).json()
        diff = second["baseline_diff"]
        assert diff["baseline_id"] == first["run_id"]
        assert diff["verdict_was"] == "pass"
        assert diff["verdict_now"] == "fail"
        assert diff["regressed"] is True
        assert {"property": "opponent_hp", "was": 80, "now": 100, "delta": 20.0} \
            in diff["changed"]
        assert diff["flipped"] == [{"label": "the jab connects", "was_ok": True,
                                    "now_ok": False,
                                    "reason": diff["flipped"][0]["reason"]}]

    def test_only_one_baseline_survives_per_bot(self, client, root, game,
                                                monkeypatch):
        _stub_probe(monkeypatch, _summary(_samples({})))
        for _ in range(3):
            client.post("/api/qa-bots/run", json={"bot": "puncher", "actions": []})
        rows = db.connect(root).execute(
            "SELECT id, is_baseline FROM qa_bot_run WHERE bot = 'puncher' "
            "ORDER BY id").fetchall()
        assert [bool(r["is_baseline"]) for r in rows] == [False, False, True]

    def test_an_errored_run_does_not_erase_the_baseline(self, client, game,
                                                        monkeypatch):
        _stub_probe(monkeypatch, _summary(_samples({"opponent_hp": 70})))
        good = client.post("/api/qa-bots/run",
                           json={"bot": "puncher", "actions": []}).json()

        _stub_probe(monkeypatch, None, ok=False)
        client.post("/api/qa-bots/run", json={"bot": "puncher", "actions": []})

        still = client.get("/api/qa-bots/baseline?bot=puncher").json()["data"]
        assert still["id"] == good["run_id"]

    def test_history_is_paginated_and_carries_the_verdicts(self, client, game,
                                                           monkeypatch):
        _stub_probe(monkeypatch, _summary(_samples({"opponent_hp": 70})))
        for _ in range(2):
            client.post("/api/qa-bots/run", json={
                "bot": "puncher", "actions": [],
                "expect": [{"property": "opponent_hp", "comparator": "lt",
                            "value": 100}]})
        client.post("/api/qa-bots/run", json={"bot": "tourist", "actions": []})

        page = client.get("/api/qa-bots/runs?bot=puncher").json()
        assert page["page"]["total"] == 2
        assert [r["verdict"] for r in page["data"]] == ["pass", "pass"]
        assert page["data"][0]["final"]["opponent_hp"] == 70
        assert "samples" not in page["data"][0]   # history stays small

        everything = client.get("/api/qa-bots/runs?limit=1").json()
        assert everything["page"]["total"] == 3
        assert len(everything["data"]) == 1

    def test_no_baseline_yet_is_a_404_not_an_empty_pass(self, client, game):
        got = client.get("/api/qa-bots/baseline?bot=nobody")
        assert got.status_code == 404
        assert got.json()["error"]["code"] == "not_found"
