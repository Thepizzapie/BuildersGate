"""The Agents console: one conversation, one graph payload, one autopilot.

Three things here are worth a test and nothing else really is.

The FENCE. A work item's title is capped at 80 characters and a message typed
into the console is routinely longer, so the transcript can only redisplay what
the brief kept. If the fence stops round-tripping, every conversation in the
product silently starts truncating at the first line and nothing else notices.

The PAYLOAD. The view makes exactly one request per poll. Every key it paints
has to be in that response, because a missing key renders as an empty panel
rather than an error.

AUTO-DEPLOY. It spawns agents with nobody watching, so its refusals to spawn
are the interesting behaviour: it holds escalations back, it backs off instead
of hot-looping a refusal, and a floor-level refusal ends the whole pass.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import db, queue as _queue, workspace as _ws
from bgate_ui import autodeploy
from bgate_ui.app import app
from bgate_ui.routes import console as _console


async def _mcp(tool: str, /, **kwargs) -> dict:
    """Call an MCP tool the way a client does — through FastMCP's dispatch, so
    the schema and the wrapper are covered too, not just the function body."""
    import json

    from bgate_mcp import server

    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    autodeploy.reset()
    return TestClient(app)


@pytest.fixture()
def spawned(monkeypatch):
    """Record dispatches instead of starting a claude process."""
    calls: list[int] = []

    def fake(root, item_id, **kw):
        calls.append(int(item_id))
        _queue.set_status(root, int(item_id), "dispatched")
        return {"ok": True, "item_id": int(item_id), "pid": 4242}

    monkeypatch.setattr("bgate_ui.dispatch.dispatch", fake)
    monkeypatch.setattr("bgate_ui.dispatch.find_claude", lambda: "claude")
    return calls


# ---------------------------------------------------------------------------
# The fence — the human's own words survive the 80-character title
# ---------------------------------------------------------------------------
class TestTheMessageSurvives:
    LONG = ("the hub screen feels dead — give it parallax, a day/night tint, and "
            "make the door hum when you can afford to open it, which is the bit "
            "that actually matters and is also well past eighty characters")

    def test_the_brief_fences_the_message(self, client, root, spawned):
        got = client.post("/api/console/say", json={"text": self.LONG}).json()
        item = _queue.get(root, got["turn_id"])
        assert _console.SAID_OPEN in item["brief"]
        assert _console.said(item["brief"]) == self.LONG

    def test_the_transcript_returns_it_whole(self, client, spawned):
        client.post("/api/console/say", json={"text": self.LONG})
        turn = client.get("/api/console/state").json()["turns"][-1]
        assert turn["said"] == self.LONG
        assert len(turn["title"]) <= 80        # the title really was cut
        assert turn["title"] != turn["said"]

    def test_a_turn_with_no_fence_falls_back_to_its_title(self, root, client):
        _queue.add(root, "director", "typed before the fence existed",
                   brief="no fence here", source=_console.CHAT_SOURCE)
        turn = client.get("/api/console/state").json()["turns"][-1]
        assert turn["said"] == "typed before the fence existed"

    def test_the_director_is_told_to_stamp_the_lineage_line(self, client, root, spawned):
        got = client.post("/api/console/say", json={"text": "make a thing"}).json()
        brief = _queue.get(root, got["turn_id"])["brief"]
        from bgate_ui.routes.orchestrator import DELEGATED_FROM

        assert f"{DELEGATED_FROM}{got['turn_id']}" in brief
        # and it must answer, not just queue silently
        assert "queue_complete" in brief

    def test_an_empty_message_is_refused_before_anything_is_created(self, client, root):
        assert client.post("/api/console/say", json={"text": "   "}).status_code == 400
        rows = db.connect(root).execute(
            "SELECT count(*) FROM work_item WHERE source = 'chat'").fetchone()
        assert rows[0] == 0


# ---------------------------------------------------------------------------
# One request paints the whole cockpit
# ---------------------------------------------------------------------------
class TestOnePayload:
    def test_every_key_the_view_paints_is_present(self, client):
        body = client.get("/api/console/state").json()
        for key in ("turns", "items", "agents", "lineage", "gates", "steps",
                    "autopilot", "floor"):
            assert key in body, f"the console cannot paint without {key}"

    def test_the_floor_tally_counts_the_board(self, client, root):
        _queue.add(root, "art", "one")
        _queue.add(root, "tech", "two")
        floor = client.get("/api/console/state").json()["floor"]
        assert floor["queued"] == 2
        assert floor["running"] == 0

    def test_chat_turns_are_not_repeated_as_board_items(self, client, spawned):
        client.post("/api/console/say", json={"text": "hello"})
        body = client.get("/api/console/state").json()
        assert body["turns"], "the turn is missing from the conversation"
        # The graph filters source='chat' itself, but the payload must make that
        # possible — a turn drawn twice is a turn that looks like two tasks.
        assert all("source" in i for i in body["items"])

    def test_an_open_qa_gate_is_a_gate(self, client, root):
        target = _queue.add(root, "art", "draw the thing")
        _queue.add(root, "qa", "QA gate: verify #%d" % target["id"],
                   source="qa-gate", source_ref=str(target["id"]))
        gates = client.get("/api/console/state").json()["gates"]
        assert any(g["kind"] == "qa" and g["over_item_id"] == target["id"]
                   for g in gates)

    def test_an_escalation_is_marked_blocking(self, client, root):
        target = _queue.add(root, "art", "draw the thing")
        _queue.add(root, "director", "QA loop broken",
                   source="qa-gate-escalation", source_ref=str(target["id"]))
        gate = [g for g in client.get("/api/console/state").json()["gates"]
                if g["kind"] == "escalation"][0]
        assert gate["blocking"] is True

    def test_a_candidate_from_running_work_is_a_gate(self, client, root):
        from bgate_core import artifacts

        item = _queue.add(root, "art", "draw the hero")
        image = root / "hero.png"
        image.write_bytes(b"hero")
        artifacts.register(root, "hero", image, producer="image_generate",
                           work_item_id=item["id"])
        gates = client.get("/api/console/state").json()["gates"]
        assert any(g["kind"] == "art" and g["item_id"] == item["id"] for g in gates)

    def test_a_candidate_from_finished_work_is_not_a_gate(self, client, root):
        """The regression this filter exists for.

        Every candidate stays 'candidate' until somebody dispositions it, so a
        project with a used art library grew a permanent wall of approval nodes
        hanging off runs that ended weeks ago — a review backlog drawn as a
        blocked floor. The review queue in Assets owns that; the graph shows
        what the work in flight is waiting on.
        """
        from bgate_core import artifacts

        item = _queue.add(root, "art", "drew the hero last week")
        _queue.set_status(root, item["id"], "done", result="shipped")
        image = root / "old.png"
        image.write_bytes(b"old")
        artifacts.register(root, "old_hero", image, producer="image_generate",
                           work_item_id=item["id"])
        gates = client.get("/api/console/state").json()["gates"]
        assert not [g for g in gates if g["kind"] == "art"]

    def test_an_unattached_candidate_is_not_a_gate(self, client, root):
        from bgate_core import artifacts

        image = root / "loose.png"
        image.write_bytes(b"loose")
        artifacts.register(root, "loose", image, producer="image_generate")
        assert not [g for g in client.get("/api/console/state").json()["gates"]
                    if g["kind"] == "art"]

    def test_delegated_children_are_reachable_through_lineage(self, client, root, spawned):
        from bgate_ui.routes.orchestrator import DELEGATED_FROM

        turn = client.post("/api/console/say", json={"text": "split this"}).json()
        child = _queue.add(root, "art", "the piece",
                           brief=f"{DELEGATED_FROM}{turn['turn_id']}\n\ndo it")
        parents = client.get("/api/console/state").json()["lineage"]["parents"]
        assert parents[str(child["id"])] == turn["turn_id"]


# ---------------------------------------------------------------------------
# Auto-deploy
# ---------------------------------------------------------------------------
class TestAutoDeploy:
    def test_it_is_off_until_asked(self, client):
        assert client.get("/api/console/autopilot").json()["on"] is False

    def test_the_switch_persists_to_the_project(self, client, root):
        client.post("/api/console/autopilot", json={"on": True})
        assert _ws.get(root, autodeploy.SEAT, autodeploy.KEY)["on"] is True
        assert autodeploy.enabled(root) is True

    def test_turning_it_on_dispatches_now_not_in_four_seconds(self, client, root, spawned):
        item = _queue.add(root, "art", "queued work")
        body = client.post("/api/console/autopilot", json={"on": True}).json()
        assert item["id"] in body["tick"]["dispatched"]
        assert spawned == [item["id"]]

    def test_off_means_off(self, client, root, spawned):
        _queue.add(root, "art", "queued work")
        client.post("/api/console/autopilot", json={"on": False})
        autodeploy.tick(root)
        assert spawned == []

    def test_priority_goes_first(self, client, root, spawned):
        low = _queue.add(root, "art", "later", priority=0)
        high = _queue.add(root, "tech", "now", priority=9)
        autodeploy.set_enabled(root, True)
        autodeploy.tick(root)
        assert spawned[0] == high["id"] and low["id"] in spawned

    def test_an_escalation_is_never_auto_dispatched(self, client, root, spawned):
        _queue.add(root, "director", "QA loop broken — you decide",
                   source="qa-gate-escalation", source_ref="7")
        autodeploy.set_enabled(root, True)
        autodeploy.tick(root)
        assert spawned == [], "autopilot spent an agent on a human decision"

    def test_a_refusal_is_not_retried_on_the_next_tick(self, root, monkeypatch):
        item = _queue.add(root, "art", "will refuse")
        calls: list[int] = []

        def refuse(root_, item_id, **kw):
            calls.append(int(item_id))
            return {"ok": False, "code": "out_of_scope", "error": "below the cut line"}

        monkeypatch.setattr("bgate_ui.dispatch.dispatch", refuse)
        monkeypatch.setattr("bgate_ui.dispatch.find_claude", lambda: "claude")
        autodeploy.reset()
        autodeploy.set_enabled(root, True)
        autodeploy.tick(root)
        autodeploy.tick(root)
        assert calls == [item["id"]], "a refused item is being hot-looped"
        assert autodeploy.state(root)["last_refusal"]["item_id"] == item["id"]

    def test_a_floor_refusal_stops_the_whole_pass(self, root, monkeypatch):
        for i in range(4):
            _queue.add(root, "art", f"item {i}")
        calls: list[int] = []

        def full(root_, item_id, **kw):
            calls.append(int(item_id))
            return {"ok": False, "code": "concurrency_limit",
                    "error": "4 agents already running — the cap is 4"}

        monkeypatch.setattr("bgate_ui.dispatch.dispatch", full)
        monkeypatch.setattr("bgate_ui.dispatch.find_claude", lambda: "claude")
        autodeploy.reset()
        autodeploy.set_enabled(root, True)
        autodeploy.tick(root)
        assert len(calls) == 1, "it kept asking after the floor said it was full"

    def test_a_missing_cli_refuses_once_not_once_per_item(self, root, monkeypatch):
        for i in range(3):
            _queue.add(root, "art", f"item {i}")
        monkeypatch.setattr("bgate_ui.dispatch.find_claude", lambda: None)
        called: list[int] = []
        monkeypatch.setattr("bgate_ui.dispatch.dispatch",
                            lambda *a, **k: called.append(1))
        autodeploy.reset()
        autodeploy.set_enabled(root, True)
        got = autodeploy.tick(root)
        assert called == []
        assert got["refused"][0]["code"] == "no_cli"

    def test_the_refusal_is_readable_from_the_state_the_view_polls(self, client, root, monkeypatch):
        _queue.add(root, "art", "will refuse")
        monkeypatch.setattr("bgate_ui.dispatch.find_claude", lambda: "claude")
        monkeypatch.setattr(
            "bgate_ui.dispatch.dispatch",
            lambda *a, **k: {"ok": False, "code": "dirty_tree",
                             "error": "3 uncommitted change(s) in the tree"})
        autodeploy.reset()
        client.post("/api/console/autopilot", json={"on": True})
        state = client.get("/api/console/state").json()["autopilot"]
        assert state["on"] is True
        assert state["last_refusal"]["code"] == "dirty_tree"
        assert "uncommitted" in state["last_refusal"]["message"]

    def test_the_toggle_refuses_anything_that_is_not_a_boolean(self, client):
        assert client.post("/api/console/autopilot", json={"on": "yes"}).status_code == 400


# ---------------------------------------------------------------------------
# The steer channel — how the director talks to its own workers
# ---------------------------------------------------------------------------
class TestSteerBox:
    def test_a_message_survives_being_written_and_claimed(self, root):
        from bgate_core import steerbox

        steerbox.post(root, 41, "use the pinned ref", by="seat:director")
        assert [m["text"] for m in steerbox.pending(root)] == ["use the pinned ref"]
        claimed = steerbox.take(root)
        assert claimed[0]["item_id"] == 41
        assert steerbox.pending(root) == [], "a claimed message was left behind"

    def test_an_empty_message_is_refused(self, root):
        from bgate_core import steerbox

        with pytest.raises(ValueError):
            steerbox.post(root, 41, "   ")

    def test_the_pump_hands_it_to_the_live_agent(self, root, monkeypatch):
        from bgate_core import steerbox
        from bgate_ui import steerpump

        sent: list[tuple] = []
        monkeypatch.setattr("bgate_ui.dispatch.steer",
                            lambda r, i, t: sent.append((int(i), t)) or {"ok": True})
        steerbox.post(root, 41, "slow down", by="seat:director")
        got = steerpump.drain(root)
        assert sent == [(41, "slow down")]
        assert len(got["delivered"]) == 1 and not got["failed"]

    def test_an_undeliverable_message_is_reported_not_swallowed(self, root, monkeypatch):
        from bgate_core import activity, steerbox
        from bgate_ui import steerpump

        monkeypatch.setattr("bgate_ui.dispatch.steer",
                            lambda r, i, t: {"ok": False, "error": "no live agent for this item"})
        steerbox.post(root, 41, "too late", by="seat:director")
        got = steerpump.drain(root)
        assert got["failed"][0]["error"] == "no live agent for this item"
        assert any("not delivered" in row["summary"]
                   for row in activity.recent(root, limit=10))

    def test_a_stale_message_is_never_delivered_to_the_next_run(self, root, monkeypatch):
        """A correction aimed at a run that has ended must not steer its
        replacement — the complaint would be about work that no longer exists."""
        import json as _json
        import time

        from bgate_core import steerbox
        from bgate_ui import steerpump

        posted = steerbox.post(root, 41, "that pose is off-model")
        path = next(steerbox.box(root).glob("*.json"))
        data = _json.loads(path.read_text(encoding="utf-8"))
        data["at"] = time.time() - steerbox.STALE_S - 5
        path.write_text(_json.dumps(data), encoding="utf-8")

        sent: list = []
        monkeypatch.setattr("bgate_ui.dispatch.steer",
                            lambda r, i, t: sent.append(t) or {"ok": True})
        got = steerpump.drain(root)
        assert sent == []
        assert got["failed"][0]["id"] == posted["id"]

    @pytest.mark.anyio
    async def test_the_mcp_tool_refuses_an_item_that_is_not_running(
            self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ROOT", str(root))
        item = _queue.add(root, "art", "queued, nobody on it")
        got = await _mcp("agent_steer", item_id=item["id"], text="hurry up")
        assert got["ok"] is False
        assert "not running" in got["error"]

    @pytest.mark.anyio
    async def test_the_mcp_tool_posts_for_a_running_item(self, root, monkeypatch):
        from bgate_core import steerbox

        monkeypatch.setenv("BGATE_ROOT", str(root))
        item = _queue.add(root, "art", "live work")
        _queue.set_status(root, item["id"], "dispatched")
        got = await _mcp("agent_steer", item_id=item["id"],
                         text="use the pinned ref")
        assert got["ok"] is True
        assert [m["text"] for m in steerbox.pending(root)] == ["use the pinned ref"]

    def test_the_director_is_told_the_channel_exists(self, client, root, spawned):
        turn = client.post("/api/console/say",
                           json={"text": "the art agent is going off-model"}).json()
        brief = _queue.get(root, turn["turn_id"])["brief"]
        assert "agent_steer" in brief


# ---------------------------------------------------------------------------
# A refused turn is still a turn
# ---------------------------------------------------------------------------
class TestARefusedTurnSurvives:
    def test_the_item_stays_on_the_board_with_the_reason(self, client, root, monkeypatch):
        monkeypatch.setattr(
            "bgate_ui.dispatch.dispatch",
            lambda *a, **k: {"ok": False, "code": "dirty_tree",
                             "error": "2 uncommitted change(s) in the tree"})
        got = client.post("/api/console/say", json={"text": "do a thing"}).json()
        assert got["dispatched"] is False
        assert got["refusal"]["code"] == "dirty_tree"
        assert _queue.get(root, got["turn_id"])["status"] == "queued"
        # and it is still in the conversation, not swallowed
        assert client.get("/api/console/state").json()["turns"][-1]["id"] == got["turn_id"]
