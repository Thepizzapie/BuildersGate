"""GRIPE 41 (EXIT 67 postmortem, 2026-09-21): tickets filed too broad spread
one agent across a whole feature. queue.brief_breadth() is the pure heuristic
that grades a brief against the four measured shapes; queue.add() persists
`size`/`acceptance` alongside it.
"""
from __future__ import annotations

from bgate_core.board import queue


class TestBriefBreadth:
    def test_narrow_brief_scores_zero(self):
        got = queue.brief_breadth(
            "Fix the double-jump bug: player.gd line 40 checks is_on_floor() "
            "after the velocity update instead of before it.")
        assert got["score"] == 0
        assert got["reasons"] == []

    def test_chained_brief_is_flagged(self):
        got = queue.brief_breadth(
            "Rig the owner mesh and then animate the walk cycle once that's done.")
        assert got["score"] >= 1
        assert any("chain" in r for r in got["reasons"])

    def test_multi_bullet_brief_is_flagged(self):
        brief = (
            "Ship the shop screen:\n"
            "- add the buy button\n"
            "- wire the currency counter\n"
            "- add a sell flow\n"
            "- add a confirmation dialog\n"
        )
        got = queue.brief_breadth(brief)
        assert got["score"] >= 1
        assert any("bullet" in r for r in got["reasons"])

    def test_long_brief_is_flagged(self):
        got = queue.brief_breadth("x" * 901)
        assert got["score"] >= 1
        assert any("900" in r for r in got["reasons"])

    def test_multi_lane_brief_is_flagged(self):
        got = queue.brief_breadth(
            "Update design/pillars.md, then touch game/assets/hero.png and "
            "also game/scripts/player.gd and game/scenes/level1.tscn.")
        assert got["score"] >= 1
        assert any("lane" in r for r in got["reasons"])

    def test_realistic_broad_brief_scores_at_least_two(self):
        # Modeled on the postmortem's own complaint: a feature-sized ask
        # wearing one item.
        brief = (
            "Build the inventory system: add the data model, then wire up "
            "the UI in game/scenes/inventory.tscn and game/scripts/inv.gd, "
            "and also touch design/items.md and game/assets/icons/*.png for "
            "the icon set, and once that's done write tests for it. " + "y" * 700
        )
        got = queue.brief_breadth(brief)
        assert got["score"] >= 2


class TestAddPersistsSizeAndAcceptance:
    def test_default_size_is_medium(self, root):
        item = queue.add(root, "tech", "small fix")
        assert item["size"] == "medium"
        assert item["acceptance"] == ""

    def test_size_and_acceptance_round_trip(self, root):
        item = queue.add(root, "tech", "small fix", size="small",
                         acceptance="godot_test_run shows 0 failures")
        got = queue.get(root, item["id"])
        assert got["size"] == "small"
        assert got["acceptance"] == "godot_test_run shows 0 failures"

    def test_unknown_size_is_refused(self, root):
        import pytest
        with pytest.raises(ValueError, match="size"):
            queue.add(root, "tech", "x", size="huge")
