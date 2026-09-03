"""The run engine off the polling path: the sweep, reopen, and live consent.

Three ways a run used to stall or spend with nobody watching:

  * ``advance`` had exactly one driver — the canvas's poll — so a queue item
    that finished after the tab closed never reached its node and the run sat
    'running' forever (``for_work_item`` had zero production callers);
  * a run started with dispatch on kept auto-starting paid nodes after the
    user turned autopilot off, and an agent could manufacture that consent by
    POSTing a graph with ``dispatch=true``;
  * a worker node whose process died had reconcile (fail everything, restart
    the workflow) as its only exit — no per-node retry.

No network and no engine: ``generate.call_provider`` and ``wfnodes.run`` are
the seams, both stubbed, so a leaked guard shows up as a recorded call rather
than as a bill.
"""
from __future__ import annotations

from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import db, settings as _settings
from bgate_core.board import generate, queue, wfnodes, workflows
from bgate_ui.app import app


# ---------------------------------------------------------------------------
# Stubs — the two seams
# ---------------------------------------------------------------------------

class Provider:
    """A fake image provider that writes a byte and records the call."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, provider, model, prompt, out_path, **kw):
        from pathlib import Path

        self.calls.append({"provider": provider, "model": model,
                           "prompt": prompt})
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG-stub")
        return {"ok": True, "path": str(out), "bytes": out.stat().st_size,
                "provider": provider, "model": model, "seconds": 0.1,
                "usd": 0.03}


class Tool:
    """A fake tool executor, in the envelope ``wfnodes.run`` really returns."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, root, **kw):
        self.calls.append(kw)
        return {"ok": True, "error": "", "artifacts": [], "usd": 0.0,
                "provider": "", "model": "", "logical_name": "",
                "message": "the stub tool finished",
                "output": {"tool": "stub", "paths": [], "artifacts": []}}


@pytest.fixture()
def provider(monkeypatch):
    stub = Provider()
    monkeypatch.setattr(generate, "call_provider", stub)
    return stub


@pytest.fixture()
def tool(monkeypatch):
    stub = Tool()
    monkeypatch.setattr(wfnodes, "run", stub)
    return stub


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    """Nothing here may put a real Claude process on the machine."""
    from bgate_ui.agents import dispatch as _dispatch

    monkeypatch.setattr(_dispatch, "dispatch",
                        lambda root, item_id, **kw: {"ok": True, "stub": True})


@pytest.fixture(autouse=True)
def _settle(root):
    """No worker may outlive its test — it would write into a deleted tmp dir."""
    yield
    workflows.join(timeout=30)


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.setenv("BGATE_ACTOR", "marta@box")
    return TestClient(app)


def node(run: dict, node_id: str) -> dict:
    return next(n for n in run["nodes"] if n["node_id"] == node_id)


def finish(root, item_id: int, result: str = "done") -> None:
    """Walk an item the way a real dispatched session does."""
    queue.set_status(root, item_id, "dispatched")
    queue.set_status(root, item_id, "done", result)


def _orphan(root, run_id: int, node_id: str) -> None:
    """The row a killed process leaves behind: claimed, running, unowned."""
    workflows._set_node(root, run_id, node_id, "running",
                        message="generating candidates…")


def _backdate(root, run_id: int, node_id: str, minutes: int = 20) -> None:
    """Age a node's claim past the stall window without waiting for it."""
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE workflow_run_node SET updated_at = datetime('now', ?) "
            "WHERE run_id = ? AND node_id = ?",
            (f"-{minutes} minutes", run_id, node_id))


# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------

def image_graph() -> dict:
    """task -> one model card. The smallest graph that can spend money."""
    return {
        "workflow": {"id": "wf_sweep_img", "name": "One model"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "a paladin who files tickets"}},
            {"id": "gen", "type": "model.image", "label": "Image model",
             "config": {"provider": "krea", "model": "krea-2-medium"}},
        ],
        "edges": [{"from": ["task", "o"], "to": ["gen", "i"]}],
    }


def agent_graph() -> dict:
    """task -> art agent -> review gate."""
    return {
        "workflow": {"id": "wf_sweep_agent", "name": "Agent then gate"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "redraw the strike frame"}},
            {"id": "art", "type": "agent.art", "label": "Art", "seat": "art",
             "brief": "Redraw it."},
            {"id": "gate", "type": "control.gate", "label": "Review gate"},
        ],
        "edges": [
            {"from": ["task", "o"], "to": ["art", "i"]},
            {"from": ["art", "o"], "to": ["gate", "i"]},
        ],
    }


def agent_then_paid_graph() -> dict:
    """task -> art agent -> model card: the paid node the sweep must not fire."""
    return {
        "workflow": {"id": "wf_sweep_paid", "name": "Agent then model"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "a paladin"}},
            {"id": "art", "type": "agent.art", "label": "Art", "seat": "art",
             "brief": "Write the prompt."},
            {"id": "gen", "type": "model.image", "label": "Image model",
             "config": {"provider": "krea", "model": "krea-2-medium"}},
        ],
        "edges": [
            {"from": ["task", "o"], "to": ["art", "i"]},
            {"from": ["art", "o"], "to": ["gen", "i"]},
        ],
    }


def free_tool_graph() -> dict:
    return {
        "workflow": {"id": "wf_sweep_free", "name": "Free tool"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "look"}},
            {"id": "status", "type": "tool.godot.status", "label": "Status",
             "config": {}},
        ],
        "edges": [{"from": ["task", "o"], "to": ["status", "i"]}],
    }


# ---------------------------------------------------------------------------
# Consent — dispatch is frozen at start, autopilot is read live
# ---------------------------------------------------------------------------

class TestAutopilotGatesPaidAutoStart:
    def test_dispatch_alone_is_not_consent_when_autopilot_is_off(
            self, root, provider):
        _settings.set(root, "autopilot.on", False)
        run = workflows.start(root, image_graph(), actor="marta@box",
                              dispatch=True)
        for _ in range(3):
            run = workflows.advance(root, run["id"])
        workflows.join(run["id"])
        assert provider.calls == [], "a tick billed with autopilot off"
        held = node(run, "gen")
        assert held["status"] == "pending"
        assert "spends money" in held["detail"]
        assert run["status"] == "running", "the run is held, not finished"

    def test_flipping_autopilot_off_stops_an_open_run_billing(
            self, root, provider):
        # The switch is read live, not snapshotted: a graph left open must
        # stop the moment the user withdraws unattended work.
        run = workflows.start(root, agent_then_paid_graph(),
                              actor="marta@box", dispatch=True)
        _settings.set(root, "autopilot.on", False)
        finish(root, node(run, "art")["work_item_id"], "a grim paladin")
        run = workflows.advance(root, run["id"])
        workflows.join(run["id"])
        assert provider.calls == []
        assert node(run, "gen")["status"] == "pending"

    def test_the_person_presses_run_regardless_of_autopilot(
            self, root, provider):
        _settings.set(root, "autopilot.on", False)
        run = workflows.start(root, image_graph(), actor="marta@box",
                              dispatch=False)
        workflows.run_node(root, run["id"], "gen", actor="marta@box")
        workflows.join(run["id"])
        assert node(workflows.get(root, run["id"]), "gen")["status"] == "passed"
        assert len(provider.calls) == 1

    def test_an_agent_started_run_cannot_auto_bill(self, root, provider):
        # dispatch=True in the POST body is one line for an agent to write;
        # it must not be the yes a human never gave.
        run = workflows.start(root, image_graph(), actor="agent:item-9",
                              dispatch=True)
        run = workflows.advance(root, run["id"])
        workflows.join(run["id"])
        assert provider.calls == []
        assert node(run, "gen")["status"] == "pending"

    def test_a_human_started_dispatching_run_still_fires(self, root, provider):
        # autopilot defaults ON — Run workflow stays the yes to all of it.
        run = workflows.start(root, image_graph(), actor="marta@box",
                              dispatch=True)
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "gen")["status"] == "passed"
        assert len(provider.calls) == 1

    def test_a_free_node_ignores_the_switch(self, root, tool):
        _settings.set(root, "autopilot.on", False)
        run = workflows.start(root, free_tool_graph(), dispatch=False)
        workflows.join(run["id"])
        assert node(workflows.get(root, run["id"]),
                    "status")["status"] == "passed"
        assert len(tool.calls) == 1


# ---------------------------------------------------------------------------
# An upstream error is not an input
# ---------------------------------------------------------------------------

class TestErrorsAreNeverConsumed:
    def test_an_unresolved_reference_never_feeds_the_paid_node(
            self, root, provider):
        graph = image_graph()
        graph["nodes"].append({"id": "ctx", "type": "input.reference",
                               "label": "Reference",
                               "config": {"ref": "no-such-anchor"}})
        graph["edges"].append({"from": ["ctx", "o"], "to": ["gen", "i"]})
        run = workflows.start(root, graph, actor="marta@box", dispatch=True)
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "ctx")["status"] == "failed"
        assert "could not resolve reference" in node(run, "ctx")["detail"]
        assert node(run, "gen")["status"] == "skipped"
        assert run["status"] == "failed"
        assert provider.calls == [], (
            "the generation consumed an upstream error as input")


# ---------------------------------------------------------------------------
# The sweep — the production caller a closed canvas never had
# ---------------------------------------------------------------------------

class TestSweep:
    def test_it_advances_a_run_whose_item_closed_while_nobody_polled(self, root):
        run = workflows.start(root, agent_graph())
        finish(root, node(run, "art")["work_item_id"], "drawn")
        # nobody polls: the node still says queued
        assert node(workflows.get(root, run["id"]), "art")["status"] == "queued"

        out = workflows.sweep(root)
        assert run["id"] in out["advanced"]
        after = workflows.get(root, run["id"])
        assert node(after, "art")["status"] == "passed"
        # ...and the run took its next step: the gate is now blocking for real
        assert node(after, "gate")["status"] == "running"

    def test_it_finds_nothing_the_second_time(self, root):
        run = workflows.start(root, agent_graph())
        finish(root, node(run, "art")["work_item_id"])
        workflows.sweep(root)
        again = workflows.sweep(root)
        assert again["advanced"] == []
        assert again["stalled"] == []

    def test_an_item_still_in_flight_is_left_alone(self, root):
        run = workflows.start(root, agent_graph())
        queue.set_status(root, node(run, "art")["work_item_id"], "dispatched")
        out = workflows.sweep(root)
        assert out["advanced"] == []
        assert node(workflows.get(root, run["id"]), "art")["status"] == "queued"

    def test_a_deleted_item_fails_the_node_honestly(self, root):
        # ON DELETE SET NULL: deleting the item erases the node's pointer, so
        # _sync_items can never notice — the node sat in flight forever with
        # nothing underneath it.
        run = workflows.start(root, agent_graph())
        item_id = int(node(run, "art")["work_item_id"])
        with db.tx(root) as conn:
            conn.execute("DELETE FROM work_item WHERE id = ?", (item_id,))
        assert node(workflows.get(root, run["id"]),
                    "art")["work_item_id"] is None
        out = workflows.sweep(root)
        assert run["id"] in out["advanced"]
        after = workflows.get(root, run["id"])
        assert node(after, "art")["status"] == "failed"
        assert "can never arrive" in node(after, "art")["detail"]
        assert after["status"] == "failed"
        # ...and a failed run leaves the sweep's window
        assert workflows.sweep(root)["advanced"] == []

    def test_it_never_starts_a_paid_node_nobody_consented_to(
            self, root, provider):
        run = workflows.start(root, agent_then_paid_graph(), dispatch=False)
        finish(root, node(run, "art")["work_item_id"], "a grim paladin")
        out = workflows.sweep(root)
        assert run["id"] in out["advanced"]
        workflows.join(run["id"])
        after = workflows.get(root, run["id"])
        assert node(after, "art")["status"] == "passed"
        assert node(after, "gen")["status"] == "pending"
        assert provider.calls == [], "the sweep spent money nobody asked for"

    def test_a_dead_workers_node_is_stamped_not_failed(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, run["id"], "gen")
        _backdate(root, run["id"], "gen")
        out = workflows.sweep(root)
        assert out["stalled"] == [{"run_id": run["id"], "node_id": "gen"}]
        stamped = node(workflows.get(root, run["id"]), "gen")
        # stamped, never failed: this process cannot tell a dead worker from a
        # second live dashboard's, so the verdict stays human
        assert stamped["status"] == "running"
        assert "reopen" in stamped["detail"]
        assert stamped["info"]["stalled"] is True
        assert workflows.get(root, run["id"])["status"] == "running"

    def test_a_fresh_claim_is_not_called_stalled(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, run["id"], "gen")
        assert workflows.sweep(root)["stalled"] == []
        assert "reopen" not in node(workflows.get(root, run["id"]),
                                    "gen")["detail"]

    def test_a_worker_this_process_still_holds_is_not_stalled(
            self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, run["id"], "gen")
        _backdate(root, run["id"], "gen")
        pending: Future = Future()
        with workflows._INFLIGHT_LOCK:
            workflows._INFLIGHT[(int(run["id"]), "gen")] = pending
        try:
            assert workflows.sweep(root)["stalled"] == []
        finally:
            pending.set_result(None)
            with workflows._INFLIGHT_LOCK:
                workflows._INFLIGHT.pop((int(run["id"]), "gen"), None)

    def test_it_never_raises(self, root, monkeypatch):
        # The followup tick calls this; an exception there kills the sweep for
        # every other subsystem behind it.
        run = workflows.start(root, agent_graph())
        finish(root, node(run, "art")["work_item_id"])
        monkeypatch.setattr(workflows, "advance",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
        assert workflows.sweep(root) == {"advanced": [], "stalled": []}


# ---------------------------------------------------------------------------
# Reopen — the per-node way out of 'running'
# ---------------------------------------------------------------------------

class TestReopen:
    def test_it_returns_a_dead_node_to_pending_and_the_press_retries_it(
            self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, run["id"], "gen")
        out = workflows.reopen(root, run["id"], "gen", actor="marta@box")
        reopened = node(out, "gen")
        assert reopened["status"] == "pending"
        assert reopened["info"]["reopened_by"] == "marta@box"
        # paid + dispatch off: the retry still waits for the person's press
        assert "spends money" in reopened["detail"]
        assert provider.calls == []

        workflows.run_node(root, run["id"], "gen", actor="marta@box")
        workflows.join(run["id"])
        assert node(workflows.get(root, run["id"]), "gen")["status"] == "passed"
        assert len(provider.calls) == 1

    def test_a_free_node_retries_on_the_spot(self, root, tool):
        # A gate downstream keeps the run alive: reopen exists for a LIVE run
        # with one stuck card, and a settled run is restarted, not reopened.
        graph = free_tool_graph()
        graph["nodes"].append({"id": "gate", "type": "control.gate",
                               "label": "Review"})
        graph["edges"].append({"from": ["status", "o"], "to": ["gate", "i"]})
        run = workflows.start(root, graph, dispatch=False)
        workflows.join(run["id"])
        assert len(tool.calls) == 1
        _orphan(root, run["id"], "status")
        workflows.reopen(root, run["id"], "status", actor="marta@box")
        workflows.join(run["id"])
        assert node(workflows.get(root, run["id"]),
                    "status")["status"] == "passed"
        assert len(tool.calls) == 2

    def test_a_settled_run_is_restarted_not_reopened(self, root, tool):
        run = workflows.start(root, free_tool_graph(), dispatch=False)
        workflows.join(run["id"])
        workflows.advance(root, run["id"])
        assert workflows.get(root, run["id"])["status"] == "passed"
        _orphan(root, run["id"], "status")
        with pytest.raises(ValueError) as caught:
            workflows.reopen(root, run["id"], "status", actor="marta@box")
        assert "not running" in str(caught.value)

    def test_it_refuses_an_agent(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, run["id"], "gen")
        with pytest.raises(PermissionError):
            workflows.reopen(root, run["id"], "gen", actor="agent:item-7")
        assert node(workflows.get(root, run["id"]), "gen")["status"] == "running"

    def test_it_refuses_a_worker_this_process_still_holds(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, run["id"], "gen")
        pending: Future = Future()
        with workflows._INFLIGHT_LOCK:
            workflows._INFLIGHT[(int(run["id"]), "gen")] = pending
        try:
            with pytest.raises(ValueError) as caught:
                workflows.reopen(root, run["id"], "gen", actor="marta@box")
            assert "still working" in str(caught.value)
        finally:
            pending.set_result(None)
            with workflows._INFLIGHT_LOCK:
                workflows._INFLIGHT.pop((int(run["id"]), "gen"), None)

    def test_it_refuses_a_gate(self, root):
        run = workflows.start(root, agent_graph())
        finish(root, node(run, "art")["work_item_id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "gate")["status"] == "running"
        with pytest.raises(ValueError) as caught:
            workflows.reopen(root, run["id"], "gate", actor="marta@box")
        assert "waiting for a human" in str(caught.value)

    def test_it_refuses_a_step_owned_by_a_queue_item(self, root):
        run = workflows.start(root, agent_graph())
        queue.set_status(root, node(run, "art")["work_item_id"], "dispatched")
        run = workflows.advance(root, run["id"])
        assert node(run, "art")["status"] == "running"
        with pytest.raises(ValueError) as caught:
            workflows.reopen(root, run["id"], "art", actor="marta@box")
        assert "queue_reopen" in str(caught.value)

    def test_it_refuses_a_node_that_is_not_running(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        assert node(run, "gen")["status"] == "pending"
        with pytest.raises(ValueError):
            workflows.reopen(root, run["id"], "gen", actor="marta@box")

    def test_over_http_it_refuses_an_agent_and_serves_a_human(
            self, client, root, provider, monkeypatch):
        started = client.post("/api/workflows/runs",
                              json=dict(image_graph(),
                                        dispatch=False)).json()["data"]
        run_id = started["id"]
        _orphan(root, run_id, "gen")

        monkeypatch.setenv("BGATE_ACTOR", "agent:item-3")
        refused = client.post(
            f"/api/workflows/runs/{run_id}/nodes/gen/reopen")
        assert refused.status_code == 403, refused.json()
        assert refused.json()["error"]["code"] == "forbidden"

        monkeypatch.setenv("BGATE_ACTOR", "marta@box")
        reopened = client.post(
            f"/api/workflows/runs/{run_id}/nodes/gen/reopen")
        assert reopened.status_code == 200, reopened.json()
        assert node(reopened.json()["data"], "gen")["status"] == "pending"

        missing = client.post(
            f"/api/workflows/runs/{run_id}/nodes/nope/reopen")
        assert missing.status_code == 404
