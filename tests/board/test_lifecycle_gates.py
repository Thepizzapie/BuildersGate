"""Lifecycle graph, human checkpoints, split-instead-of-rerun, scope grading."""
from __future__ import annotations

import pytest

from bgate_core.board import lifecycle, queue
from bgate_core.store import db


def _status(root, item_id, status, result="ok"):
    with db.tx(root) as tx:
        tx.execute("UPDATE work_item SET status = ?, result = ? WHERE id = ?",
                   (status, result, int(item_id)))


class TestScopeGrade:
    def test_one_deliverable_passes(self):
        assert queue.scope_grade("build the dash", "dash test green",
                                 "medium", "Dash ability")["score"] == 0

    def test_many_checks_large_and_joined_title_are_broad(self):
        got = queue.scope_grade("build it", "dash works; parry works; hud shows",
                                "large", "dash and parry and hud")
        assert got["score"] >= 2
        assert any("checks" in r for r in got["reasons"])

    @pytest.mark.anyio
    async def test_mcp_refuses_the_director_too(self, root, monkeypatch):
        import json
        from bgate_mcp import server
        monkeypatch.setenv("BGATE_ROOT", str(root))

        async def call(**kw):
            result = await server.mcp.call_tool("queue_add", kw)
            content = result[0] if isinstance(result, tuple) else result
            return json.loads(content[0].text)

        wide = dict(seat="gameplay", title="dash and parry and hud",
                    brief="build all of it", size="large",
                    acceptance="dash; parry; hud")
        got = await call(**wide)
        assert got.get("refused") == "too_broad"
        ok = await call(**wide, allow_broad="one prototype scene, the human "
                                            "asked for it whole")
        assert ok.get("id")


class TestCheckpoint:
    def test_a_checkpoint_parks_for_the_human_in_any_gate_mode(self, root):
        a = queue.add(root, "gameplay", "dash")
        cp = queue.add(root, "level", "teach room", depends_on=a["id"],
                       checkpoint=True, checkpoint_note="play the teach room")
        after = queue.add(root, "art", "dress room", depends_on=cp["id"])
        _status(root, a["id"], "done")
        got = queue.complete(root, cp["id"], result="layout done")
        assert got["status"] == "review"
        assert after["id"] not in {r["id"] for r in queue.ready(root)}
        covers = queue.checkpoint_digest(root, cp["id"])
        assert [c["id"] for c in covers] == [a["id"]]
        queue.approve(root, cp["id"], by="human")
        assert after["id"] in {r["id"] for r in queue.ready(root)}

    def test_the_digest_stops_at_the_previous_checkpoint(self, root):
        a = queue.add(root, "gameplay", "a")
        b = queue.add(root, "gameplay", "b", depends_on=a["id"], checkpoint=True)
        c = queue.add(root, "gameplay", "c", depends_on=b["id"])
        d = queue.add(root, "gameplay", "d", depends_on=c["id"], checkpoint=True)
        assert [x["id"] for x in queue.checkpoint_digest(root, d["id"])] == [b["id"], c["id"]]


class TestSplit:
    def test_a_machine_reopen_after_two_runs_becomes_a_split(self, root, monkeypatch):
        from bgate_core.board import activity
        monkeypatch.setattr(activity, "is_machine", lambda *a, **k: True)
        item = queue.add(root, "gameplay", "whole hud", brief="build the hud")
        _status(root, item["id"], "failed")
        queue.reopen(root, item["id"], "health bar missing")       # run 2
        _status(root, item["id"], "failed")
        got = queue.reopen(root, item["id"], "ammo counter missing")
        assert got["status"] == "parked"
        split = queue.get(root, got["split_requested"])
        assert split["seat"] == "director" and split["split_of"] == item["id"]
        assert "ammo counter missing" in split["brief"]
        again = queue.request_split(root, item["id"], "still")
        assert again["split_requested"] == split["id"]          # idempotent

    def test_a_human_reopen_still_goes_through(self, root, monkeypatch):
        from bgate_core.board import activity
        monkeypatch.setattr(activity, "is_machine", lambda *a, **k: False)
        item = queue.add(root, "gameplay", "hud", brief="build the hud")
        _status(root, item["id"], "failed")
        queue.reopen(root, item["id"], "x")
        _status(root, item["id"], "failed")
        assert queue.reopen(root, item["id"], "y")["status"] == "queued"


class TestLifecycle:
    def test_chains_hold_a_lane_and_independent_work_reuses_lanes(self, root):
        chain = queue.add_chain(root, [
            {"seat": "gameplay", "title": "dash", "brief": "build the dash"},
            {"seat": "level", "title": "room", "brief": "after the dash, lay out the room",
             "after": True}], mode="linear")
        solo = queue.add(root, "audio", "whoosh")
        g = lifecycle.graph(root)
        by = {n["id"]: n for n in g["nodes"]}
        assert by[chain[0]["id"]]["lane"] == by[chain[1]["id"]]["lane"]
        assert {"from": chain[0]["id"], "to": chain[1]["id"]} in g["edges"]
        assert by[chain[1]["id"]]["row"] > by[chain[0]["id"]]["row"]
        assert by[solo["id"]]["state"] == "ready"
        assert g["lanes"] <= 2

    def test_a_finished_checkpoint_is_its_own_state(self, root):
        cp = queue.add(root, "level", "room", checkpoint=True)
        queue.complete(root, cp["id"], result="done")
        g = lifecycle.graph(root)
        assert g["checkpoints_waiting"] == [cp["id"]]


def test_console_state_carries_every_dependency(root):
    """The graph draws dependencies; a fan-in drawn with one parent hides it."""
    from bgate_ui.routes import console
    a = queue.add(root, "art", "sheet")
    b = queue.add(root, "audio", "sfx")
    c = queue.add(root, "gameplay", "wire", depends_on=a["id"])
    queue.add_dependency(root, c["id"], b["id"])
    items = [dict(queue.get(root, i)) for i in (a["id"], b["id"], c["id"])]
    got = console._deps(db.connect(root), items)
    assert sorted(got[str(c["id"])]) == sorted([a["id"], b["id"]])


def test_lifecycle_marks_what_spawned_a_gate(root):
    item = queue.add(root, "art", "sheet")
    gate = queue.add(root, "qa", "QA gate", source="qa-gate", source_ref=str(item["id"]))
    by = {n["id"]: n for n in lifecycle.graph(root)["nodes"]}
    assert by[gate["id"]]["spawned_by"] == item["id"]


def test_lanes_are_named_by_paths_not_by_words():
    """'the game falls back' named eight lanes and refused a one-file fix."""
    assert queue.brief_breadth(
        "the game silently loads the fallback; fix src/llm_adapters.ts")["score"] == 0
    wide = queue.brief_breadth("touch game/scripts/a.gd, game/assets/x.png, "
                               "game/levels/z.tscn and design/levels/l.md")
    assert any("lanes" in r for r in wide["reasons"])


def test_a_split_hands_dependents_to_the_split_item_instead_of_cutting_them(root):
    """vibe-coder 2026-10-12: a corpus item parked for a split released its
    QA item, which then dispatched against half-written work."""
    from bgate_core.board import queue

    work = queue.add(root, "narrative", "write the corpus", brief="x")
    check = queue.add(root, "qa", "check it", brief="x", depends_on=work["id"])
    got = queue.request_split(root, work["id"], "too broad")
    split_id = got["split_requested"]
    parents = {int(r["depends_on"]) for r in db.connect(root).execute(
        "SELECT depends_on FROM work_item_dep WHERE item_id = ? AND "
        "(cut_at IS NULL OR cut_at = '')", (check["id"],))}
    assert split_id in parents and work["id"] not in parents
    assert queue.blocker(root, check["id"]) is not None


def test_a_slice_items_follow_up_is_admitted_at_the_slice_stage():
    from bgate_core.board.queue import _admitted_in_slice
    follow_up = {"id": 59, "kind": "", "source": "seat:art", "source_ref": "46",
                 "depends_on": 46}
    stray = {"id": 60, "kind": "", "source": "seat:art", "source_ref": "",
             "depends_on": None}
    assert _admitted_in_slice(follow_up, {46})
    assert not _admitted_in_slice(stray, {46})


def test_a_follow_ups_follow_up_inherits_slice_admission(tmp_path):
    """A split link two steps from a feel-check item, or a chain member of a
    split, is admitted through its lineage rather than a manual kind=fix."""
    from bgate_core.board import queue
    root = tmp_path
    feel = queue.add(root, "gameplay", "FEEL FAIL: main", "x", source="feel-check")
    child = queue.add(root, "gameplay", "Sam hunts", "x", source="seat:director",
                      source_ref=str(feel["id"]))
    link = queue.add(root, "gameplay", "#a keep physics on", "x", source="seat:director")
    db.connect(root).execute("UPDATE work_item SET chain_id = ? WHERE id = ?",
                             (f"split-{child['id']}", link["id"]))
    stray = queue.add(root, "art", "a poster nobody asked for", "x", source="seat:art")
    link_row, stray_row = queue.get(root, link["id"]), queue.get(root, stray["id"])
    assert queue._admitted_in_slice(child, set(), root)
    assert queue._admitted_in_slice(link_row, set(), root)
    assert not queue._admitted_in_slice(stray_row, set(), root)
    # no root: the old one-step rule, nothing crashes
    assert not queue._admitted_in_slice(link_row, set())
