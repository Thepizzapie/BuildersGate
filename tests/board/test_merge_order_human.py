"""Merge order, human steps in the dependency graph, dispatch following the order."""
from __future__ import annotations

import pytest

from bgate_core.board import lifecycle, mergeorder, queue
from bgate_core.store import db


def _done(root, item_id):
    with db.tx(root) as tx:
        tx.execute("UPDATE work_item SET status='done', result='ok' WHERE id=?", (int(item_id),))


class TestMergeOrder:
    def test_waves_critical_path_then_unblocks(self, root):
        a = queue.add(root, "gameplay", "core loop")                 # long chain head
        b = queue.add(root, "level", "room", depends_on=a["id"])
        c = queue.add(root, "art", "dress room", depends_on=b["id"])
        solo = queue.add(root, "audio", "whoosh")                   # independent
        fan = queue.add(root, "tech", "save schema")
        for t in ("save player", "save world"):
            queue.add(root, "tech", t, depends_on=fan["id"])
        got = mergeorder.order(root)
        rank = {e["id"]: e for e in got["order"]}
        assert rank[a["id"]]["wave"] == 0 and rank[c["id"]]["wave"] == 2
        assert rank[a["id"]]["critical"] and rank[c["id"]]["critical"]
        assert rank[a["id"]]["rank"] < rank[fan["id"]]["rank"] < rank[solo["id"]]["rank"]
        assert rank[fan["id"]]["unblocks"] == 2
        assert "lands after" in rank[b["id"]]["why"]

    def test_a_finished_parent_is_satisfied(self, root):
        a = queue.add(root, "gameplay", "core loop")
        b = queue.add(root, "level", "room", depends_on=a["id"])
        _done(root, a["id"])
        got = {e["id"]: e for e in mergeorder.order(root)["order"]}
        assert a["id"] not in got and got[b["id"]]["wave"] == 0

    def test_ready_dispatches_the_longest_chain_first(self, root):
        solo = queue.add(root, "audio", "whoosh")
        head = queue.add(root, "gameplay", "core loop")
        queue.add(root, "level", "room", depends_on=head["id"])
        ready = [r["id"] for r in queue.ready(root)]
        assert ready.index(head["id"]) < ready.index(solo["id"])

    def test_priority_still_outranks_the_order(self, root):
        head = queue.add(root, "gameplay", "core loop")
        queue.add(root, "level", "room", depends_on=head["id"])
        urgent = queue.add(root, "tech", "crash on boot", priority=9)
        assert queue.ready(root)[0]["id"] == urgent["id"]


class TestHumanSteps:
    def test_work_waits_on_a_human_step_until_it_is_done(self, root):
        build = queue.add(root, "gameplay", "jump feel")
        polish = queue.add(root, "art", "jump vfx")
        step = queue.add_human_task(root, "Play the jump and pick the arc",
                                    blocks=[polish["id"]], after=[build["id"]])
        ready = {r["id"] for r in queue.ready(root)}
        assert step["id"] not in ready                      # never dispatched
        assert polish["id"] not in ready
        order = {e["id"]: e for e in mergeorder.order(root)["order"]}
        assert order[step["id"]]["human"] and order[step["id"]]["state"] == "needs you"
        _done(root, build["id"])
        queue.human_done(root, step["id"], note="the high arc", by="human")
        assert polish["id"] in {r["id"] for r in queue.ready(root)}

    def test_human_done_refuses_agent_work(self, root):
        item = queue.add(root, "gameplay", "jump feel")
        with pytest.raises(ValueError, match="not a human step"):
            queue.human_done(root, item["id"])

    def test_lifecycle_marks_human_steps_and_checkpoints(self, root):
        item = queue.add(root, "art", "hero sheet")
        step = queue.add_human_task(root, "Approve the palette", blocks=[item["id"]])
        g = lifecycle.graph(root)
        by = {n["id"]: n for n in g["nodes"]}
        assert by[step["id"]]["state"] == "human"
        assert step["id"] in g["human_waiting"]

    def test_set_checkpoint_on_open_work_only(self, root):
        item = queue.add(root, "level", "teach room")
        assert queue.set_checkpoint(root, item["id"], True, "play it")["checkpoint"] == 1
        _done(root, item["id"])
        with pytest.raises(ValueError, match="finished work"):
            queue.set_checkpoint(root, item["id"], False)


def test_merge_order_reaches_the_seat_brief(root):
    from bgate_core.board import seats
    head = queue.add(root, "gameplay", "core loop")
    queue.add(root, "level", "room", depends_on=head["id"])
    block = seats.brief(root, "gameplay")["merge_order"]
    assert block["yours"][0]["id"] == head["id"]
    assert block["first_on_the_board"][0]["id"] == head["id"]
