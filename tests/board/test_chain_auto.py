"""A chain is for order, not for lists.

MEASURED (exit-67-r2, 2026-09-22): ten independent cutout rigs were filed as
a ten-deep ladder and one art agent climbed it alone while the board had
two slots and the human asked why only one agent was running. A link now
waits on the one before it only when it is a seat handoff, its brief names
the predecessor, or it says so; same-seat links that do not mention each
other are siblings.
"""
from __future__ import annotations


from bgate_core.board import queue


def _ready_ids(root):
    return {r["id"] for r in queue.ready(root)}


class TestAutoLinking:
    def test_same_seat_links_that_ignore_each_other_run_beside_each_other(self, root):
        rigs = queue.add_chain(root, [
            {"seat": "gameplay", "title": f"rig {n} from its anchor", "brief": "cutout kit"}
            for n in range(4)])
        assert [r["depends_on"] for r in rigs] == [None, None, None, None]
        assert {r["id"] for r in rigs} <= _ready_ids(root)

    def test_a_seat_handoff_still_waits(self, root):
        tech, gp = queue.add_chain(root, [
            {"seat": "tech", "title": "bake the scene"},
            {"seat": "gameplay", "title": "wire the view"}])
        assert gp["depends_on"] == tech["id"]

    def test_a_brief_that_names_the_predecessor_waits(self, root):
        a, b = queue.add_chain(root, [
            {"seat": "gameplay", "title": "the arena", "brief": "build it"},
            {"seat": "gameplay", "title": "the boss", "brief": "spawn it after the arena lands"}])
        assert b["depends_on"] == a["id"]

    def test_after_is_explicit_either_way(self, root):
        a, b, c = queue.add_chain(root, [
            {"seat": "gameplay", "title": "one"},
            {"seat": "gameplay", "title": "two", "after": True},
            {"seat": "tech", "title": "three", "after": False}])
        assert b["depends_on"] == a["id"]
        # `after: false` runs BESIDE two, so it hangs off what two hangs off.
        assert c["depends_on"] == a["id"]

    def test_siblings_hang_off_the_handoff_above_them(self, root):
        tech, r1, r2 = queue.add_chain(root, [
            {"seat": "tech", "title": "the anchors"},
            {"seat": "art", "title": "rig one from its anchor"},
            {"seat": "art", "title": "rig two from its anchor"}])
        assert r1["depends_on"] == tech["id"] and r2["depends_on"] == tech["id"]

    def test_linear_mode_is_the_old_ladder(self, root):
        a, b, c = queue.add_chain(root, [
            {"seat": "gameplay", "title": "one"},
            {"seat": "gameplay", "title": "two"},
            {"seat": "gameplay", "title": "three"}], mode="linear")
        assert (b["depends_on"], c["depends_on"]) == (a["id"], b["id"])

    def test_the_protocol_says_so(self):
        from bgate_core.board import seats
        assert "A CHAIN IS FOR ORDER, NOT FOR LISTS" in seats.DIRECTOR_PROTOCOL


def test_link_builds_on_reads_the_words():
    prev = {"id": 41, "seat": "gameplay"}
    assert queue.link_builds_on({"seat": "gameplay", "title": "x", "brief": "once #41 lands"}, prev)
    assert queue.link_builds_on({"seat": "gameplay", "title": "x", "brief": "from the previous room"}, prev)
    assert not queue.link_builds_on({"seat": "gameplay", "title": "rig two", "brief": "a fresh kit"}, prev)
    assert queue.link_builds_on({"seat": "art", "title": "rig two", "brief": "a fresh kit"}, prev)
