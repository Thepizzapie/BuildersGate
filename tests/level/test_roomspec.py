"""room_build / room_audit: a designed top-down room from a plan, and the
space faults that shipped on the benchmark (a walled-in NPC, a party spawned
under a weighbridge) refused before the scene exists and found in any scene
after."""
from __future__ import annotations

import json
import re

import pytest

from bgate_core.level import roomspec, tilemap

MANIFEST = {
    "kind": "bgate-tileset", "version": 1, "tile_px": 32, "bits": 8,
    "floor": {"source": 0, "table": {"255": [0, 0]}, "solid": [0, 0],
              "variants": [[1, 0], [2, 0]]},
    "wall": {"source": 0, "layout": "solid", "atlas": [0, 1], "variants": [[1, 1]]},
    "props": {
        "tacos_stand": {"source": 0, "atlas": [0, 2], "footprint": [2, 2], "blocking": True},
        "planter": {"source": 0, "atlas": [2, 2], "footprint": [1, 1], "blocking": True},
        "rug": {"source": 0, "atlas": [3, 2], "footprint": [2, 1], "blocking": False},
    },
}

PLAN = [
    "##########",
    "#........#",
    "#........#",
    "#........#",
    "#........#",
    "#####..###",
]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    tiles = tmp_path / "assets" / "tiles"
    tiles.mkdir(parents=True)
    (tiles / "town.tres").write_text(
        '[gd_resource type="TileSet" format=3]\n[resource]\ntile_size = Vector2i(32, 32)\n',
        encoding="utf-8")
    (tiles / "town.tiles.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (tmp_path / "scenes").mkdir()
    (tmp_path / "scenes" / "npc.tscn").write_text(
        '[gd_scene format=3]\n[node name="NPC" type="Node2D"]\n', encoding="utf-8")
    return tmp_path


def test_build_writes_layers_props_and_markers_from_cells(project):
    r = roomspec.build(
        project, project, "res://scenes/town.tscn", "res://assets/tiles/town.tres", PLAN,
        props=[{"type": "tacos_stand", "at": [2, 1]}, {"type": "planter", "at": [7, 3]}],
        markers=[{"name": "Spawn_Start", "at": [5, 4]},
                 {"name": "NPC_Vendor", "scene": "res://scenes/npc.tscn", "at": [4, 1],
                  "props": {"npc_id": '"vendor"'}},
                 {"name": "Exit_South", "at": [5, 5], "kind": "exit"}],
        seed=3)
    assert r["ok"] and r["written"], r
    text = (project / "scenes" / "town.tscn").read_text(encoding="utf-8")
    assert 'name="Floor" type="TileMapLayer"' in text
    assert 'name="Walls" type="TileMapLayer"' in text
    assert 'name="Props" type="TileMapLayer"' in text
    assert 'name="NPC_Vendor" parent="." instance=ExtResource' in text
    assert 'npc_id = "vendor"' in text
    assert "position = Vector2(176, 144)" in text     # cell (5,4) centre at 32px
    assert r["cells"] == {"Floor": 34, "Walls": 26, "Props": 2}
    # the audit ran on what was written, and the room is sound
    assert r["audit"]["ok"], r["audit"]["findings"]
    assert r["audit"]["counts"]["solid"] == 26 + 4 + 1
    assert "S" in r["audit"]["ascii"] and "N" in r["audit"]["ascii"]


def test_build_refuses_a_marker_in_a_wall_and_a_prop_off_the_floor(project):
    r = roomspec.build(
        project, project, "res://scenes/bad.tscn", "res://assets/tiles/town.tres", PLAN,
        props=[{"type": "tacos_stand", "at": [8, 4]},        # 2x2 hangs into the wall
               {"type": "planter", "at": [2, 2]},
               {"type": "rug", "at": [2, 2]}],               # overlaps the planter
        markers=[{"name": "Spawn_Start", "at": [0, 0]},      # in the wall
                 {"name": "NPC_Ghost", "at": [2, 2]},        # inside the planter
                 {"name": "Sign_Far", "at": [40, 40]}])      # off the map
    assert r["ok"] is False and r["written"] is False
    assert not (project / "scenes" / "bad.tscn").exists()
    joined = " | ".join(r["problems"])
    assert "not entirely on floor" in joined
    assert "overlaps 'planter'" in joined
    assert "'Spawn_Start' at cell [0, 0] is inside a wall" in joined
    assert "'NPC_Ghost' at cell [2, 2] is inside prop 'planter'" in joined
    assert "'Sign_Far' at cell [40, 40] is off the map" in joined


def test_build_refuses_unknown_legend_and_unknown_prop(project):
    with pytest.raises(roomspec.RoomError, match="legend"):
        roomspec.plan_room(MANIFEST, ["#..?#"])
    r = roomspec.plan_room(MANIFEST, PLAN, props=[{"type": "fountain", "at": [3, 3]}],
                           markers=[{"name": "Spawn", "at": [3, 3]}])
    assert not r["ok"] and "fountain" in r["problems"][0]


def _scene_with(project, cells_by_layer: dict, extra_nodes: str) -> str:
    res = "res://scenes/audit.tscn"
    parts = ['[gd_scene format=3]',
             '[ext_resource type="TileSet" path="res://assets/tiles/town.tres" id="1_t"]',
             '[node name="Room" type="Node2D"]']
    for name, cells in cells_by_layer.items():
        packed = tilemap.encode_cells(cells)
        parts.append(f'[node name="{name}" type="TileMapLayer" parent="."]\n'
                     f'tile_map_data = PackedByteArray("{packed}")\ntile_set = ExtResource("1_t")')
    parts.append(extra_nodes)
    (project / "scenes" / "audit.tscn").write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return res


def test_audit_finds_a_walled_in_npc_and_a_spawn_under_a_prop(project):
    floor = [{"x": x, "y": y, "source": 0, "ax": 0, "ay": 0} for y in range(1, 5) for x in range(1, 9)]
    ring = {(x, 0) for x in range(10)} | {(x, 5) for x in range(10)}         | {(0, y) for y in range(6)} | {(9, y) for y in range(6)}
    # a pen around (7,3): walls on three sides, a prop on the fourth
    pen = {(6, 2), (7, 2), (8, 2), (6, 3), (6, 4), (7, 4), (8, 4)}
    walls = [{"x": x, "y": y, "source": 0, "ax": 0, "ay": 1} for (x, y) in sorted(ring | pen)]
    props = [{"x": 8, "y": 3, "source": 0, "ax": 2, "ay": 2}]       # planter closes the pen
    extra = '''[node name="Spawn_Start" type="Marker2D" parent="."]
position = Vector2(80, 48)

[node name="NPC_Penned" type="Node2D" parent="."]
position = Vector2(240, 112)

[node name="Weighbridge" type="Node2D" parent="."]
position = Vector2(64, 32)
footprint = Vector2i(2, 1)

[node name="Sign_Fine" type="Node2D" parent="."]
position = Vector2(144, 144)
'''
    # NPC_Penned and Sign_Fine are plain Node2Ds here; the audit reads
    # instanced scenes and Marker2Ds, so mark them the way a baked room does
    extra = extra.replace('[node name="NPC_Penned" type="Node2D" parent="."]',
                          '[node name="NPC_Penned" parent="." instance=ExtResource("1_t")]')
    extra = extra.replace('[node name="Sign_Fine" type="Node2D" parent="."]',
                          '[node name="Sign_Fine" parent="." instance=ExtResource("1_t")]')
    res = _scene_with(project, {"Ground": floor, "Blocks": walls, "Dressing": props}, extra)
    r = roomspec.audit(project, project, res)
    codes = {(f["code"], f["node"]) for f in r["findings"]}
    assert ("marker_in_solid", "Spawn_Start") in codes          # under the weighbridge (2,1)
    assert ("unreachable", "NPC_Penned") in codes              # the pen at (7,3)
    assert ("unreachable", "Sign_Fine") not in codes
    assert r["ok"] is False
    assert r["counts"]["solid"] == len(ring | pen) + 1 + 2
    assert "P" in r["ascii"] and "N" in r["ascii"]


def test_audit_reads_the_manifest_beside_the_tileset_and_passes_a_sound_room(project):
    floor = [{"x": x, "y": y, "source": 0, "ax": 0, "ay": 0} for y in range(1, 5) for x in range(1, 9)]
    walls = [{"x": x, "y": 0, "source": 0, "ax": 1, "ay": 1} for x in range(10)]   # a wall VARIANT
    extra = '''[node name="Spawn_Start" type="Marker2D" parent="."]
position = Vector2(48, 48)

[node name="Chest_Key" parent="." instance=ExtResource("1_t")]
position = Vector2(240, 112)
'''
    res = _scene_with(project, {"Terrain": floor + walls}, extra)
    r = roomspec.audit(project, project, res)
    assert r["ok"], r["findings"]
    assert r["layers"][0]["manifest"] is True
    assert r["layers"][0]["wall"] == 10 and r["layers"][0]["floor"] == 32
    assert r["counts"]["reached"] == 32


def test_audit_without_a_spawn_says_so_and_still_checks_solids(project):
    floor = [{"x": x, "y": y, "source": 0, "ax": 0, "ay": 0} for y in range(3) for x in range(3)]
    extra = '''[node name="NPC_A" parent="." instance=ExtResource("1_t")]
position = Vector2(16, 16)
'''
    res = _scene_with(project, {"Terrain": floor}, extra)
    r = roomspec.audit(project, project, res)
    assert [f["code"] for f in r["findings"]] == ["no_spawn"]
    assert r["ok"] is True                    # a warn is not a fail


def test_audit_accepts_int_list_packed_arrays_the_hand_bakers_write(project):
    cells = [{"x": 0, "y": 0, "source": 0, "ax": 0, "ay": 0}]
    packed = tilemap.encode_cells(cells)
    import base64
    ints = ", ".join(str(b) for b in base64.b64decode(packed))
    text = ('[gd_scene format=3]\n'
            '[ext_resource type="TileSet" path="res://assets/tiles/town.tres" id="1_t"]\n'
            '[node name="Room" type="Node2D"]\n'
            '[node name="Terrain" type="TileMapLayer" parent="."]\n'
            f'tile_map_data = PackedByteArray({ints})\ntile_set = ExtResource("1_t")\n')
    (project / "scenes" / "ints.tscn").write_text(text, encoding="utf-8")
    r = roomspec.audit(project, project, "res://scenes/ints.tscn")
    assert r["counts"]["floor"] == 1
