"""GRIPE 40 (EXIT 67 postmortem, 2026-09-21): hours were burned on one item
because every item got the same ceiling regardless of size. These test the
pure size-derived budget math in runlimits.py — the numbers dispatch.py's
spawn path and _watch_completion loop actually use.
"""
from __future__ import annotations

from bgate_core.board import runlimits


class TestSizeCeilings:
    def test_defaults_match_the_spec(self, root):
        assert runlimits.ceiling_for_size(root, "small") == (900, 60)
        assert runlimits.ceiling_for_size(root, "medium") == (3600, 300)
        # large falls through to "no size override" (0, 0)
        assert runlimits.ceiling_for_size(root, "large") == (0, 0)

    def test_unknown_size_falls_back_to_medium(self, root):
        assert runlimits.ceiling_for_size(root, "huge") == (3600, 300)

    def test_project_override_wins_over_the_default(self, root):
        runlimits.set_limits(root, small_runtime_s=120, small_turns=10)
        assert runlimits.ceiling_for_size(root, "small") == (120, 10)

    def test_size_of_defaults_and_normalizes(self):
        assert runlimits.size_of({}) == "medium"
        assert runlimits.size_of({"size": "SMALL"}) == "small"
        assert runlimits.size_of({"size": "bogus"}) == "medium"


class TestRuntimeCeilingForItem:
    def test_small_item_gets_the_small_ceiling(self, root):
        got = runlimits.runtime_ceiling_for_item(root, {"size": "small"})
        assert got == 900

    def test_items_own_override_wins_over_size(self, root):
        got = runlimits.runtime_ceiling_for_item(
            root, {"size": "small", "max_runtime_s": 55})
        assert got == 55

    def test_large_falls_back_to_project_default(self, root):
        runlimits.set_limits(root, max_runtime_s=7200)
        got = runlimits.runtime_ceiling_for_item(root, {"size": "large"})
        assert got == 7200

    def test_turn_cap_for_item(self, root):
        assert runlimits.turn_cap_for_item(root, {"size": "medium"}) == 300
        assert runlimits.turn_cap_for_item(root, {"size": "large"}) == 0


class TestBudgetSteer:
    def test_budget_fraction(self):
        assert runlimits.budget_fraction(450, 900) == 0.5
        assert runlimits.budget_fraction(10, 0) == 0.0  # uncapped

    def test_checkpoints_are_sixty_and_eightyfive_percent(self):
        assert runlimits.LAND_WHAT_YOU_HAVE_CHECKPOINTS == (0.60, 0.85)

    def test_land_what_you_have_text_names_remaining_time(self):
        text = runlimits.land_what_you_have_text(540, 900, 0.60)
        assert "BUDGET CHECK" in text
        assert "60%" in text
        assert "6 min left" in text

    def test_steer_fires_at_each_checkpoint_once(self):
        """The guard dispatch.py's watch loop uses: a checkpoint fires once
        elapsed crosses its fraction of the ceiling, and not again."""
        limit_s = 900
        fired = set()

        def tick(elapsed_s):
            posted = []
            for pct in runlimits.LAND_WHAT_YOU_HAVE_CHECKPOINTS:
                if pct in fired:
                    continue
                if elapsed_s < limit_s * pct:
                    continue
                fired.add(pct)
                posted.append(pct)
            return posted

        assert tick(100) == []
        assert tick(540) == [0.60]      # 60% of 900
        assert tick(600) == []          # already fired, no repeat
        assert tick(765) == [0.85]      # 85% of 900
        assert tick(890) == []
