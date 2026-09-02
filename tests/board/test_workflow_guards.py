"""The guards on a workflow run: who may spend, what waits, and what gets out.

`bgate_core.board.wfnodes` is the table that decides whether a node costs money and
whether it may run beside another one, and it had no test at all — the two flags
that gate a provider bill and a last-write-wins repo edit were asserted by
nothing. Around them sat four holes that were each a way for the engine to spend
or stall without a person involved:

  * a paid node could be started by an AGENT, through the one route that took an
    actor and then ignored it;
  * a generate node started itself on every tick regardless of the dispatch
    switch, so opening a run to press ▶ on one card fired every other one;
  * a context or reference node that could not resolve went GREEN, and the
    generation behind it billed for a prompt with no subject and no anchor;
  * a node whose worker died with its process stayed 'running' for ever, with
    nothing in the product able to move it.

No network and no engine: `generate.call_provider` is the seam every provider
call goes through, and `wfnodes.run` is the seam every tool call goes through.
Both are stubbed, so a guard that leaks is visible as a RECORDED CALL rather
than as a bill.
"""
from __future__ import annotations

from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

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
                           "prompt": prompt,
                           "style_refs": list(kw.get("style_refs") or ())})
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG-stub")
        return {"ok": True, "path": str(out), "bytes": out.stat().st_size,
                "provider": provider, "model": model, "seconds": 0.1,
                "estimated_usd": 0.03}


class Tool:
    """A fake tool executor, in the envelope `wfnodes.run` really returns.

    Replacing `wfnodes.run` rather than `wfnodes.call_tool` keeps the MCP server
    — FastMCP, Blender, Godot, every provider adapter — out of the test process
    entirely, which is the whole reason that import is lazy in the first place.
    """

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


# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------

def image_graph() -> dict:
    """task -> one model card. The smallest graph that can spend money."""
    return {
        "workflow": {"id": "wf_guard", "name": "One model"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "a paladin who files tickets"}},
            {"id": "gen", "type": "model.image", "label": "Image model",
             "config": {"provider": "krea", "model": "krea-2-medium"}},
        ],
        "edges": [{"from": ["task", "o"], "to": ["gen", "i"]}],
    }


def paid_tool_graph() -> dict:
    """task -> the raw image tool, which the registry marks paid."""
    return {
        "workflow": {"id": "wf_paid", "name": "Paid tool"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "a paladin"}},
            {"id": "buy", "type": "tool.image.generate", "label": "Image tool",
             "config": {"filename": "paladin.png"}},
        ],
        "edges": [{"from": ["task", "o"], "to": ["buy", "i"]}],
    }


def gated_graph() -> dict:
    """art agent -> review gate. The two statuses reconcile must not touch."""
    return {
        "workflow": {"id": "wf_gate", "name": "Gate"},
        "nodes": [
            {"id": "art", "type": "agent.art", "label": "Art", "seat": "art",
             "brief": "Redraw it."},
            {"id": "gate", "type": "control.gate", "label": "Review gate"},
        ],
        "edges": [{"from": ["art", "o"], "to": ["gate", "i"]}],
    }


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------

# Every tool node that reaches a provider that bills. Written out rather than
# derived, because deriving it from the same flag it is checking would assert
# nothing: this list is the human answer to "which of these cost money", and a
# new provider tool that arrives without paid=True has to fail here.
PAID = {
    "tool.cinematic.shot", "tool.image.generate", "tool.item.generate",
    "tool.item.variants", "tool.music.generate", "tool.storyboard.auto",
    "tool.storyboard.frame", "tool.voice.speak",
}


class TestRegistryFlags:
    def test_exactly_the_billing_tools_are_marked_paid(self):
        assert {t for t, spec in wfnodes.REGISTRY.items() if spec.paid} == PAID

    def test_a_paid_node_never_takes_the_line(self):
        """A paid tool writes only NEW files, so it fans out like a model card.

        Marking one exclusive would serialise the one thing users run several of
        at once (three shots, four variants) for no collision that exists."""
        for node_type in PAID:
            assert wfnodes.REGISTRY[node_type].exclusive is False, node_type

    def test_engine_writes_are_exclusive_and_reads_are_not(self):
        for node_type in ("tool.godot.run", "tool.godot.import",
                          "tool.scene.wire", "tool.scene.set_property",
                          "tool.blender.rig", "tool.level.generate"):
            assert wfnodes.REGISTRY[node_type].exclusive is True, node_type
        for node_type in ("tool.godot.status", "tool.scene.outline",
                          "tool.image.status", "tool.level.plan"):
            assert wfnodes.REGISTRY[node_type].exclusive is False, node_type

    def test_the_palette_is_told_both_flags(self):
        """The catalogue is what badges a card PAID before anyone presses it."""
        cards = {c["type"]: c for c in wfnodes.catalogue()}
        assert set(cards) == set(wfnodes.REGISTRY)
        for node_type, spec in wfnodes.REGISTRY.items():
            assert cards[node_type]["paid"] == spec.paid
            assert cards[node_type]["exclusive"] == spec.exclusive

    def test_the_flags_are_read_from_the_table_not_from_the_graph(self):
        """A hand-POSTed node cannot declare its way out of either flag."""
        lying = {"type": "tool.music.generate", "kind": "tool",
                 "paid": False, "exclusive": False, "config": {}}
        assert workflows._tool_paid(lying) is True
        assert workflows._node_spends(lying) is True
        writer = {"type": "tool.scene.wire", "kind": "tool", "exclusive": False}
        assert workflows._tool_exclusive(writer) is True

    def test_an_unknown_tool_type_takes_the_line(self):
        """Absent from the table means unknown, and unknown must not fan out."""
        assert workflows._tool_exclusive({"type": "tool.from.the.future"}) is True
        assert workflows._tool_paid({"type": "tool.from.the.future"}) is False


class TestNodeTypes:
    def test_the_live_palette_type_generates(self):
        assert workflows.kind_for({"type": "model.image"}) == "generate"

    def test_the_legacy_names_still_generate(self):
        """A run stores a SNAPSHOT of the graph, and workflows are saved JSON.

        Dropping a name a saved graph still uses would make that node 'passive':
        green, instant, and generating nothing."""
        for legacy in ("model.generate", "llm.prompt"):
            assert workflows.kind_for({"type": legacy}) == "generate"

    def test_the_names_nothing_ever_emitted_are_gone(self):
        """Neither appears in any palette, template, fixture or doc in this
        repository — they guarded a graph that cannot exist."""
        for dead in ("image.generate", "gen.image"):
            assert workflows.kind_for({"type": dead}) != "generate"
        # ...and the tool-node type whose NAME is nearly one of them is intact
        assert workflows.kind_for({"type": "tool.image.generate"}) == "tool"


# ---------------------------------------------------------------------------
# Gap 1 — an agent may not spend
# ---------------------------------------------------------------------------

class TestOnlyAHumanSpends:
    def test_an_agent_cannot_run_a_paid_tool_node(self, root, tool):
        run = workflows.start(root, paid_tool_graph())
        assert workflows.spends_money(root, run["id"], "buy") is True
        with pytest.raises(PermissionError) as caught:
            workflows.run_node(root, run["id"], "buy", actor="agent:item-7")
        assert "spends money" in str(caught.value)
        assert tool.calls == [], "an agent bought something"
        assert node(workflows.get(root, run["id"]), "buy")["status"] == "pending"

    def test_an_agent_cannot_run_a_model_card(self, root, provider):
        run = workflows.start(root, image_graph())
        with pytest.raises(PermissionError):
            workflows.run_node(root, run["id"], "gen", actor="agent:item-7")
        assert provider.calls == []

    def test_a_free_node_is_not_gated_on_anybody(self, root, tool):
        """The gate is on MONEY, not on the ▶. A free status node that needed a
        human would be this guard growing past what it is for."""
        graph = paid_tool_graph()
        graph["nodes"][1] = {"id": "buy", "type": "tool.godot.status",
                             "label": "Godot status", "config": {}}
        run = workflows.start(root, graph, dispatch=False)
        assert workflows.spends_money(root, run["id"], "buy") is False
        workflows.join(run["id"])
        # it ran on the tick, with no human anywhere near it
        assert node(workflows.get(root, run["id"]), "buy")["status"] == "passed"
        assert len(tool.calls) == 1

    def test_a_human_runs_the_same_paid_node(self, root, tool):
        run = workflows.start(root, paid_tool_graph())
        workflows.run_node(root, run["id"], "buy", actor="marta@box")
        workflows.join(run["id"])
        assert node(workflows.get(root, run["id"]), "buy")["status"] == "passed"
        assert len(tool.calls) == 1

    def test_the_route_refuses_an_agent_and_then_serves_a_human(
            self, client, root, tool, monkeypatch):
        started = client.post("/api/workflows/runs",
                              json=dict(paid_tool_graph(),
                                        dispatch=False)).json()["data"]
        run_id = started["id"]

        monkeypatch.setenv("BGATE_ACTOR", "agent:item-3")
        refused = client.post(f"/api/workflows/runs/{run_id}/nodes/buy/run")
        assert refused.status_code == 403, refused.json()
        assert refused.json()["error"]["code"] == "forbidden"
        assert tool.calls == [], "the 403 was reported after the money was spent"

        monkeypatch.setenv("BGATE_ACTOR", "marta@box")
        ran = client.post(f"/api/workflows/runs/{run_id}/nodes/buy/run")
        assert ran.status_code == 200, ran.json()
        workflows.join(run_id)
        assert len(tool.calls) == 1

    def test_a_missing_node_is_still_a_404_not_a_403(self, client, root, tool):
        """The money check runs first, so it has to fail the same way the run
        it precedes does — otherwise a typo becomes a permissions mystery."""
        started = client.post("/api/workflows/runs",
                              json=paid_tool_graph()).json()["data"]
        missing = client.post(
            f"/api/workflows/runs/{started['id']}/nodes/nope/run")
        assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Gap 2 — the dispatch switch is the money switch
# ---------------------------------------------------------------------------

class TestDispatchGatesSpending:
    def test_a_model_card_waits_for_a_person_when_dispatch_is_off(
            self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        for _ in range(3):                       # the dashboard polling
            run = workflows.advance(root, run["id"])
        workflows.join(run["id"])
        assert provider.calls == [], "a poll spent money nobody asked for"
        held = node(run, "gen")
        assert held["status"] == "pending"
        assert "spends money" in held["detail"]
        assert held["info"]["awaiting_human"] is True
        assert run["status"] == "running", "the run is held, not finished"

    def test_the_person_presses_run_and_it_goes(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        run = workflows.run_node(root, run["id"], "gen", actor="marta@box")
        workflows.join(run["id"])
        assert node(workflows.get(root, run["id"]), "gen")["status"] == "passed"
        assert len(provider.calls) == 1

    def test_run_workflow_is_the_yes_to_all_of_it(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=True)
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "gen")["status"] == "passed"
        assert len(provider.calls) == 1

    def test_a_paid_tool_node_waits_on_the_same_switch(self, root, tool):
        run = workflows.start(root, paid_tool_graph(), dispatch=False)
        for _ in range(3):
            run = workflows.advance(root, run["id"])
        assert tool.calls == []
        assert node(run, "buy")["status"] == "pending"
        assert "spends money" in node(run, "buy")["detail"]

    def test_a_free_tool_node_does_not_wait(self, root, tool):
        """Only the bill is gated. A free node that needed a press would make
        every graph a sequence of clicks."""
        graph = paid_tool_graph()
        graph["nodes"][1] = {"id": "buy", "type": "tool.godot.status",
                             "label": "Godot status", "config": {}}
        run = workflows.start(root, graph, dispatch=False)
        workflows.join(run["id"])
        assert len(tool.calls) == 1


# ---------------------------------------------------------------------------
# Gap 3 — a context or reference node that failed is not a green node
# ---------------------------------------------------------------------------

def context_graph(node_spec: dict) -> dict:
    """task + one context/reference node -> a model card that consumes both."""
    return {
        "workflow": {"id": "wf_ctx", "name": "Context"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "a paladin"}},
            node_spec,
            {"id": "gen", "type": "model.image", "label": "Image model",
             "config": {"provider": "krea", "model": "krea-2-medium"}},
        ],
        "edges": [
            {"from": ["task", "o"], "to": ["gen", "i"]},
            {"from": [node_spec["id"], "o"], "to": ["gen", "i"]},
        ],
    }


def held_context_graph(node_spec: dict) -> dict:
    """The same context node, behind an agent step it takes input from.

    A passive node is resolved by whatever tick reaches it, so the only way a
    human can be standing at an UNRESOLVED one is with something unfinished in
    front of it — which is the state the ▶ exists for.
    """
    graph = context_graph(node_spec)
    graph["nodes"].insert(1, {"id": "art", "type": "agent.art", "label": "Art",
                              "seat": "art", "brief": "Draw it."})
    graph["edges"].append({"from": ["art", "o"], "to": [node_spec["id"], "i"]})
    return graph


BAD_BIBLE = {"id": "ctx", "type": "input.bible", "label": "Design bible",
             "config": {"section_id": "9999"}}
BAD_REF = {"id": "ctx", "type": "input.reference", "label": "Reference",
           "config": {"ref": "no-such-anchor"}}


class TestUnresolvedContextFails:
    @pytest.mark.parametrize("spec,says", [
        (BAD_BIBLE, "no bible section"),
        (BAD_REF, "could not resolve reference"),
    ])
    def test_it_fails_the_node_instead_of_carrying_nothing_through(
            self, root, provider, spec, says):
        run = workflows.start(root, context_graph(spec), dispatch=True)
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])

        failed = node(run, "ctx")
        assert failed["status"] == "failed", (
            "the node went green with an error in its output — the generation "
            "behind it then billed for a prompt with no world and no anchor")
        assert says in failed["detail"]
        assert run["status"] == "failed"
        assert node(run, "gen")["status"] == "skipped"
        assert provider.calls == []

    @pytest.mark.parametrize("spec,says", [
        (BAD_BIBLE, "no bible section"),
        (BAD_REF, "could not resolve reference"),
    ])
    def test_pressing_run_on_one_refuses_with_the_reason(self, root, provider,
                                                         spec, says):
        """Refused, not failed: the human is at the card and can fix the name."""
        run = workflows.start(root, held_context_graph(spec), dispatch=False)
        item = node(run, "art")["work_item_id"]
        queue.set_status(root, item, "dispatched")
        queue.set_status(root, item, "done", "drawn")
        with pytest.raises(ValueError) as caught:
            workflows.run_node(root, run["id"], "ctx", actor="marta@box")
        assert says in str(caught.value)
        assert node(workflows.get(root, run["id"]), "ctx")["status"] == "pending"

    def test_a_resolvable_reference_still_passes(self, root, provider):
        """The failure path must not have eaten the working one: a ref that
        names a real file inside the project resolves and rides the wire."""
        anchor = root / "game" / "assets" / "anchor.png"
        anchor.parent.mkdir(parents=True, exist_ok=True)
        anchor.write_bytes(b"\x89PNG")
        spec = {"id": "ctx", "type": "input.reference", "label": "Reference",
                "config": {"ref": "game/assets/anchor.png"}}
        run = workflows.start(root, context_graph(spec), dispatch=True)
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "ctx")["status"] == "passed"
        assert node(run, "gen")["status"] == "passed"
        assert provider.calls[0]["style_refs"], "the anchor never reached the model"


# ---------------------------------------------------------------------------
# Gap 4 — the way out of 'running'
# ---------------------------------------------------------------------------

def _orphan(root, run_id: int, node_id: str) -> None:
    """The row a killed process leaves behind: claimed, running, unowned.

    Written directly because that is the only way to produce it without killing
    a real worker mid-test — _INFLIGHT is per-process, so after a restart the
    node is 'running' and the registry that would have finished it is empty.
    """
    workflows._set_node(root, run_id, node_id, "running",
                        message="generating candidates…")


class TestReconcile:
    def test_it_releases_a_node_whose_worker_died(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, run["id"], "gen")
        # ...and the poll cannot move it: 'running' reads as work in flight
        assert node(workflows.advance(root, run["id"]), "gen")["status"] == "running"

        out = workflows.reconcile(root, run["id"])
        assert [r["node_id"] for r in out["released"]] == ["gen"]
        released = node(out["runs"][0], "gen")
        assert released["status"] == "failed"
        assert "can never arrive" in released["detail"]
        assert released["info"]["reconciled"] is True
        assert out["runs"][0]["status"] == "failed"

    def test_it_leaves_a_gate_alone(self, root):
        """A gate is 'running' BECAUSE it is waiting for a human. Releasing it
        would turn every unattended approval into a failed run overnight."""
        run = workflows.start(root, gated_graph())
        item = node(run, "art")["work_item_id"]
        queue.set_status(root, item, "dispatched")
        queue.set_status(root, item, "done", "drawn")
        run = workflows.advance(root, run["id"])
        assert node(run, "gate")["status"] == "running"

        assert workflows.reconcile(root, run["id"])["released"] == []
        assert node(workflows.get(root, run["id"]), "gate")["status"] == "running"

    def test_it_leaves_an_agent_step_alone(self, root):
        """An agent step's status belongs to its queue item, which _sync_items
        already reconciles against the queue."""
        run = workflows.start(root, gated_graph())
        queue.set_status(root, node(run, "art")["work_item_id"], "dispatched")
        run = workflows.advance(root, run["id"])
        assert node(run, "art")["status"] == "running"

        assert workflows.reconcile(root, run["id"])["released"] == []
        assert node(workflows.get(root, run["id"]), "art")["status"] == "running"

    def test_it_leaves_a_worker_this_process_still_holds(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, run["id"], "gen")
        pending: Future = Future()
        with workflows._INFLIGHT_LOCK:
            workflows._INFLIGHT[(int(run["id"]), "gen")] = pending
        try:
            assert workflows.reconcile(root, run["id"])["released"] == []
            assert node(workflows.get(root, run["id"]), "gen")["status"] == "running"
        finally:
            pending.set_result(None)
            with workflows._INFLIGHT_LOCK:
                workflows._INFLIGHT.pop((int(run["id"]), "gen"), None)

    def test_the_sweep_covers_every_live_run(self, root, provider):
        first = workflows.start(root, image_graph(), dispatch=False)
        second = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, first["id"], "gen")
        _orphan(root, second["id"], "gen")

        out = workflows.reconcile(root)
        assert {r["run_id"] for r in out["released"]} == {first["id"], second["id"]}
        assert all(r["status"] == "failed" for r in out["runs"])

    def test_reconciling_twice_changes_nothing(self, root, provider):
        run = workflows.start(root, image_graph(), dispatch=False)
        _orphan(root, run["id"], "gen")
        workflows.reconcile(root, run["id"])
        assert workflows.reconcile(root, run["id"])["released"] == []

    def test_over_http(self, client, root, provider):
        started = client.post("/api/workflows/runs",
                              json=dict(image_graph(), dispatch=False)).json()["data"]
        _orphan(root, started["id"], "gen")

        one = client.post(f"/api/workflows/runs/{started['id']}/reconcile")
        assert one.status_code == 200, one.json()
        assert [r["node_id"] for r in one.json()["data"]["released"]] == ["gen"]

        assert client.post("/api/workflows/reconcile").json()["data"]["released"] == []
        assert client.post("/api/workflows/runs/999/reconcile").status_code == 404
