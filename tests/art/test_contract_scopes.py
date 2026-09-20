"""The sprite contract scopes a cell and a view per character and per action,
a patch merges instead of replacing, the scale contract grades per stage,
and a generator refuses to spend on a name that already has an approved file."""
from __future__ import annotations

import pytest

from bgate_core.art import spritecontract as sc
from bgate_core.three_d import scalecontract as scale


def test_a_character_and_an_action_can_scope_cell_view_and_standing_height(root):
    sc.apply_preset(root, "four_dir", {
        "cell": [32, 32], "standing_px": 27,
        "characters": {
            "chuco_battle": {"cell": [128, 128], "view": "side", "standing_px": 76,
                             "drawn": ["e"],
                             "actions": {"cast": {"cell": [160, 128]}}},
            "chuco_ow": {},
        }})
    ow = sc.contract_for(root, "chuco_ow", "walk")
    assert ow["cell"] == [32, 32] and ow["view"] == "top_down_3q"
    assert ow["standing_px"] == 27 and ow["scope"] == "project"
    battle = sc.contract_for(root, "chuco_battle", "attack")
    assert battle["cell"] == [128, 128] and battle["view"] == "side"
    assert battle["standing_px"] == 76 and battle["scope"] == "character"
    assert battle["drawn"] == ["e"] and battle["feet_row"] == 125
    cast = sc.contract_for(root, "chuco_battle", "cast")
    assert cast["cell"] == [160, 128] and cast["scope"] == "action"
    assert cast["view"] == "side"                # inherited from the character


def test_a_scoped_override_is_validated_like_the_top_level(root):
    with pytest.raises(sc.ContractError, match="view"):
        sc.normalise({"characters": {"x": {"view": "sideways"}}})
    with pytest.raises(sc.ContractError, match="cell"):
        sc.normalise({"characters": {"x": {"cell": [4, 4]}}})
    with pytest.raises(sc.ContractError, match="taller"):
        sc.normalise({"cell": [32, 32], "standing_px": 40})


def test_merge_patch_keeps_the_characters_it_does_not_name():
    current = sc.normalise({"characters": {
        "a": {"cell": [64, 64], "actions": {"walk": {"frames": 6}}},
        "b": {"cell": [96, 96]}}})
    merged = sc.merge_patch(current, {"characters": {
        "a": {"view": "side", "actions": {"walk": {"fps": 8}}},
        "b": None}})
    out = sc.normalise(merged)
    assert out["characters"]["a"]["cell"] == [64, 64]          # kept
    assert out["characters"]["a"]["view"] == "side"            # added
    assert out["characters"]["a"]["actions"]["walk"] == {"frames": 6, "fps": 8}
    assert "b" not in out["characters"]                        # None removes


def test_the_scale_unit_is_the_standing_height_not_the_cell(root):
    sc.apply_preset(root, "four_dir", {"cell": [32, 32], "standing_px": 27})
    assert scale.contract(root)["player_height_px"] == 27


def test_stages_grade_against_their_own_unit(root, tmp_path):
    from PIL import Image
    scale.set_contract(root, player_height_px=27, stages={"battle": 76, "overworld": 27})
    got = scale.contract(root)
    assert got["stages"] == {"battle": 76, "overworld": 27}
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    for y in range(20, 172):
        for x in range(80, 120):
            img.putpixel((x, y), (200, 40, 40, 255))          # 152px tall
    p = root / "enemy.png"
    img.save(p)
    field = scale.check(root, "enemy.png", "enemy")            # 152/27 = 5.6x: a wall
    battle = scale.check(root, "enemy.png", "enemy", stage="battle")   # 152/76 = 2.0x
    assert field["ok"] is False and battle["ok"] is True
    assert battle["player_height_px"] == 76 and battle["stage"] == "battle"
    with pytest.raises(ValueError, match="no stage"):
        scale.check(root, "enemy.png", "enemy", stage="sky")
    scale.set_contract(root, stages={"overworld": None})
    assert "overworld" not in scale.contract(root)["stages"]
    assert "boss" in scale.contract(root)["classes"]


def test_a_generator_refuses_a_name_that_already_has_an_approved_file(root, monkeypatch):
    from bgate_core.store import artifacts
    import bgate_mcp.server as server
    monkeypatch.setenv("BGATE_ROOT", str(root))
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets" / "oracle.png").write_bytes(b"png")
    rev = artifacts.register(root, "oracle", "assets/oracle.png", producer="image_generate")
    assert server._regen_gate("oracle") is None                 # not approved yet: free to go
    artifacts.review(root, rev["id"], "approved", actor="reviewer")
    refused = server._regen_gate("oracle")
    assert refused and refused["code"] == "already_approved"
    assert refused["path"] == "assets/oracle.png"
    assert server._regen_gate("oracle", "the eyes are wrong") is None
    assert server._regen_gate("nothing_here") is None
    artifacts.review(root, rev["id"], "rejected", "wrong", actor="reviewer")
    assert server._regen_gate("oracle") is None                 # rejected: the gate opens
