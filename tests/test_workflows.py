"""Workflow runs — the graph has to actually run, and the gates have to gate."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import artifacts, queue, workflows
from bgate_ui import api
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    """A dashboard client that presents this project's token, so the tests hold
    whether or not the suite happens to have the auth guard switched off."""
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app, headers={"x-bgate-token": api.ensure_token(root)})


def graph(*, threshold: int = 80, task: str = "Scoville's hit-detection fires from behind"):
    """task -> art agent -> consistency check -> human gate -> tech agent."""
    return {
        "workflow": {"id": "wf_test", "name": "Fix the hitbox", "category": "agent"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task", "config": {"text": task}},
            {"id": "art", "type": "agent.art", "label": "Art agent", "seat": "art",
             "brief": "Redraw the strike frame."},
            {"id": "cons", "type": "control.consistency", "label": "Consistency check",
             "config": {"threshold": threshold}},
            {"id": "gate", "type": "control.gate", "label": "Review gate"},
            {"id": "tech", "type": "agent.tech", "label": "Tech agent", "seat": "tech",
             "brief": "Rig it into Godot."},
        ],
        "edges": [
            {"from": ["task", "o"], "to": ["art", "i"]},
            {"from": ["art", "o"], "to": ["cons", "candidate"]},
            {"from": ["cons", "o"], "to": ["gate", "i"]},
            {"from": ["gate", "o"], "to": ["tech", "i"]},
        ],
        "dispatch": False,
    }


def node(run: dict, node_id: str) -> dict:
    return next(n for n in run["nodes"] if n["node_id"] == node_id)


def finish(root, item_id: int, result: str = "done") -> None:
    """Walk an item the way a real dispatched session does."""
    queue.set_status(root, item_id, "dispatched")
    queue.set_status(root, item_id, "done", result)


class TestStart:
    def test_creating_a_run_persists_every_node(self, root):
        run = workflows.start(root, graph(), actor="human@box")
        assert run["status"] == "running"
        assert {n["node_id"] for n in run["nodes"]} == {"task", "art", "cons", "gate", "tech"}
        # kinds are re-derived server-side, not trusted from the client
        assert node(run, "cons")["kind"] == "consistency"
        assert node(run, "gate")["kind"] == "gate"
        assert node(run, "task")["kind"] == "passive"
        # the passive input carried through; the first agent step is in flight;
        # everything after it is untouched
        assert node(run, "task")["status"] == "passed"
        assert node(run, "art")["status"] == "queued"
        assert node(run, "cons")["status"] == "pending"
        assert node(run, "tech")["status"] == "pending"

    def test_a_run_survives_a_reload(self, root):
        started = workflows.start(root, graph())
        again = workflows.get(root, started["id"], include_graph=True)
        assert again["status"] == "running"
        assert again["workflow_id"] == "wf_test"
        assert len(again["graph"]["nodes"]) == 5
        assert workflows.latest_for_workflow(root, "wf_test")["id"] == started["id"]

    def test_a_graph_with_no_agent_step_is_refused(self, root):
        with pytest.raises(ValueError):
            workflows.start(root, {"workflow": {"id": "x"},
                                   "nodes": [{"id": "t", "type": "input.task"}],
                                   "edges": []})


class TestAgentSteps:
    def test_agent_step_creates_one_item_carrying_the_run_id(self, root):
        run = workflows.start(root, graph())
        item_id = node(run, "art")["work_item_id"]
        item = queue.get(root, item_id)
        assert item["seat"] == "art"
        assert item["source"] == "workflow"
        assert item["source_ref"] == f"run:{run['id']}:art"
        # the brief carries the run and the task text, not "(no task text)"
        assert f"run #{run['id']}" in item["brief"]
        assert "hit-detection fires from behind" in item["brief"]
        assert workflows.for_work_item(root, item_id) == {
            "run_id": run["id"], "node_id": "art"}

    def test_node_status_follows_its_item(self, root):
        run = workflows.start(root, graph())
        item_id = node(run, "art")["work_item_id"]

        queue.set_status(root, item_id, "dispatched")
        run = workflows.advance(root, run["id"])
        assert node(run, "art")["status"] == "running"

        queue.set_status(root, item_id, "done", "redrew the strike frame")
        run = workflows.advance(root, run["id"])
        assert node(run, "art")["status"] == "passed"
        assert "redrew the strike frame" in node(run, "art")["detail"]
        # and the run moved on to the next step by itself
        assert node(run, "cons")["status"] == "queued"

    def test_a_failed_item_fails_the_run_and_skips_the_rest(self, root):
        run = workflows.start(root, graph())
        queue.set_status(root, node(run, "art")["work_item_id"], "failed", "off-model")
        run = workflows.advance(root, run["id"])
        assert run["status"] == "failed"
        assert node(run, "art")["status"] == "failed"
        assert node(run, "gate")["status"] == "skipped"


class TestConsistencyGate:
    def _to_consistency(self, root, threshold=80):
        run = workflows.start(root, graph(threshold=threshold))
        finish(root, node(run, "art")["work_item_id"], "frames drawn")
        run = workflows.advance(root, run["id"])
        assert node(run, "cons")["status"] == "queued"
        return run

    def test_breached_threshold_fails_the_run(self, root):
        run = self._to_consistency(root)
        cons_item = node(run, "cons")["work_item_id"]
        workflows.observe(root, run["id"], "cons", score=41,
                          detail="lost the headband")
        finish(root, cons_item, "reviewed 6 frames")
        run = workflows.advance(root, run["id"])

        assert node(run, "cons")["status"] == "failed"
        assert "41" in node(run, "cons")["detail"]
        assert run["status"] == "failed"
        # the gate downstream never even opened
        assert node(run, "gate")["status"] == "skipped"

    def test_met_threshold_passes_and_hands_over_to_the_gate(self, root):
        run = self._to_consistency(root)
        workflows.observe(root, run["id"], "cons", score=94)
        finish(root, node(run, "cons")["work_item_id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "cons")["status"] == "passed"
        assert node(run, "gate")["status"] == "running"

    def test_recorded_artifact_verdicts_are_the_evidence(self, root):
        """The real path: art_qa_verdict writes metadata.qa_review on the
        candidates the graded step produced, and the check reads the worst."""
        run = workflows.start(root, graph(threshold=80))
        art_item = node(run, "art")["work_item_id"]
        for name, score in (("strike_a", 91), ("strike_b", 62)):
            path = root / f"{name}.png"
            path.write_bytes(b"png")
            artifacts.register(root, name, path, producer="image_generate",
                               work_item_id=art_item)
            artifacts.record_check(root, path, "qa_review",
                                   {"verdict": "fail" if score < 80 else "pass",
                                    "score": score, "reasons": "palette drift"})
        finish(root, art_item)
        run = workflows.advance(root, run["id"])
        finish(root, node(run, "cons")["work_item_id"])
        run = workflows.advance(root, run["id"])
        # 62 is the worst of the two — one off-model frame is an off-model sheet
        assert node(run, "cons")["status"] == "failed"
        assert "62" in node(run, "cons")["detail"]

    def test_no_evidence_at_all_cannot_certify(self, root):
        run = self._to_consistency(root)
        finish(root, node(run, "cons")["work_item_id"], "looked fine to me")
        run = workflows.advance(root, run["id"])
        assert node(run, "cons")["status"] == "failed"
        assert "no on-model score" in node(run, "cons")["detail"]


class TestHumanGate:
    def _to_gate(self, root):
        run = workflows.start(root, graph())
        finish(root, node(run, "art")["work_item_id"])
        run = workflows.advance(root, run["id"])
        workflows.observe(root, run["id"], "cons", score=95)
        finish(root, node(run, "cons")["work_item_id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "gate")["status"] == "running"
        return run

    def test_the_gate_blocks_until_a_human_resolves_it(self, root):
        run = self._to_gate(root)
        # ticking forever changes nothing — the run is genuinely held
        for _ in range(3):
            run = workflows.advance(root, run["id"])
        assert node(run, "gate")["status"] == "running"
        assert node(run, "tech")["status"] == "pending"
        assert node(run, "tech")["work_item_id"] is None
        assert run["status"] == "running"
        assert [g["node_id"] for g in workflows.pending_gates(root)] == ["gate"]

        run = workflows.approve(root, run["id"], "gate", decision="approve",
                                actor="marta@box", note="looks right")
        assert node(run, "gate")["status"] == "passed"
        assert node(run, "gate")["info"]["approved_by"] == "marta@box"
        assert node(run, "tech")["status"] == "queued"
        assert workflows.pending_gates(root) == []

    def test_the_gate_refuses_an_agent(self, root):
        run = self._to_gate(root)
        with pytest.raises(PermissionError):
            workflows.approve(root, run["id"], "gate", actor="agent:item-7")
        assert node(workflows.get(root, run["id"]), "gate")["status"] == "running"

    def test_rejecting_a_gate_fails_the_run(self, root):
        run = self._to_gate(root)
        run = workflows.approve(root, run["id"], "gate", decision="reject",
                                actor="marta@box", note="arm is broken")
        assert run["status"] == "failed"
        assert node(run, "tech")["status"] == "skipped"

    def test_a_score_cannot_be_posted_to_a_gate(self, root):
        run = self._to_gate(root)
        with pytest.raises(ValueError):
            workflows.observe(root, run["id"], "gate", score=99)


class TestHttp:
    def test_run_lifecycle_over_http(self, client, root):
        started = client.post("/api/workflows/runs", json=graph()).json()
        assert started["ok"] is True, started
        run_id = started["data"]["id"]

        # the poll never re-ships the graph
        polled = client.post(f"/api/workflows/runs/{run_id}/advance").json()["data"]
        assert "graph" not in polled
        assert polled["nodes"]

        # a reload asks for it explicitly
        reloaded = client.get(f"/api/workflows/runs/{run_id}?graph=1").json()["data"]
        assert reloaded["graph"]["workflow"]["id"] == "wf_test"

        latest = client.get("/api/workflows/runs/latest?workflow_id=wf_test").json()
        assert latest["data"]["id"] == run_id

    def test_gate_approval_refuses_an_agent_over_http(self, client, root, monkeypatch):
        started = client.post("/api/workflows/runs", json=graph()).json()["data"]
        run_id = started["id"]
        finish(root, node(started, "art")["work_item_id"])
        run = client.post(f"/api/workflows/runs/{run_id}/advance").json()["data"]
        client.post(f"/api/workflows/runs/{run_id}/nodes/cons/observe",
                    json={"score": 90})
        finish(root, node(run, "cons")["work_item_id"])
        run = client.post(f"/api/workflows/runs/{run_id}/advance").json()["data"]
        assert node(run, "gate")["status"] == "running"

        gates = client.get("/api/workflows/gates").json()["data"]
        assert gates[0]["node_id"] == "gate"

        monkeypatch.setenv("BGATE_ACTOR", "agent:item-3")
        refused = client.post(
            f"/api/workflows/runs/{run_id}/nodes/gate/approve",
            json={"decision": "approve"})
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "forbidden"

        monkeypatch.setenv("BGATE_ACTOR", "marta@box")
        opened = client.post(
            f"/api/workflows/runs/{run_id}/nodes/gate/approve",
            json={"decision": "approve"}).json()["data"]
        assert node(opened, "gate")["status"] == "passed"

    def test_missing_run_is_a_clean_404(self, client, root):
        got = client.get("/api/workflows/runs/999")
        assert got.status_code == 404
        assert got.json()["error"]["code"] == "not_found"
