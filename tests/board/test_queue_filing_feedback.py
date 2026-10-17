"""A filing says what the board will do with it.

MEASURED (Turf War, 2026-10-16, a second web project on one machine): a
nine-link chain filed at the slice stage came back from queue_add_chain as
nine ordinary queued rows and sat held with no refusal anywhere, because the
stage hold was computed only by queue_list; a QA re-gate filed by hand hit
the same silence an hour later. A chain had no plan_row, so the only way
onto the slice was pointing the rows at its items by hand. And queue_list
printed the depends_on column alone, so the chain the director had
serialised with queue_add_dependency read as a fan to anyone who looked.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.board import queue
from bgate_core.design import gameplan, greenlight
from bgate_core.store import db
from bgate_mcp import server

MANIFEST = [
    {"kind": "scene", "name": "hub_room", "seat": "gameplay", "slice": True,
     "acceptance": "boots headless, player spawns"},
    {"kind": "asset", "name": "hero_sheet", "seat": "art", "slice": False,
     "acceptance": "idle+walk, consistency_check green"},
]


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.setenv("BGATE_SEAT", "director")
    return root


@pytest.fixture()
def at_slice(wired):
    """Rows exist, nothing is filed against them, the stage is slice."""
    gameplan.ingest(wired, MANIFEST, file_slice=False)
    doc = greenlight._doc(wired)
    doc["stage"] = greenlight.SLICE
    greenlight._save(wired, doc)
    return wired


async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


def _row_item(root, name):
    return db.connect(root).execute(
        "SELECT work_item_id FROM plan_row WHERE name = ?", (name,)).fetchone()[0]


@pytest.mark.anyio
class TestFilingSaysWhatTheStageWillDo:
    async def test_queue_add_at_slice_reports_the_hold_on_the_item(self, at_slice):
        got = await call("queue_add", seat="art", title="concept a menu",
                         brief="paint the menu", acceptance="a png exists")
        assert got["status"] == "queued"
        assert "slice" in got["stage_hold"]
        assert "plan_row" in got["stage_hold"]      # it names the way in

    async def test_queue_add_on_a_slice_row_has_no_hold(self, at_slice):
        got = await call("queue_add", seat="gameplay", title="the hub room",
                         brief="build it", plan_row="hub_room")
        assert "stage_hold" not in got
        assert _row_item(at_slice, "hub_room") == got["id"]

    async def test_at_production_nothing_is_said(self, wired):
        got = await call("queue_add", seat="art", title="concept a menu",
                         brief="paint the menu", acceptance="a png exists")
        assert "stage_hold" not in got


@pytest.mark.anyio
class TestChainLinksOnPlanRows:
    async def test_a_link_takes_a_plan_row_and_its_follow_up_is_admitted(
            self, at_slice):
        got = await call("queue_add_chain", mode="linear", links=[
            {"seat": "gameplay", "title": "the hub room", "brief": "build it",
             "plan_row": "hub_room"},
            {"seat": "gameplay", "title": "the hub door",
             "brief": "once the hub lands, hang the door",
             "acceptance": "door opens"},
            {"seat": "gameplay", "title": "the hub lights",
             "brief": "after the door, light it", "acceptance": "lit"},
        ])
        first, second, third = got["items"]
        assert first["plan_row"] == "hub_room"
        assert _row_item(at_slice, "hub_room") == first["id"]
        # The row's acceptance and domain goal reached the link.
        assert queue.get(at_slice, first["id"])["acceptance"].startswith(
            "boots headless")
        # Link 1 is slice work; link 2 is its own follow-up (admitted); link
        # 3 hangs off link 2, which is not a slice item, so the stage holds
        # it - and the FILING says so, which is the whole point.
        assert "stage_hold" not in first and "stage_hold" not in second
        assert "slice" in third["stage_hold"]
        assert got["held"] == [third["id"]]
        assert "1 of 3" in got["note"]

    async def test_a_row_named_twice_refuses_before_anything_lands(
            self, at_slice):
        got = await call("queue_add_chain", links=[
            {"seat": "gameplay", "title": "one", "plan_row": "hub_room"},
            {"seat": "gameplay", "title": "two", "plan_row": "hub_room"},
        ])
        assert got["ok"] is False and got["refused"] == "plan_row"
        assert got["link"] == 1
        assert queue.list_items(at_slice) == []

    async def test_a_row_already_being_built_refuses_the_chain(self, at_slice):
        await call("queue_add", seat="gameplay", title="the hub room",
                   brief="build it", plan_row="hub_room")
        got = await call("queue_add_chain", links=[
            {"seat": "gameplay", "title": "again", "plan_row": "hub_room"},
            {"seat": "tech", "title": "wire it"},
        ])
        assert got["ok"] is False and got["refused"] == "plan_row"
        assert "already being built" in got["error"]
        assert len(queue.list_items(at_slice)) == 1

    async def test_a_chain_without_rows_is_unchanged_but_honest(self, at_slice):
        got = await call("queue_add_chain", links=[
            {"seat": "tech", "title": "bake", "acceptance": "baked"},
            {"seat": "gameplay", "title": "wire", "acceptance": "wired"},
        ])
        assert got["count"] == 2
        assert set(got["held"]) == {i["id"] for i in got["items"]}
        assert all("slice" in i["stage_hold"] for i in got["items"])


@pytest.mark.anyio
class TestQueueListCarriesEveryParent:
    async def test_depends_on_all_is_every_parent_not_the_column(self, wired):
        a = queue.add(wired, "tech", "a")
        b = queue.add(wired, "tech", "b")
        c = queue.add(wired, "gameplay", "c", depends_on=a["id"])
        queue.add_dependency(wired, c["id"], b["id"])
        rows = {r["id"]: r for r in (await call("queue_list"))["items"]}
        assert rows[c["id"]]["depends_on"] == a["id"]
        assert rows[c["id"]]["depends_on_all"] == [a["id"], b["id"]]
        assert rows[a["id"]]["depends_on_all"] == []


@pytest.mark.anyio
class TestARelabelCannotLiftAHold:
    """MEASURED (Turf War, 2026-10-17): an autonomous director changed two
    held feature items to kind=fix "so the slice-stage hold doesn't block
    them", the hold accepted the label, and both ran. The ``wired`` fixture
    stamps BGATE_SEAT, so the caller here is an agent until a test unsets it."""

    def _held_feature(self, root):
        item = queue.add(root, "gameplay", "triple the damage",
                         brief="so an athlete dies in three hits", kind="feature")
        assert "slice" in server._stage_hold_for(root, item)
        return item

    async def test_an_agent_cannot_relabel_a_held_feature_as_a_fix(self, at_slice):
        item = self._held_feature(at_slice)
        got = await call("queue_update", item_id=item["id"], kind="fix")
        assert got["ok"] is False and got["refused"] == "stage_hold"
        assert "greenlight_advance" in got["error"]
        assert queue.get(at_slice, item["id"])["kind"] == "feature"

    async def test_a_relabel_that_keeps_the_hold_is_an_ordinary_edit(self, at_slice):
        item = self._held_feature(at_slice)
        got = await call("queue_update", item_id=item["id"], kind="content")
        assert got.get("ok", True) is not False and got["kind"] == "content"

    async def test_the_human_may_and_the_change_is_on_the_record(
            self, at_slice, monkeypatch):
        from bgate_core.board import activity
        item = self._held_feature(at_slice)
        monkeypatch.delenv("BGATE_SEAT")
        monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
        got = await call("queue_update", item_id=item["id"], kind="fix")
        assert got["kind"] == "fix"
        summaries = [r["summary"] for r in activity.recent(at_slice, limit=5)]
        assert any("kind 'feature'->'fix'" in s for s in summaries), summaries
