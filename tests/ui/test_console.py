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

from bgate_core.store import db, workspace as _ws
from bgate_core.board import queue as _queue
from bgate_ui.agents import autodeploy
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
    """Record dispatches instead of starting a claude process.

    Two spawn paths now leave the console: an @seat turn dispatches a worker
    (dispatch.dispatch), a director turn submits to the persistent session
    (directorsession.submit). Both are recorded, neither starts a process —
    a fixture that neutered only the first would have every director-turn
    test quietly launching a real claude in a background thread.

    ``calls`` keeps its shape (a list of item ids) for the tests that predate
    the session; ``calls.prompts`` carries what the session would have heard.
    """
    class Calls(list):
        prompts: dict[int, str]

    calls = Calls()
    prompts = calls.prompts = {}

    def fake(root, item_id, **kw):
        calls.append(int(item_id))
        _queue.set_status(root, int(item_id), "dispatched")
        return {"ok": True, "item_id": int(item_id), "pid": 4242}

    def fake_submit(root, item_id, prompt, reseed_context=""):
        calls.append(int(item_id))
        prompts[int(item_id)] = str(prompt)
        return {"ok": True, "item_id": int(item_id)}

    monkeypatch.setattr("bgate_ui.agents.dispatch.dispatch", fake)
    monkeypatch.setattr("bgate_ui.agents.dispatch.find_claude", lambda: "claude")
    monkeypatch.setattr("bgate_ui.agents.directorsession.submit", fake_submit)
    return calls


def test_attention_dismissals_persist_in_the_console_state(client):
    key = "item:72:failed:failed:2026-09-03 10:00:00"
    r = client.post("/api/console/attention/dismiss", json={"key": key})
    assert r.status_code == 200, r.text
    assert key in client.get("/api/console/state?steps=false").json()["dismissed_attention"]

    r = client.post("/api/console/attention/dismiss",
                    json={"key": key, "dismissed": False})
    assert r.status_code == 200, r.text
    assert key not in client.get("/api/console/state?steps=false").json()["dismissed_attention"]


def test_attention_dismiss_refuses_unscoped_keys(client):
    assert client.post("/api/console/attention/dismiss",
                       json={"key": "anything"}).status_code == 400


# ---------------------------------------------------------------------------
# The fence — the human's own words survive the 80-character title
# ---------------------------------------------------------------------------

class TestTheStateShowsWhatJustClosed:
    """LAST CLOSED SHOWED ONLY FAILURES, on a board where plenty had landed.

    The window orders by status — dispatched, review, queued, FAILED, then
    everything finished — and cuts at 80. With 89 failed items the whole window
    was failures, so the console's "last closed" list could not show a single
    done item and the studio looked like it never finished anything.
    """

    def test_recent_done_work_reaches_the_payload_past_a_wall_of_failures(
            self, client, root):
        from bgate_core.board import queue as _queue

        # More failures than the window holds, so the old ordering fills it.
        for i in range(_console.BOARD + 5):
            item = _queue.add(root, "art", f"failed thing {i}")
            _queue.set_status(root, item["id"], "failed")
        landed = _queue.add(root, "tech", "the thing that actually landed")
        _queue.set_status(root, landed["id"], "done")

        items = client.get("/api/console/state").json()["items"]
        ids = {i["id"] for i in items}
        assert landed["id"] in ids, (
            "a done item newer than every failure is missing — the console "
            "cannot show what just closed")
        done = [i for i in items if i["status"] == "done"]
        assert done and done[0]["title"] == "the thing that actually landed"


class TestChainStateTellsTheWholeTruth:
    """The card's readiness must match what dispatch would actually do.

    Three lies the wire used to carry: an unmet EXTRA parent (work_item_dep)
    rendered `ready` with a deploy button whose one outcome was a refusal; a
    held row (escalation, chat) looked like any queued item; and a successor
    behind a FAILED predecessor read as patiently "waiting" on a link that
    was never going to close on its own.
    """

    def _items(self, client) -> dict:
        rows = client.get("/api/console/state").json()["items"]
        return {int(r["id"]): r for r in rows}

    def test_an_extra_parent_blocks_the_card(self, client, root):
        parent = _queue.add(root, "art", "the tileset")
        child = _queue.add(root, "gameplay", "the level")
        _queue.add_dependency(root, child["id"], parent["id"])
        got = self._items(client)[int(child["id"])]
        assert got["ready"] is False
        assert got["waiting_on"]["id"] == parent["id"]

    def test_a_dead_predecessor_is_stuck_not_waiting(self, client, root):
        parent = _queue.add(root, "art", "doomed")
        child = _queue.add(root, "gameplay", "downstream",
                           depends_on=int(parent["id"]))
        _queue.set_status(root, parent["id"], "failed")
        got = self._items(client)[int(child["id"])]
        assert got["ready"] is False
        assert got["stuck"] is True

    def test_a_held_row_says_so(self, client, root):
        held = _queue.add(root, "director", "two agents disagreed",
                          source="qa-gate-escalation", source_ref="1")
        plain = _queue.add(root, "art", "ordinary work")
        items = self._items(client)
        assert items[int(held["id"])]["held"] is True
        assert items[int(plain["id"])].get("held") is False


# ---------------------------------------------------------------------------
# One request paints the whole cockpit
# ---------------------------------------------------------------------------
class TestOnePayload:
    def test_every_key_the_view_paints_is_present(self, client):
        body = client.get("/api/console/state").json()
        for key in ("items", "agents", "lineage", "gates", "steps",
                    "autopilot", "floor"):
            assert key in body, f"the console cannot paint without {key}"

    def test_the_floor_tally_counts_the_board(self, client, root):
        _queue.add(root, "art", "one")
        _queue.add(root, "tech", "two")
        floor = client.get("/api/console/state").json()["floor"]
        assert floor["queued"] == 2
        assert floor["running"] == 0

    def test_every_item_names_where_it_came_from(self, client, root):
        # The graph groups by source, so a row without one is a row it cannot
        # place.
        _queue.add(root, "art", "draw the thing")
        body = client.get("/api/console/state").json()
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
        from bgate_core.store import artifacts

        item = _queue.add(root, "art", "draw the hero")
        image = root / "hero.png"
        image.write_bytes(b"hero")
        artifacts.register(root, "hero", image, producer="image_generate",
                           work_item_id=item["id"])
        gates = client.get("/api/console/state").json()["gates"]
        assert any(g["kind"] == "art" and g["item_id"] == item["id"] for g in gates)

    def test_a_candidate_names_the_seat_that_produced_it(self, client, root):
        """The seat was hardcoded 'art' while item_id named the real row.

        cinematic.py, music.py and storyboard.py all call artifacts.register, so
        a cinematic shot raised a gate addressed to art. Anything that reads a
        gate BY SEAT then points at the wrong room: the studio floor walked the
        art character to the Director's door carrying a cinematic item's title,
        while the seat that was actually blocked showed nothing.
        """
        from bgate_core.store import artifacts

        item = _queue.add(root, "cinematic", "the establishing shot")
        frame = root / "shot.png"
        frame.write_bytes(b"shot")
        artifacts.register(root, "shot_01", frame, producer="cinematic_generate_shot",
                           work_item_id=item["id"])
        gate = [g for g in client.get("/api/console/state").json()["gates"]
                if g["kind"] == "art" and g["item_id"] == item["id"]][0]
        assert gate["seat"] == "cinematic"

    def test_a_candidate_from_finished_work_is_not_a_gate(self, client, root):
        """The regression this filter exists for.

        Every candidate stays 'candidate' until somebody dispositions it, so a
        project with a used art library grew a permanent wall of approval nodes
        hanging off runs that ended weeks ago — a review backlog drawn as a
        blocked floor. The review queue in Assets owns that; the graph shows
        what the work in flight is waiting on.
        """
        from bgate_core.store import artifacts

        item = _queue.add(root, "art", "drew the hero last week")
        _queue.set_status(root, item["id"], "done", result="shipped")
        image = root / "old.png"
        image.write_bytes(b"old")
        artifacts.register(root, "old_hero", image, producer="image_generate",
                           work_item_id=item["id"])
        gates = client.get("/api/console/state").json()["gates"]
        assert not [g for g in gates if g["kind"] == "art"]

    def test_an_unattached_candidate_is_not_a_gate(self, client, root):
        from bgate_core.store import artifacts

        image = root / "loose.png"
        image.write_bytes(b"loose")
        artifacts.register(root, "loose", image, producer="image_generate")
        assert not [g for g in client.get("/api/console/state").json()["gates"]
                    if g["kind"] == "art"]

    def test_delegated_children_are_reachable_through_lineage(self, client, root):
        from bgate_ui.routes.orchestrator import DELEGATED_FROM

        parent = _queue.add(root, "director", "split this")
        child = _queue.add(root, "art", "the piece",
                           brief=f"{DELEGATED_FROM}{parent['id']}\n\ndo it")
        parents = client.get("/api/console/state").json()["lineage"]["parents"]
        assert parents[str(child["id"])] == parent["id"]


# ---------------------------------------------------------------------------
# Auto-deploy
# ---------------------------------------------------------------------------
class TestAutoDeploy:
    def test_it_is_on_by_default(self, client):
        # Flipped 2026-08-19: shipped off, a filed chain looked exactly like a
        # running one and sat still until somebody found the toggle.
        assert client.get("/api/console/autopilot").json()["on"] is True

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

        monkeypatch.setattr("bgate_ui.agents.dispatch.dispatch", refuse)
        monkeypatch.setattr("bgate_ui.agents.dispatch.find_claude", lambda: "claude")
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

        monkeypatch.setattr("bgate_ui.agents.dispatch.dispatch", full)
        monkeypatch.setattr("bgate_ui.agents.dispatch.find_claude", lambda: "claude")
        autodeploy.reset()
        autodeploy.set_enabled(root, True)
        autodeploy.tick(root)
        assert len(calls) == 1, "it kept asking after the floor said it was full"

    def test_a_missing_cli_refuses_once_not_once_per_item(self, root, monkeypatch):
        for i in range(3):
            _queue.add(root, "art", f"item {i}")
        monkeypatch.setattr("bgate_ui.agents.dispatch.find_claude", lambda: None)
        called: list[int] = []
        monkeypatch.setattr("bgate_ui.agents.dispatch.dispatch",
                            lambda *a, **k: called.append(1))
        autodeploy.reset()
        autodeploy.set_enabled(root, True)
        got = autodeploy.tick(root)
        assert called == []
        assert got["refused"][0]["code"] == "no_cli"

    def test_the_refusal_is_readable_from_the_state_the_view_polls(self, client, root, monkeypatch):
        _queue.add(root, "art", "will refuse")
        monkeypatch.setattr("bgate_ui.agents.dispatch.find_claude", lambda: "claude")
        monkeypatch.setattr(
            "bgate_ui.agents.dispatch.dispatch",
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
        from bgate_core.board import steerbox

        steerbox.post(root, 41, "use the pinned ref", by="seat:director")
        assert [m["text"] for m in steerbox.pending(root)] == ["use the pinned ref"]
        claimed = steerbox.take(root)
        assert claimed[0]["item_id"] == 41
        assert steerbox.pending(root) == [], "a claimed message was left behind"

    def test_an_empty_message_is_refused(self, root):
        from bgate_core.board import steerbox

        with pytest.raises(ValueError):
            steerbox.post(root, 41, "   ")

    def test_the_pump_hands_it_to_the_live_agent(self, root, monkeypatch):
        from bgate_core.board import steerbox
        from bgate_ui.pumps import steerpump

        sent: list[tuple] = []
        monkeypatch.setattr("bgate_ui.agents.dispatch.steer",
                            lambda r, i, t: sent.append((int(i), t)) or {"ok": True})
        steerbox.post(root, 41, "slow down", by="seat:director")
        got = steerpump.drain(root)
        assert sent == [(41, "slow down")]
        assert len(got["delivered"]) == 1 and not got["failed"]

    def test_an_undeliverable_message_is_reported_not_swallowed(self, root, monkeypatch):
        from bgate_core.board import activity, steerbox
        from bgate_ui.pumps import steerpump

        monkeypatch.setattr("bgate_ui.agents.dispatch.steer",
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

        from bgate_core.board import steerbox
        from bgate_ui.pumps import steerpump

        posted = steerbox.post(root, 41, "that pose is off-model")
        path = next(steerbox.box(root).glob("*.json"))
        data = _json.loads(path.read_text(encoding="utf-8"))
        data["at"] = time.time() - steerbox.STALE_S - 5
        path.write_text(_json.dumps(data), encoding="utf-8")

        sent: list = []
        monkeypatch.setattr("bgate_ui.agents.dispatch.steer",
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
        from bgate_core.board import steerbox

        monkeypatch.setenv("BGATE_ROOT", str(root))
        item = _queue.add(root, "art", "live work")
        _queue.set_status(root, item["id"], "dispatched")
        got = await _mcp("agent_steer", item_id=item["id"],
                         text="use the pinned ref")
        assert got["ok"] is True
        assert [m["text"] for m in steerbox.pending(root)] == ["use the pinned ref"]

    def test_the_director_is_told_the_channel_exists(self):
        # The guidance lives in the session's standing prompt now — it is a
        # fact about the director, not about any one turn.
        from bgate_ui.agents import directorsession

        assert "agent_steer" in directorsession.DIRECTOR_SYSTEM
        assert "queue_reopen" in directorsession.DIRECTOR_SYSTEM


# ---------------------------------------------------------------------------
# The director chat — a session's transcript, not a board row
# ---------------------------------------------------------------------------
class TestTheDirectorChat:
    """A MESSAGE IS A MESSAGE. It used to be a work item with a fenced brief,
    a reserved row and a lineage stamp, which is why the board filled up with
    rows nobody filed and the transcript had costs on it."""

    def test_a_message_files_nothing_on_the_board(self, client, root, monkeypatch):
        said = []
        monkeypatch.setattr("bgate_ui.agents.directorsession.send",
                            lambda r, text: said.append(text) or {"ok": True, "n": 1})
        assert client.post("/api/director/say", json={"text": "hello"}).json()["ok"]
        assert said == ["hello"]
        rows = db.connect(root).execute("SELECT count(*) FROM work_item").fetchone()
        assert rows[0] == 0

    def test_an_empty_message_is_refused(self, client):
        assert client.post("/api/director/say", json={"text": "  "}).status_code == 400

    def test_the_transcript_reads_back_what_was_said(self, client, root):
        from bgate_ui.agents import directorsession

        directorsession._post(root, "user", "make the hub hum")
        directorsession._post(root, "assistant", "filed it for art")
        body = client.get("/api/director/chat").json()
        assert [m["text"] for m in body["messages"]] == [
            "make the hub hum", "filed it for art"]
        assert body["running"] is False

    def test_after_returns_only_what_is_new(self, client, root):
        from bgate_ui.agents import directorsession

        first = directorsession._post(root, "user", "one")["n"]
        directorsession._post(root, "assistant", "two")
        body = client.get(f"/api/director/chat?after={first}").json()
        assert [m["text"] for m in body["messages"]] == ["two"]
