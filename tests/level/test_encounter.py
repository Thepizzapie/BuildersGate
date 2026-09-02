"""Enemies as interactions, tasks as commitment shapes."""
from __future__ import annotations

import pytest

from bgate_core.level import encounter


PAIR = [
    {"name": "Shambler", "role": "melee",
     "pressure": "closes the distance and forces you off your position",
     "alters": [{"enemy": "Watcher",
                 "effect": "pushes you out of cover, which is the only place "
                           "the Watcher cannot reach you"}]},
    {"name": "Watcher", "role": "ranged",
     "pressure": "punishes standing still in the open",
     "alters": [{"enemy": "Shambler",
                 "effect": "makes backing away from the Shambler cost health, "
                           "so you have to go through it"}]},
]


class TestRoster:
    def test_a_category_is_not_a_pressure(self):
        with pytest.raises(ValueError, match="no 'pressure'"):
            encounter.validate_roster([{"name": "Shambler", "role": "melee"}])

    def test_appearing_together_is_not_an_interaction(self):
        with pytest.raises(ValueError, match="has no effect"):
            encounter.validate_roster([
                {"name": "A", "pressure": "chases you across the room",
                 "alters": [{"enemy": "B", "effect": "both"}]},
                {"name": "B", "pressure": "shoots you from the doorway"},
            ])

    def test_an_enemy_cannot_alter_itself(self):
        with pytest.raises(ValueError, match="altering itself"):
            encounter.validate_roster([
                {"name": "A", "pressure": "chases you across the room",
                 "alters": [{"enemy": "A",
                             "effect": "it makes itself more dangerous"}]}])

    def test_altering_someone_not_in_the_roster(self):
        with pytest.raises(ValueError, match="not in the roster"):
            encounter.validate_roster([
                {"name": "A", "pressure": "chases you across the room",
                 "alters": [{"enemy": "Ghost",
                             "effect": "it makes the Ghost harder to read"}]}])

    def test_isolated_machines_are_a_finding_not_a_crash(self, root):
        got = encounter.set_roster(root, [
            {"name": "A", "pressure": "chases you across the room"},
            {"name": "B", "pressure": "shoots you from the doorway"},
        ])
        assert any("does not change" in f for f in got["findings"])
        assert any("no enemy in this roster alters any other" in f.lower()
                   for f in got["findings"])

    def test_one_enemy_is_not_asked_for_a_combination(self, root):
        got = encounter.set_roster(root, [
            {"name": "A", "pressure": "chases you across the room"}])
        assert got["findings"] == []

    def test_a_real_pair_passes(self, root):
        got = encounter.set_roster(root, PAIR)
        assert got["findings"] == []
        assert encounter.production_blockers(root) == []


class TestObjectives:
    def test_the_shape_vocabulary_is_closed(self):
        with pytest.raises(ValueError, match="is not one of"):
            encounter.validate_objectives([
                {"name": "Purge the terminal", "shape": "stand in the circle",
                 "costs": "you cannot move while it runs"}])

    def test_a_task_that_takes_nothing_away(self):
        with pytest.raises(ValueError, match="no 'costs'"):
            encounter.validate_objectives([
                {"name": "Purge", "shape": "dwell"}])

    def test_eight_dwells_is_one_task_eight_times(self, root):
        rows = [{"name": f"Quota {i}", "shape": "dwell",
                 "costs": "you have to stay put while the meter fills"}
                for i in range(8)]
        got = encounter.set_objectives(root, rows)
        assert any("commitment shape" in f for f in got["findings"])
        assert any("are 'dwell'" in f for f in got["findings"])

    def test_two_tasks_are_not_graded(self, root):
        got = encounter.set_objectives(root, [
            {"name": "A", "shape": "dwell",
             "costs": "you have to stay put while the meter fills"},
            {"name": "B", "shape": "dwell",
             "costs": "you have to stay put while the meter fills"},
        ])
        assert got["findings"] == []

    def test_a_spread_passes(self, root):
        got = encounter.set_objectives(root, [
            {"name": "Purge the terminal", "shape": "dwell",
             "costs": "you have to stay put while the meter fills"},
            {"name": "Haul the drum", "shape": "carry",
             "costs": "both hands, so no light and no weapon"},
            {"name": "Reroute the loop", "shape": "route",
             "costs": "you visit the breakers in an order you did not pick"},
            {"name": "Hold the door", "shape": "defend",
             "costs": "you fight where the door is, not where you want to be"},
        ])
        assert got["findings"] == []
        assert encounter.production_blockers(root) == []

    def test_a_majority_of_one_non_dwell_shape_is_still_uniform(self, root):
        got = encounter.set_objectives(root, [
            {"name": f"Haul {i}", "shape": "carry",
             "costs": "both hands, so no light and no weapon"}
            for i in range(4)
        ] + [
            {"name": "Purge", "shape": "dwell",
             "costs": "you have to stay put while the meter fills"},
            {"name": "Reroute", "shape": "route",
             "costs": "you visit the breakers in an order you did not pick"},
        ])
        assert any("wearing a different hat" in f for f in got["findings"])


def test_nothing_declared_blocks_nothing(root):
    # A puzzle game with no enemies and no quota tasks is not refused for
    # being a puzzle game.
    assert encounter.production_blockers(root) == []
