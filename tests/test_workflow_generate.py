"""Multi-model comparison: generate nodes, picks, and running a graph BY HAND.

The claim under test is not "images appear". It is the scheduling contract:

  * sibling generate nodes genuinely OVERLAP — the whole point of comparing
    three models is not waiting for three round trips — while agent steps stay
    strictly one at a time, because those write to the game repo;
  * ▶ on a node runs that node and nothing else;
  * a pick BLOCKS, refuses a robot, and its choice actually reaches the next
    node rather than being a mood the run records;
  * money is refused before it is spent, and a provider failure fails the node
    with something a human can act on.

No network anywhere: `generate.call_provider` is the single seam every provider
call goes through, so the stub replaces it and writes a byte to disk instead.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from bgate_core import artifacts, generate, queue, spend, workflows
from bgate_ui.app import app


# ---------------------------------------------------------------------------
# Stub provider
# ---------------------------------------------------------------------------

class Provider:
    """A fake image provider that records WHEN each call was in flight."""

    def __init__(self, *, delay: float = 0.0, fail: str = ""):
        self.delay = delay
        self.fail = fail
        self.calls: list[dict] = []
        self.spans: list[tuple[float, float]] = []
        self._lock = threading.Lock()

    def __call__(self, provider, model, prompt, out_path, **kw):
        started = time.monotonic()
        with self._lock:
            self.calls.append({"provider": provider, "model": model,
                               "prompt": prompt, "out_path": out_path,
                               "style_refs": list(kw.get("style_refs") or ()),
                               "size": kw.get("size"), "seed": kw.get("seed")})
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.spans.append((started, time.monotonic()))
        if self.fail:
            return {"ok": False, "error": self.fail, "provider": provider,
                    "model": model}
        from pathlib import Path

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG-stub-" + model.encode())
        return {"ok": True, "path": str(out), "bytes": out.stat().st_size,
                "provider": provider, "model": model, "seconds": 0.1,
                "estimated_usd": 0.03}

    def overlapped(self) -> bool:
        """True if any two calls were in flight at the same moment."""
        for i, (a0, a1) in enumerate(self.spans):
            for b0, b1 in self.spans[i + 1:]:
                if a0 < b1 and b0 < a1:
                    return True
        return False


@pytest.fixture()
def provider(monkeypatch):
    stub = Provider(delay=0.15)
    monkeypatch.setattr(generate, "call_provider", stub)
    return stub


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.setenv("BGATE_ACTOR", "marta@box")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _settle(root):
    """No worker may outlive its test — it would write into a deleted tmp dir.

    Depends on `root` so it is torn down BEFORE the project it writes into.
    """
    yield
    workflows.join(timeout=30)


def node(run: dict, node_id: str) -> dict:
    return next(n for n in run["nodes"] if n["node_id"] == node_id)


def finish(root, item_id: int, result: str = "done") -> None:
    queue.set_status(root, item_id, "dispatched")
    queue.set_status(root, item_id, "done", result)


# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------

def fanout_graph(count: int = 1) -> dict:
    """task -> three models at once -> a human picks -> the tech seat rigs it."""
    return {
        "workflow": {"id": "wf_cmp", "name": "Which model draws the paladin"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "the Project Manager Paladin, idle, 16-bit"}},
            {"id": "a", "type": "model.generate", "label": "draft",
             "config": {"task_kind": "concept", "tier": "draft", "count": count}},
            {"id": "b", "type": "model.generate", "label": "standard",
             "config": {"task_kind": "concept", "tier": "standard", "count": count}},
            {"id": "c", "type": "model.generate", "label": "hero",
             "config": {"provider": "krea", "model": "krea-2-large", "count": count}},
            {"id": "pick", "type": "control.select", "label": "Pick one"},
            {"id": "tech", "type": "agent.tech", "label": "Rig it", "seat": "tech",
             "brief": "Import the chosen frame."},
        ],
        "edges": [
            {"from": ["task", "o"], "to": ["a", "i"]},
            {"from": ["task", "o"], "to": ["b", "i"]},
            {"from": ["task", "o"], "to": ["c", "i"]},
            {"from": ["a", "o"], "to": ["pick", "i"]},
            {"from": ["b", "o"], "to": ["pick", "i"]},
            {"from": ["c", "o"], "to": ["pick", "i"]},
            {"from": ["pick", "o"], "to": ["tech", "i"]},
        ],
        "dispatch": False,
    }


def serial_agents_graph() -> dict:
    """Two agent steps side by side — nothing about generate parallelism may
    let these two run at once."""
    return {
        "workflow": {"id": "wf_serial", "name": "Two seats, one repo"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "fix the hitbox"}},
            {"id": "art", "type": "agent.art", "label": "Art", "seat": "art"},
            {"id": "tech", "type": "agent.tech", "label": "Tech", "seat": "tech"},
        ],
        "edges": [
            {"from": ["task", "o"], "to": ["art", "i"]},
            {"from": ["task", "o"], "to": ["tech", "i"]},
        ],
        "dispatch": False,
    }


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------

class TestKinds:
    def test_kinds_are_rederived_from_the_type(self, root, provider):
        run = workflows.start(root, fanout_graph())
        assert node(run, "a")["kind"] == "generate"
        assert node(run, "pick")["kind"] == "pick"
        assert node(run, "tech")["kind"] == "agent"

    def test_a_step_cannot_declare_its_way_out_of_a_pick(self, root, provider):
        """The client's step registry says control.select is a 'gate'; a hostile
        client could say 'passive'. The TYPE decides, either way."""
        graph = fanout_graph()
        for spec in graph["nodes"]:
            if spec["id"] == "pick":
                spec["kind"] = "passive"
        run = workflows.start(root, graph)
        assert node(run, "pick")["kind"] == "pick"

    def test_a_generate_node_makes_no_queue_item(self, root, provider):
        run = workflows.start(root, fanout_graph())
        workflows.join(run["id"])
        assert queue.list_items(root) == []


# ---------------------------------------------------------------------------
# Parallelism — the load-bearing claim
# ---------------------------------------------------------------------------

class TestParallelism:
    def test_sibling_generate_nodes_overlap(self, root, provider):
        run = workflows.start(root, fanout_graph())
        # all three claimed on the first tick, none of them queued as work
        assert [node(run, n)["status"] for n in ("a", "b", "c")] == \
            ["running", "running", "running"]
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])

        assert [node(run, n)["status"] for n in ("a", "b", "c")] == \
            ["passed", "passed", "passed"]
        assert len(provider.calls) == 3
        assert provider.overlapped(), (
            "the three models ran one after another — a comparison you have to "
            "wait through is one nobody runs")
        # three different models were actually asked
        assert len({c["model"] for c in provider.calls}) == 3

    def test_agent_steps_still_run_one_at_a_time(self, root, provider):
        run = workflows.start(root, serial_agents_graph())
        live = [n for n in run["nodes"] if n["status"] == "queued"]
        assert len(live) == 1
        for _ in range(3):
            run = workflows.advance(root, run["id"])
        assert len([n for n in run["nodes"] if n["status"] == "queued"]) == 1

        first = next(n for n in run["nodes"] if n["status"] == "queued")
        finish(root, first["work_item_id"])
        run = workflows.advance(root, run["id"])
        assert len([n for n in run["nodes"] if n["status"] == "queued"]) == 1

    def test_the_fan_out_respects_the_concurrency_cap(self, root, provider):
        spend.set_budget(root, max_concurrent=1)
        run = workflows.start(root, fanout_graph())
        running = [n["node_id"] for n in run["nodes"] if n["status"] == "running"]
        assert len(running) == 1, "the budget's max_concurrent is the ceiling"
        workflows.join(run["id"])
        for _ in range(4):
            run = workflows.advance(root, run["id"])
            workflows.join(run["id"])
        assert [node(run, n)["status"] for n in ("a", "b", "c")] == \
            ["passed", "passed", "passed"]

    def test_a_second_tick_does_not_generate_twice(self, root, provider):
        run = workflows.start(root, fanout_graph())
        for _ in range(3):
            workflows.advance(root, run["id"])   # the dashboard polling
        workflows.join(run["id"])
        assert len(provider.calls) == 3


# ---------------------------------------------------------------------------
# Per-node run
# ---------------------------------------------------------------------------

def stepped_graph() -> dict:
    """task -> an agent step -> two generate siblings.

    The agent step is the pause the per-node tests need: while its queue item is
    unfinished nothing downstream can auto-start, so once it IS finished the
    test — not :func:`advance` — decides which sibling runs.
    """
    return {
        "workflow": {"id": "wf_step", "name": "Step by step"},
        "nodes": [
            {"id": "task", "type": "input.task", "label": "Task",
             "config": {"text": "a paladin"}},
            {"id": "writer", "type": "agent.narrative", "label": "Prompt writer",
             "seat": "narrative", "brief": "Write the prompt."},
            {"id": "a", "type": "model.generate", "label": "A",
             "config": {"provider": "krea", "model": "krea-2-medium"}},
            {"id": "b", "type": "model.generate", "label": "B",
             "config": {"provider": "krea", "model": "flux-1-dev"}},
        ],
        "edges": [
            {"from": ["task", "o"], "to": ["writer", "i"]},
            {"from": ["writer", "o"], "to": ["a", "i"]},
            {"from": ["writer", "o"], "to": ["b", "i"]},
        ],
        "dispatch": False,
    }


class TestRunOneNode:
    def _ready(self, root):
        """A run whose two generate nodes are ready but not yet ticked into."""
        run = workflows.start(root, stepped_graph())
        queue.set_status(root, node(run, "writer")["work_item_id"], "dispatched")
        queue.set_status(root, node(run, "writer")["work_item_id"], "done",
                         "a weary paladin in dented plate")
        return run

    def test_it_runs_that_node_and_nothing_else(self, root, provider):
        run = self._ready(root)
        run = workflows.run_node(root, run["id"], "a", actor="marta@box")
        assert node(run, "a")["status"] == "running"
        assert node(run, "b")["status"] == "pending", "the sibling was cascaded into"
        workflows.join(run["id"])

        state = workflows.get(root, run["id"])
        assert node(state, "a")["status"] == "passed"
        assert node(state, "b")["status"] == "pending"
        assert len(provider.calls) == 1
        assert provider.calls[0]["model"] == "krea-2-medium"
        # ...and the ordinary poll still carries on from there
        state = workflows.advance(root, run["id"])
        workflows.join(run["id"])
        assert node(workflows.get(root, run["id"]), "b")["status"] == "passed"

    def test_it_refuses_a_node_whose_inputs_are_not_satisfied(self, root, provider):
        run = workflows.start(root, stepped_graph())
        with pytest.raises(ValueError) as caught:
            workflows.run_node(root, run["id"], "a")
        assert "'writer'" in str(caught.value)
        assert provider.calls == []

    def test_it_refuses_a_node_that_is_already_running(self, root, provider):
        # The guard used to fire only for kind == "generate" and said "already
        # generating". A tool node that was mid-run answered the second click
        # with nothing at all, so a slow tool looked like a dead button and got
        # clicked again. Widening it meant one wording had to cover both, and
        # "running" is the word the status column already uses.
        run = self._ready(root)
        workflows.run_node(root, run["id"], "a")
        with pytest.raises(ValueError) as caught:
            workflows.run_node(root, run["id"], "a")
        assert "already running" in str(caught.value)

    def test_a_gate_is_resolved_not_run(self, root, provider):
        run = workflows.start(root, fanout_graph())
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        with pytest.raises(ValueError) as caught:
            workflows.run_node(root, run["id"], "pick")
        assert "resolved by a human" in str(caught.value)

    def test_agent_steps_stay_single_file_even_when_pressed_by_hand(self, root, provider):
        run = workflows.start(root, serial_agents_graph())
        waiting = next(n["node_id"] for n in run["nodes"] if n["status"] == "pending"
                       and n["kind"] == "agent")
        with pytest.raises(ValueError) as caught:
            workflows.run_node(root, run["id"], waiting)
        assert "one at a time" in str(caught.value)

    def test_run_one_node_over_http(self, client, root, provider):
        started = client.post("/api/workflows/runs", json=stepped_graph()).json()
        assert started["ok"] is True, started
        run_id = started["data"]["id"]
        writer = node(started["data"], "writer")["work_item_id"]
        finish(root, writer, "a weary paladin")

        ran = client.post(f"/api/workflows/runs/{run_id}/nodes/a/run")
        assert ran.status_code == 200, ran.json()
        assert node(ran.json()["data"], "a")["status"] == "running"
        assert node(ran.json()["data"], "b")["status"] == "pending"
        workflows.join(run_id)

        again = client.post(f"/api/workflows/runs/{run_id}/nodes/a/run")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "conflict"

        missing = client.post(f"/api/workflows/runs/{run_id}/nodes/nope/run")
        assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Picks
# ---------------------------------------------------------------------------

class TestPick:
    def _to_pick(self, root):
        run = workflows.start(root, fanout_graph())
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "pick")["status"] == "running"
        return run

    def test_the_pick_blocks_until_a_human_chooses(self, root, provider):
        run = self._to_pick(root)
        for _ in range(3):
            run = workflows.advance(root, run["id"])
        assert node(run, "pick")["status"] == "running"
        assert node(run, "tech")["status"] == "pending"
        assert node(run, "tech")["work_item_id"] is None
        assert run["status"] == "running"
        assert run["picks"] == ["pick"]
        assert [p["node_id"] for p in workflows.pending_picks(root)] == ["pick"]

    def test_it_offers_every_upstream_candidate(self, root, provider):
        run = self._to_pick(root)
        options = workflows.candidates(root, run["id"], "pick")
        assert len(options) == 3
        assert {o["node_id"] for o in options} == {"a", "b", "c"}
        assert all(o["artifact_id"] for o in options)

    def test_a_pick_refuses_an_agent(self, root, provider):
        run = self._to_pick(root)
        chosen = workflows.candidates(root, run["id"], "pick")[0]
        with pytest.raises(PermissionError):
            workflows.pick(root, run["id"], "pick",
                           artifact_id=chosen["artifact_id"], actor="agent:item-7")
        assert node(workflows.get(root, run["id"]), "pick")["status"] == "running"

    def test_approving_a_pick_with_candidates_is_refused(self, root, provider):
        """A yes is not an answer to 'which one'."""
        run = self._to_pick(root)
        with pytest.raises(ValueError) as caught:
            workflows.approve(root, run["id"], "pick", actor="marta@box")
        assert "pick(artifact_id=" in str(caught.value)

    def test_an_unrelated_artifact_cannot_be_picked(self, root, provider):
        run = self._to_pick(root)
        stray = root / "stray.png"
        stray.write_bytes(b"png")
        other = artifacts.register(root, "stray", stray, producer="test")
        with pytest.raises(ValueError) as caught:
            workflows.pick(root, run["id"], "pick", artifact_id=other["id"],
                           actor="marta@box")
        assert "not one of this pick's candidates" in str(caught.value)

    def test_the_choice_reaches_the_downstream_step(self, root, provider):
        run = self._to_pick(root)
        options = workflows.candidates(root, run["id"], "pick")
        chosen = next(o for o in options if o["node_id"] == "b")
        run = workflows.pick(root, run["id"], "pick",
                             artifact_id=chosen["artifact_id"],
                             actor="marta@box", note="best silhouette")

        picked = node(run, "pick")
        assert picked["status"] == "passed"
        assert picked["info"]["artifact_id"] == chosen["artifact_id"]
        assert picked["output"]["picked"]["path"] == chosen["path"]
        # and the next step was told WHICH candidate, by path and by id
        assert node(run, "tech")["status"] == "queued"
        brief = queue.get(root, node(run, "tech")["work_item_id"])["brief"]
        assert chosen["path"] in brief
        assert f"#{chosen['artifact_id']}" in brief

    def test_the_choice_becomes_a_downstream_generation_s_anchor(self, root, provider):
        """The other half of 'reaching the next node': a generate node behind a
        pick conditions on the chosen image."""
        graph = fanout_graph()
        graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "tech"]
        graph["edges"] = [e for e in graph["edges"] if e["to"][0] != "tech"]
        graph["nodes"].append(
            {"id": "final", "type": "model.generate", "label": "Final",
             "config": {"provider": "krea", "model": "krea-2-large",
                        "prompt": "turnaround of {input}"}})
        graph["edges"].append({"from": ["pick", "o"], "to": ["final", "i"]})
        run = workflows.start(root, graph)
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        chosen = workflows.candidates(root, run["id"], "pick")[0]
        run = workflows.pick(root, run["id"], "pick",
                             artifact_id=chosen["artifact_id"], actor="marta@box")
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])

        assert node(run, "final")["status"] == "passed"
        last = provider.calls[-1]
        assert last["style_refs"], "the picked candidate was not used as an anchor"
        assert chosen["path"].replace("/", "\\") in last["style_refs"][0][0].replace("/", "\\")

    def test_rejecting_every_candidate_fails_the_node_with_a_reason(self, root, provider):
        run = self._to_pick(root)
        run = workflows.pick(root, run["id"], "pick", reject=True,
                             actor="marta@box", note="all three lost the tabard")
        assert node(run, "pick")["status"] == "failed"
        assert "all three lost the tabard" in node(run, "pick")["detail"]
        assert run["status"] == "failed"
        assert node(run, "tech")["status"] == "skipped"

    def test_pick_over_http_refuses_an_agent_and_then_works(self, client, root,
                                                            provider, monkeypatch):
        started = client.post("/api/workflows/runs", json=fanout_graph()).json()["data"]
        run_id = started["id"]
        workflows.join(run_id)
        run = client.post(f"/api/workflows/runs/{run_id}/advance").json()["data"]
        assert node(run, "pick")["status"] == "running"

        options = client.get(
            f"/api/workflows/runs/{run_id}/nodes/pick/candidates").json()["data"]
        assert len(options) == 3
        assert client.get("/api/workflows/picks").json()["data"][0]["node_id"] == "pick"

        monkeypatch.setenv("BGATE_ACTOR", "agent:item-3")
        refused = client.post(f"/api/workflows/runs/{run_id}/nodes/pick/pick",
                              json={"artifact_id": options[0]["artifact_id"]})
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "forbidden"

        monkeypatch.setenv("BGATE_ACTOR", "marta@box")
        bad = client.post(f"/api/workflows/runs/{run_id}/nodes/pick/pick", json={})
        assert bad.status_code == 400

        done = client.post(f"/api/workflows/runs/{run_id}/nodes/pick/pick",
                           json={"artifact_id": options[1]["artifact_id"]}).json()["data"]
        assert node(done, "pick")["status"] == "passed"
        assert node(done, "pick")["info"]["artifact_id"] == options[1]["artifact_id"]


# ---------------------------------------------------------------------------
# Prompt on a wire
# ---------------------------------------------------------------------------

class TestPromptOnAWire:
    def prompt_graph(self, template: str = "") -> dict:
        config = {"provider": "krea", "model": "krea-2-medium"}
        if template:
            config["prompt"] = template
        return {
            "workflow": {"id": "wf_prompt", "name": "The LLM writes the prompt"},
            "nodes": [
                {"id": "task", "type": "input.task", "label": "Task",
                 "config": {"text": "a paladin who files tickets"}},
                {"id": "writer", "type": "agent.narrative", "label": "Prompt writer",
                 "seat": "narrative", "brief": "Write an image prompt."},
                {"id": "img", "type": "model.generate", "label": "Image",
                 "config": config},
            ],
            "edges": [
                {"from": ["task", "o"], "to": ["writer", "i"]},
                {"from": ["writer", "o"], "to": ["img", "i"]},
            ],
            "dispatch": False,
        }

    def test_an_upstream_text_output_becomes_the_prompt(self, root, provider):
        run = workflows.start(root, self.prompt_graph())
        written = ("weary paladin in dented plate, clipboard in one hand, "
                   "16-bit pixel art, side view")
        finish(root, node(run, "writer")["work_item_id"], written)
        run = workflows.advance(root, run["id"])
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])

        assert node(run, "img")["status"] == "passed"
        assert provider.calls[-1]["prompt"] == written

    def test_a_template_composes_with_the_wire(self, root, provider):
        run = workflows.start(root, self.prompt_graph("{input}, transparent background"))
        finish(root, node(run, "writer")["work_item_id"], "a weary paladin")
        run = workflows.advance(root, run["id"])
        workflows.join(run["id"])
        assert provider.calls[-1]["prompt"] == "a weary paladin, transparent background"

    def test_a_task_node_feeds_a_generate_node_directly(self, root, provider):
        run = workflows.start(root, fanout_graph())
        workflows.join(run["id"])
        assert all(c["prompt"] == "the Project Manager Paladin, idle, 16-bit"
                   for c in provider.calls)

    def test_a_node_with_no_prompt_at_all_fails_honestly(self, root, provider):
        graph = {
            "workflow": {"id": "wf_empty", "name": "No prompt"},
            "nodes": [{"id": "img", "type": "model.generate", "label": "Image",
                       "config": {"provider": "krea", "model": "z-image"}}],
            "edges": [],
        }
        run = workflows.start(root, graph)
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "img")["status"] == "failed"
        assert "no prompt" in node(run, "img")["detail"]
        assert provider.calls == []


# ---------------------------------------------------------------------------
# Money and failure
# ---------------------------------------------------------------------------

class TestMoneyAndFailure:
    def test_the_budget_refuses_before_anything_is_generated(self, root, provider):
        spend.set_budget(root, per_day_usd=0.001, enforced=1)
        run = workflows.start(root, fanout_graph())
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "a")["status"] == "failed"
        assert "budget" in node(run, "a")["detail"]
        assert provider.calls == [], "money was spent after the ceiling refused it"

    def test_spend_is_recorded_per_candidate(self, root, provider):
        run = workflows.start(root, fanout_graph(count=2))
        workflows.join(run["id"])
        assert len(provider.calls) == 6
        totals = spend.totals(root)
        assert totals["by_kind"]["image"] == pytest.approx(0.03 * 6, abs=1e-6)

    def test_a_provider_failure_fails_the_node_with_the_reason(self, root, monkeypatch):
        stub = Provider(fail="Krea has no API credit — top it up at krea.ai/settings")
        monkeypatch.setattr(generate, "call_provider", stub)
        run = workflows.start(root, fanout_graph())
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        failed = node(run, "a")
        assert failed["status"] == "failed"
        assert "no API credit" in failed["detail"]
        assert "krea-2-medium" in failed["detail"] or "z-image" in failed["detail"]
        assert run["status"] == "failed"

    def test_a_model_the_adapter_does_not_know_fails_before_spending(self, root, provider):
        graph = fanout_graph()
        for spec in graph["nodes"]:
            if spec["id"] == "c":
                spec["config"] = {"provider": "krea", "model": "krea-9-enormous"}
        run = workflows.start(root, graph)
        workflows.join(run["id"])
        run = workflows.advance(root, run["id"])
        assert node(run, "c")["status"] == "failed"
        assert "krea-9-enormous" in node(run, "c")["detail"]

    def test_candidates_are_registered_as_revisions_of_one_logical_name(self, root, provider):
        """One NODE's candidates are revisions of one name — and two nodes never
        share it. Two cards both labelled "Image model" used to register every
        candidate under a single logical name, which made the comparison's arms
        indistinguishable in the registry; "which model made this" is the only
        question the node exists to answer."""
        run = workflows.start(root, fanout_graph(count=3))
        workflows.join(run["id"])
        made = [r for r in artifacts.list_revisions(root, limit=100)
                if r["producer"] == generate.PRODUCER]
        by_name: dict[str, list] = {}
        for rev in made:
            by_name.setdefault(rev["logical_name"], []).append(rev)

        assert len(by_name) == 3, f"one name per node expected, got {sorted(by_name)}"
        for name, revisions in by_name.items():
            assert len(revisions) == 3, name
            assert sorted(r["revision"] for r in revisions) == [1, 2, 3]
            assert all(r["metadata"]["run_id"] == run["id"] for r in revisions)

    def test_two_cards_with_the_same_label_do_not_collide(self, root, provider):
        graph = fanout_graph(count=1)
        for spec in graph["nodes"]:
            if spec["id"] in ("a", "b"):
                spec["label"] = "Image model"      # the palette's default
        run = workflows.start(root, graph)
        workflows.join(run["id"])
        names = {r["logical_name"] for r in artifacts.list_revisions(root, limit=100)
                 if r["producer"] == generate.PRODUCER}
        assert len(names) == 3, f"identical labels collapsed into {sorted(names)}"
