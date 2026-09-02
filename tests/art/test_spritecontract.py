"""The sprite contract: the declared shape of a game's sheets.

What is being pinned: presets are internally valid, a contract that would
generate contradictions is REFUSED (never repaired), and the override ladder
resolves the way downsizing's hand-built tables proved necessary — project
default, per-character drawn rows, per-(character, action) exceptions.
"""
from __future__ import annotations

import pytest

from bgate_core.art import spritecontract as sc


class TestPresets:
    def test_every_preset_normalises(self):
        for name in sc.PRESETS:
            got = sc.normalise({**sc.PRESETS[name], "preset": name})
            assert got["preset"] == name

    def test_every_direction_is_drawn_or_mirrored(self):
        for name in sc.PRESETS:
            got = sc.normalise({**sc.PRESETS[name], "preset": name})
            for direction in got["directions"]:
                assert (direction in got["drawn"]
                        or direction in got["mirror"]), (name, direction)

    def test_four_corner_is_downsizings_shape(self):
        got = sc.normalise({**sc.PRESETS["four_corner"], "preset": "four_corner"})
        assert got["cell"] == [96, 80]
        assert got["drawn"] == ["nw", "sw"]
        assert got["mirror"] == {"ne": "nw", "se": "sw"}
        assert got["layout"] == "grid_rows"


class TestRefusals:
    def test_a_typo_direction_is_refused_not_invented(self):
        with pytest.raises(sc.ContractError, match="not a direction"):
            sc.normalise({"directions": ["e", "eest"], "drawn": ["e"],
                          "rows": ["e"]})

    def test_a_mirror_of_an_undrawn_direction_is_refused(self):
        with pytest.raises(sc.ContractError, match="not drawn"):
            sc.normalise({"directions": ["e", "w"], "drawn": ["e"],
                          "rows": ["e"], "mirror": {"e": "w"}})

    def test_a_mirror_that_is_not_a_flip_is_refused(self):
        """n flipped is still n — pairing it with e is a contradiction."""
        with pytest.raises(sc.ContractError, match="not a horizontal flip"):
            sc.normalise({"directions": ["e", "n"], "drawn": ["e"],
                          "rows": ["e"], "mirror": {"n": "e"}})

    def test_an_unreachable_direction_is_refused(self):
        with pytest.raises(sc.ContractError, match="neither drawn nor mirrored"):
            sc.normalise({"directions": ["e", "n"], "drawn": ["e"],
                          "rows": ["e"], "mirror": {}})

    def test_a_row_of_mirrored_pixels_is_refused(self):
        """A sheet row holds generated pixels; mirrors are made at runtime."""
        with pytest.raises(sc.ContractError, match="not a drawn"):
            sc.normalise({"directions": ["e", "w"], "drawn": ["e"],
                          "rows": ["w"], "mirror": {"w": "e"}})


class TestResolution:
    def _stored(self, root):
        sc.apply_preset(root, "four_corner", {
            "actions": {"walk": {"frames": 4, "fps": 8.0}},
            "characters": {
                "pm_paladin": {"drawn": ["ne", "sw"],
                               "actions": {"attack": {"drawn": ["ne", "se"]}}},
            }})

    def test_project_default_reaches_an_unlisted_character(self, root):
        self._stored(root)
        got = sc.contract_for(root, "hr_bard", "walk")
        assert got["drawn"] == ["nw", "sw"]
        assert got["action"] == {"name": "walk", "frames": 4, "fps": 8.0}

    def test_character_override_beats_the_project(self, root):
        self._stored(root)
        got = sc.contract_for(root, "pm_paladin", "walk")
        assert got["drawn"] == ["ne", "sw"]
        assert got["rows"] == ["ne", "sw"]      # the override IS the row plan
        assert got["mirror"] == {"nw": "ne", "se": "sw"}

    def test_action_exception_beats_the_character(self, root):
        self._stored(root)
        got = sc.contract_for(root, "pm_paladin", "attack")
        assert got["drawn"] == ["ne", "se"]
        assert got["mirror"] == {"nw": "ne", "sw": "se"}


    def test_rows_follow_an_override_under_strip_layout_too(self, root):
        """A station-facing override ("working is drawn north") on a strip
        contract must move the ROWS with it — the start-frame slicer reads
        rows, and default rows pointed it at pixels of the wrong facing."""
        sc.apply_preset(root, "sidescroller", {
            "characters": {"audio": {"actions": {"working": {"drawn": ["n"]}}}}})
        got = sc.contract_for(root, "audio", "working")
        assert got["drawn"] == ["n"] and got["rows"] == ["n"]

    def test_a_facing_left_without_pixels_or_flip_is_reported(self, root):
        sc.apply_preset(root, "four_corner",
                        {"characters": {"solo": {"drawn": ["nw"]}}})
        got = sc.contract_for(root, "solo", "walk")
        # Only ne survives (a flipped nw); se needs sw drawn and sw needs se.
        assert set(got["unplayable"]) == {"se", "sw"}
        assert got["mirror"] == {"ne": "nw"}

    def test_an_unset_project_answers_with_the_default(self, root):
        got = sc.load(root)
        assert got["preset"] == "single" and got["drawn"] == ["e"]


class TestViewClause:
    def test_each_view_has_words_and_unknown_has_none(self):
        for view in sc.VIEWS:
            assert sc.view_clause(view)
        assert sc.view_clause("cinematic-drone-shot") == ""
