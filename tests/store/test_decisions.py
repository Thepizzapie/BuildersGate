"""The decision register and the no-list.

TWO RULES CARRY THE WHOLE FEATURE and everything here is written from them.

A DECISION WITHOUT AN ACCEPTANCE TEST IS AN OPINION. The director's mission has
always said a settled decision names its test and what it leaves dark; the
product could not record either, so the panel that draws them was empty on every
project. Making the fields optional would have shipped a register that is
technically full and answers nothing — worse than an empty one, because it looks
answered. So blank is a refusal at the moment of writing, and these tests pin
that down field by field.

AN UNSAID NO GETS BUILT. The no-list is only worth having if agents can READ it,
and only trustworthy if agents cannot WRITE it: a refusal has no acceptance test
anybody can check it against, so an agent-written one is an unreviewable
instruction to every future session. The route tests below assert both halves of
that split, because getting either backwards produces a feature that looks
present and does the opposite of its job.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import db, project
from bgate_core.design import decisions
from bgate_ui.app import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    project.init(tmp_path, "Decisions")
    yield TestClient(app)
    db.close_all()


@pytest.fixture()
def as_agent(monkeypatch):
    """A dispatched seat worker, identified the way dispatch.py stamps one.

    BGATE_ACTOR is the explicit stamp; api.current_actor also infers an agent
    from BGATE_WORK_ITEM/BGATE_SEAT so that forgetting the stamp cannot silently
    disable the gate. Setting the explicit one here keeps the test about the
    decision rules rather than about identity inference, which test_auth_guard
    and test_accountability already own.
    """
    monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")


def data(response) -> dict:
    body = response.json()
    assert body["ok"] is True, body
    return body["data"]


def _settled(root, title="Inventory is grid-based", **kw):
    return decisions.add(
        root, title,
        kw.pop("acceptance", "a 6x4 grid holds 24 stacks and refuses the 25th"),
        kw.pop("leaves_dark", "nothing is said about weight or encumbrance"),
        **kw)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

class TestTheMandatoryFields:
    """The two fields that are easy to skip are the two the feature is for."""

    def test_a_decision_records_its_test_and_its_left_dark(self, root):
        row = _settled(root)
        assert row["acceptance"].startswith("a 6x4 grid")
        assert "encumbrance" in row["leaves_dark"]
        assert row["state"] == "settled"

    def test_a_decision_with_no_acceptance_test_is_refused(self, root):
        """Not defaulted to '' — a register full of untestable rulings looks
        answered and answers nothing, which is worse than an empty one."""
        with pytest.raises(ValueError, match="acceptance test"):
            decisions.add(root, "Inventory is grid-based", "   ",
                          "nothing about weight")

    def test_a_decision_that_names_nothing_dark_is_refused(self, root):
        """A deferral nobody labelled gets 'fixed' as a bug by the next agent."""
        with pytest.raises(ValueError, match="leaves dark"):
            decisions.add(root, "Inventory is grid-based", "24 stacks", "")

    def test_a_proposal_must_name_them_too(self, root):
        """An open proposal missing them cannot be ruled on: the human would
        have to invent both, which is the work the proposal was meant to do."""
        with pytest.raises(ValueError):
            decisions.add(root, "Maybe grid inventory", "", "", state="open")

    def test_a_refusal_must_say_why(self, root):
        """An unexplained no is re-proposed every few weeks by somebody who
        cannot see what was wrong with it."""
        with pytest.raises(ValueError, match="reason"):
            decisions.refuse(root, "multiplayer", "")

    def test_an_unknown_state_is_refused(self, root):
        with pytest.raises(ValueError, match="state must be one of"):
            decisions.add(root, "t", "a", "d", state="maybe")


class TestTheRegister:
    def test_newest_first_because_the_question_is_what_changed_lately(self, root):
        _settled(root, "first")
        _settled(root, "second")
        assert [d["title"] for d in decisions.list_decisions(root)] == \
            ["second", "first"]

    def test_state_filters_the_list(self, root):
        _settled(root, "ruled")
        _settled(root, "asked", state="open")
        assert [d["title"] for d in decisions.list_decisions(root, state="open")] \
            == ["asked"]

    def test_settling_a_proposal_rewrites_who_is_accountable(self, root,
                                                             monkeypatch):
        """The person who settles it owns it, not whichever agent drafted it."""
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-3")
        proposed = _settled(root, "asked", state="open")
        assert proposed["actor"] == "agent:item-3"
        monkeypatch.delenv("BGATE_ACTOR")
        settled = decisions.settle(root, proposed["id"], actor="a-human")
        assert settled["state"] == "settled" and settled["actor"] == "a-human"

    def test_settling_something_already_settled_is_a_no_op(self, root):
        row = _settled(root)
        assert decisions.settle(root, row["id"])["state"] == "settled"

    def test_superseding_keeps_both_rows(self, root):
        """A register you can quietly erase is not a register: without the loser
        row, the next person to propose it cannot learn it was already tried."""
        old = _settled(root, "hand-placed levels")
        new = _settled(root, "generated levels")
        decisions.supersede(root, old["id"], new["id"])
        after = decisions.get(root, old["id"])
        assert after["state"] == "superseded"
        assert after["superseded_by"] == new["id"]
        assert decisions.get(root, new["id"])["state"] == "settled"

    def test_a_superseded_decision_cannot_be_revived(self, root):
        old, new = _settled(root, "a"), _settled(root, "b")
        decisions.supersede(root, old["id"], new["id"])
        with pytest.raises(ValueError, match="superseded"):
            decisions.settle(root, old["id"])

    def test_a_decision_cannot_supersede_itself(self, root):
        row = _settled(root)
        with pytest.raises(ValueError, match="itself"):
            decisions.supersede(root, row["id"], row["id"])

    def test_a_missing_decision_is_a_lookup_error(self, root):
        with pytest.raises(LookupError):
            decisions.get(root, 999)


class TestTheNoList:
    def test_a_refusal_carries_the_thing_and_the_reason(self, root):
        row = decisions.refuse(root, "online co-op",
                               "one person cannot test netcode",
                               tag="scope")
        assert row["text"] == "online co-op" and row["tag"] == "scope"
        assert "netcode" in row["reason"]

    def test_the_tag_is_optional_and_filters(self, root):
        decisions.refuse(root, "co-op", "no netcode budget", tag="scope")
        decisions.refuse(root, "consoles", "no devkit")
        assert len(decisions.list_not_building(root)) == 2
        assert [r["text"] for r in
                decisions.list_not_building(root, tag="scope")] == ["co-op"]

    def test_lifting_a_refusal_deletes_it(self, root):
        """Unlike a superseded decision. A stale no is actively harmful: every
        agent reads it as binding, so it silently prevents work that is wanted
        now. The activity ledger keeps the record that it existed."""
        row = decisions.refuse(root, "co-op", "no netcode budget")
        decisions.unrefuse(root, row["id"])
        assert decisions.list_not_building(root) == []

    def test_the_overview_separates_the_three_states(self, root):
        _settled(root, "ruled")
        _settled(root, "asked", state="open")
        decisions.refuse(root, "co-op", "no netcode budget")
        view = decisions.overview(root)
        assert [d["title"] for d in view["decisions"]] == ["ruled"]
        assert [d["title"] for d in view["open"]] == ["asked"]
        assert len(view["not_building"]) == 1


class TestLinks:
    def test_a_blank_link_is_stored_as_nothing_not_as_zero(self, root):
        """An empty number field posts "" or 0, and a foreign key onto work item
        0 is a row that will never resolve."""
        row = _settled(root, work_item_id=0, session_id=None)
        assert row["work_item_id"] is None and row["session_id"] is None

    def test_a_decision_survives_the_work_item_it_was_about(self, root):
        """ON DELETE SET NULL, matching plan_row: the ruling outlives the
        attempt. Deleting an abandoned item must not take it along."""
        from bgate_core.board import queue
        item = queue.add(root, "gameplay", "build the grid", "brief")
        row = _settled(root, work_item_id=item["id"])
        assert row["work_item_id"] == item["id"]
        with db.tx(root) as conn:
            conn.execute("DELETE FROM work_item WHERE id = ?", (item["id"],))
        assert decisions.get(root, row["id"])["work_item_id"] is None

    def test_long_prose_is_truncated_rather_than_refused(self, root):
        """A rail 340px wide cannot render a pasted design document, but
        refusing the write would mean a decision that goes unrecorded — which is
        the failure this module exists to end."""
        row = _settled(root, "x" * 500, acceptance="y" * 5000)
        assert len(row["title"]) == decisions.MAX_TITLE
        assert len(row["acceptance"]) == decisions.MAX_TEXT


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class TestReadsAreOpenToEverybody:
    """The entire point of the no-list is that agents consult it."""

    def test_an_agent_may_read_the_no_list(self, client, tmp_path, as_agent):
        decisions.refuse(tmp_path, "online co-op", "one person cannot test it")
        rows = data(client.get("/api/not-building"))
        assert [r["text"] for r in rows] == ["online co-op"]

    def test_an_agent_may_read_the_register(self, client, tmp_path, as_agent):
        _settled(tmp_path)
        assert len(data(client.get("/api/decisions"))) == 1

    def test_the_overview_is_one_read_for_the_whole_panel(self, client, tmp_path):
        _settled(tmp_path, "ruled")
        _settled(tmp_path, "asked", state="open")
        decisions.refuse(tmp_path, "co-op", "no netcode budget")
        view = data(client.get("/api/decisions/overview"))
        assert len(view["decisions"]) == 1
        assert len(view["open"]) == 1
        assert len(view["not_building"]) == 1

    def test_overview_is_not_swallowed_as_a_decision_id(self, client):
        """`/api/decisions/{id}` is declared AFTER it on purpose — FastAPI
        matches in declaration order, so the wildcard would answer 422 for a
        route that exists."""
        assert client.get("/api/decisions/overview").status_code == 200

    def test_a_missing_decision_is_a_404_in_the_envelope(self, client):
        body = client.get("/api/decisions/999").json()
        assert body["ok"] is False and body["error"]["code"] == "not_found"


class TestProposeButDoNotSettle:
    def test_an_agent_may_file_a_proposal(self, client, as_agent):
        row = data(client.post("/api/decisions", json={
            "title": "cap enemies at 40", "acceptance": "the profiler stays 60fps",
            "leaves_dark": "says nothing about projectile count",
            "state": "open"}))
        assert row["state"] == "open" and row["actor"] == "agent:item-7"

    def test_an_agent_may_not_settle_one(self, client, as_agent):
        resp = client.post("/api/decisions", json={
            "title": "cap enemies at 40", "acceptance": "60fps",
            "leaves_dark": "projectiles", "state": "settled"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    def test_an_agent_may_not_settle_a_proposal_afterwards(self, client,
                                                           tmp_path, as_agent):
        row = _settled(tmp_path, state="open")
        assert client.post(f"/api/decisions/{row['id']}/settle").status_code == 403
        assert decisions.get(tmp_path, row["id"])["state"] == "open"

    def test_a_human_settles_it(self, client, tmp_path):
        row = _settled(tmp_path, state="open")
        assert data(client.post(
            f"/api/decisions/{row['id']}/settle"))["state"] == "settled"

    def test_the_missing_field_error_says_what_the_field_is_for(self, client):
        body = client.post("/api/decisions", json={
            "title": "cap enemies at 40", "acceptance": "", "leaves_dark": "x"}
        ).json()
        assert body["ok"] is False
        assert "acceptance test" in body["error"]["message"]

    def test_superseded_cannot_be_filed_directly(self, client):
        """The row would claim it was replaced by nothing, which the panel
        cannot draw honestly."""
        resp = client.post("/api/decisions", json={
            "title": "t", "acceptance": "a", "leaves_dark": "d",
            "state": "superseded"})
        assert resp.status_code == 400

    def test_superseding_without_naming_the_replacement_is_refused(self, client,
                                                                   tmp_path):
        row = _settled(tmp_path)
        resp = client.post(f"/api/decisions/{row['id']}/supersede", json={})
        assert resp.status_code == 400
        assert "by_id" in resp.json()["error"]["message"]


class TestTheNoListIsAHumansList:
    def test_an_agent_cannot_write_a_refusal(self, client, as_agent):
        resp = client.post("/api/not-building", json={
            "text": "online co-op", "reason": "no netcode budget"})
        assert resp.status_code == 403

    def test_the_refusal_names_the_door_that_is_open_to_the_agent(self, client,
                                                                  as_agent):
        """A 403 that does not say what to do next produces an agent that
        retries the same call until it gives up."""
        body = client.post("/api/not-building", json={
            "text": "co-op", "reason": "x"}).json()
        assert "/api/decisions" in body["error"]["message"]
        assert "open" in body["error"]["message"]

    def test_an_agent_cannot_lift_one_either(self, client, tmp_path, as_agent):
        row = decisions.refuse(tmp_path, "co-op", "no netcode budget")
        assert client.delete(f"/api/not-building/{row['id']}").status_code == 403
        assert len(decisions.list_not_building(tmp_path)) == 1

    def test_a_human_writes_and_lifts_one(self, client):
        row = data(client.post("/api/not-building", json={
            "text": "online co-op", "reason": "one person cannot test netcode",
            "tag": "scope"}))
        assert row["tag"] == "scope"
        assert data(client.delete(f"/api/not-building/{row['id']}"))["deleted"]

    def test_a_refusal_with_no_reason_is_a_400(self, client):
        resp = client.post("/api/not-building", json={"text": "co-op"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# The MCP surface
# ---------------------------------------------------------------------------
# THE SAME PROPOSE/SETTLE SPLIT LIVES IN TWO PLACES, because the product has two
# front doors and a gate that holds on only one of them is not a gate. The HTTP
# side reasons from api.current_actor; the MCP side from _caller_is_agent, which
# also reads BGATE_SEAT because dispatch stamps a spawned seat with that and not
# with BGATE_ACTOR. These tests exist so the two cannot drift apart silently.

@pytest.mark.anyio
class TestTheToolsAgentsActuallyCall:

    async def _call(self, tool, /, **kwargs):
        import json

        from bgate_mcp import server
        result = await server.mcp.call_tool(tool, kwargs)
        content = result[0] if isinstance(result, tuple) else result
        block = content[0]
        return json.loads(block.text) if hasattr(block, "text") else block

    async def test_an_agent_can_read_the_no_list(self, root, monkeypatch):
        """The one that matters most: an agent that cannot read the no-list
        builds the no. This tool is the reason the no is not unsaid."""
        monkeypatch.setenv("BGATE_ROOT", str(root))
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        decisions.refuse(root, "online co-op", "one person cannot test netcode")
        out = await self._call("not_building_list")
        assert [r["text"] for r in out["not_building"]] == ["online co-op"]

    async def test_an_agent_proposes_rather_than_settles(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ROOT", str(root))
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        refused = await self._call(
            "decision_add", title="cap enemies at 40",
            acceptance="the profiler stays at 60fps",
            leaves_dark="says nothing about projectile count", state="settled")
        assert refused["ok"] is False and "state='open'" in refused["error"]
        proposed = await self._call(
            "decision_add", title="cap enemies at 40",
            acceptance="the profiler stays at 60fps",
            leaves_dark="says nothing about projectile count", state="open")
        assert proposed["state"] == "open"

    async def test_an_agent_may_not_write_the_no_list(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ROOT", str(root))
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        out = await self._call("not_building_add", text="co-op",
                               reason="no netcode budget")
        assert out["ok"] is False and "decision_add" in out["error"]

    async def test_a_human_session_settles_and_refuses(self, root, monkeypatch):
        """No BGATE_SEAT and no BGATE_WORK_ITEM is what a top-level session —
        the human's own — looks like to the server."""
        monkeypatch.setenv("BGATE_ROOT", str(root))
        for var in ("BGATE_SEAT", "BGATE_WORK_ITEM", "BGATE_ACTOR"):
            monkeypatch.delenv(var, raising=False)
        filed = await self._call("decision_add", title="grid inventory",
                                 acceptance="24 stacks, the 25th is refused",
                                 leaves_dark="nothing about encumbrance")
        assert filed["state"] == "settled"
        assert (await self._call("not_building_add", text="online co-op",
                                 reason="one person cannot test netcode"))["text"]
        listed = await self._call("decision_list")
        assert listed["count"] == 1 and listed["open"] == 0


class TestTheMigrationIsSafeOnAnExistingProject:
    def test_both_tables_exist_after_a_plain_init(self, root):
        """Forward-only via PRAGMA user_version — a project created before this
        migration picks the tables up on its next connect, with nothing to
        back-fill because both tables start empty."""
        conn = db.connect(root)
        found = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('decision', 'not_building')")}
        assert found == {"decision", "not_building"}
