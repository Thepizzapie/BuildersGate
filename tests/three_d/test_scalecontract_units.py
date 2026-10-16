"""scale_contract_set's unit mistakes come back as one sentence, never a raw
TypeError: classes are player-height ratio bands {low, high}; stages are
px heights.
"""
from __future__ import annotations

import pytest

from bgate_core.three_d import scalecontract


class TestTheUnitsSentence:
    @pytest.mark.parametrize("kwargs", [
        {"classes": {"player": {"px": 40}}},                  # the gnomefall shape
        {"classes": {"player": {"low": None, "high": 1.2}}},  # float(None): TypeError
        {"classes": {"player": {"low": "40px", "high": 1.2}}},
        {"classes": {"player": 40}},
        {"stages": {"backyard": {"px": 40}}},                 # int(dict): TypeError
        {"stages": {"backyard": "forty"}},
        {"player_height_px": {"px": 40}},
    ])
    def test_every_unit_mistake_is_a_value_error_naming_the_units(self, root, kwargs):
        kwargs = {"player_height_px": 40, **kwargs}
        with pytest.raises(ValueError) as caught:
            scalecontract.set_contract(root, **kwargs)
        assert scalecontract.UNITS in str(caught.value)
        assert "ratio bands {low, high}" in str(caught.value)
        assert "px heights" in str(caught.value)

    def test_a_pixel_dict_no_longer_slips_through_with_defaults(self, root):
        with pytest.raises(ValueError, match="unknown band field.*px"):
            scalecontract.set_contract(root, player_height_px=40,
                                       classes={"door": {"px": 60}})
        assert scalecontract.contract(root)["classes"]["door"]["low"] == 1.05

    def test_the_right_units_still_land(self, root):
        got = scalecontract.set_contract(
            root, player_height_px=40,
            classes={"door": {"low": 1.1, "high": 1.4}}, stages={"yard": 40})
        assert got["player_height_px"] == 40
        assert got["classes"]["door"]["low"] == 1.1
        assert got["stages"] == {"yard": 40}


class TestTheTool:
    def test_the_tool_answers_with_an_error_payload(self, root, monkeypatch):
        from bgate_mcp import server

        monkeypatch.setenv("BGATE_ROOT", str(root))
        got = server.scale_contract_set.__wrapped__(
            player_height_px=40, classes={"player": {"px": 40}})
        assert got["ok"] is False
        assert got["units"] == scalecontract.UNITS
        assert "px" in got["error"]

    def test_the_tool_still_returns_the_contract_on_success(self, root, monkeypatch):
        from bgate_mcp import server

        monkeypatch.setenv("BGATE_ROOT", str(root))
        got = server.scale_contract_set.__wrapped__(player_height_px=40)
        assert got["player_height_px"] == 40
