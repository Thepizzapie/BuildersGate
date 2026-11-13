"""tscn_export: a Godot map as JSON another engine can load.

Written for a real port (a Godot RPG moving to Unity), so the fixtures are
the shapes that game's maps have: one TileMapLayer over a TileSet with a
custom-data "kind" layer, NPC and exit instances whose meaning is all in
exported properties, and a physics polygon on a wall tile. The near-misses
are the ones that would ship a wrong map without an error: a negative cell
coordinate, a value Godot wrapped across lines, a parent offset that has to
be added in.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bgate_core.level import sceneexport, tilemap

TILESET = """[gd_resource type="TileSet" load_steps=3 format=3]

[ext_resource type="Texture2D" path="res://assets/kinds.png" id="1_strip"]

[sub_resource type="TileSetAtlasSource" id="Atlas_kinds"]
texture = ExtResource("1_strip")
texture_region_size = Vector2i(32, 32)
use_texture_padding = false
0:0/0 = 0
0:0/0/custom_data_0 = "grass"
1:0/0 = 0
1:0/0/custom_data_0 = "wall"
1:0/0/physics_layer_0/polygon_0/points = PackedVector2Array(-16, -16, 16, -16, 16, 16, -16, 16)
2:0/size_in_atlas = Vector2i(2, 1)
2:0/0 = 0
2:0/1 = 1
2:0/1/flip_h = true

[resource]
tile_size = Vector2i(32, 32)
physics_layer_0/collision_layer = 1
custom_data_layer_0/name = "kind"
custom_data_layer_0/type = 4
sources/0 = SubResource("Atlas_kinds")
"""

NPC = """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/npc.gd" id="1_s"]

[node name="NPC" type="Node2D"]
script = ExtResource("1_s")
"""

# An instance of an instance: the script lives two scenes down.
VENDOR = """[gd_scene load_steps=2 format=3]

[ext_resource type="PackedScene" path="res://scenes/npc.tscn" id="1_n"]

[node name="Vendor" instance=ExtResource("1_n")]
"""


def _town(cells: list) -> str:
    packed = tilemap.encode_cells(cells)
    return f"""[gd_scene load_steps=6 format=3 uid="uid://ctown"]

; a comment line, which is not a property
[ext_resource type="Script" path="res://scripts/field.gd" id="1_field"]
[ext_resource type="TileSet" path="res://tiles/kinds.tres" id="2_tiles"]
[ext_resource type="PackedScene" path="res://scenes/npc.tscn" id="3_npc"]
[ext_resource type="PackedScene" path="res://scenes/vendor.tscn" id="4_vendor"]

[sub_resource type="LabelSettings" id="Label_sign"]
font_size = 8

[node name="Town" type="Node2D"]
script = ExtResource("1_field")
display_name = "Kingsburg \\"East\\""
is_world = false

[node name="Terrain" type="TileMapLayer" parent="."]
z_index = -1
tile_map_data = PackedByteArray("{packed}")
tile_set = ExtResource("2_tiles")

[node name="Props" type="Node2D" parent="." groups=["props", "solid"]]
position = Vector2(100, 50)

[node name="NPC_Kid" parent="Props" instance=ExtResource("3_npc")]
position = Vector2(10, 20)
npc_id = "kid"
lines = [
"hi",
"bye"
]

[node name="Vendor" parent="." instance=ExtResource("4_vendor")]
position = Vector2(64, 64)

[node name="Spin" type="Node2D" parent="."]
rotation = 1.5708

[node name="Under" type="Marker2D" parent="Spin"]
position = Vector2(8, 0)

[node name="Sign" type="Label" parent="."]
label_settings = SubResource("Label_sign")
modulate = Color(1, 0.5, 0.25, 1)
region = Rect2(0, 64, 32, 32)
"""


def _files(cells=None) -> dict[str, str]:
    cells = cells if cells is not None else [
        {"x": 0, "y": 0, "source": 0, "ax": 0, "ay": 0, "alt": 0},
        {"x": -1, "y": 2, "source": 0, "ax": 1, "ay": 0, "alt": 0},
        {"x": 3, "y": 1, "source": 0, "ax": 2, "ay": 0, "alt": 1},
    ]
    return {
        "res://scenes/town.tscn": _town(cells),
        "res://tiles/kinds.tres": TILESET,
        "res://scenes/npc.tscn": NPC,
        "res://scenes/vendor.tscn": VENDOR,
    }


def _export(files=None) -> dict:
    files = files or _files()
    doc = sceneexport.export_scene(files["res://scenes/town.tscn"],
                                   "res://scenes/town.tscn", files.get)
    json.dumps(doc, allow_nan=False)   # every export must be strict JSON
    return doc


def _node(doc, path):
    return next(n for n in doc["nodes"] if n["path"] == path)


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [
    ("Vector2(3, -4.5)", [3, -4.5]),
    ("Vector2i(2, 1)", [2, 1]),
    ("Rect2(0, 64, 32, 16)", {"position": [0, 64], "size": [32, 16]}),
    ("Color(1, 0, 0)", [1, 0, 0, 1.0]),
    ('"a \\"b\\"\\n"', 'a "b"\n'),
    ('&"walk"', "walk"),
    ('NodePath("../Door")', {"node_path": "../Door"}),
    ("PackedVector2Array(1, 2, 3, 4)", [[1, 2], [3, 4]]),
    ('PackedStringArray("a", "b")', ["a", "b"]),
    ('{"hp": 3, "tags": ["x"]}', {"hp": 3, "tags": ["x"]}),
    ("Array[int]([1, 2])", [1, 2]),
    ("1e-05", 1e-05),
    ("inf", {"godot": "inf"}),
    ("Basis(1, 0, 0, 0, 1, 0, 0, 0, 1)", {"godot": "Basis(1, 0, 0, 0, 1, 0, 0, 0, 1)"}),
])
def test_godot_literals_decode_to_json(raw, want):
    assert sceneexport.parse_value(raw) == want


def test_a_file_that_is_not_godot_text_is_refused():
    with pytest.raises(sceneexport.ExportError):
        sceneexport.export_scene("hello", "res://x.tscn", lambda _p: None)


# ---------------------------------------------------------------------------
# Layers and tilesets
# ---------------------------------------------------------------------------
def test_cells_come_out_sorted_and_signed():
    """A cell left of the origin is stored as 65535; exported as anything but
    -1 it lands sixty-five thousand tiles away in the other engine."""
    doc = _export()
    (layer,) = doc["layers"]
    assert layer["tileset"] == "res://tiles/kinds.tres"
    assert layer["z_index"] == -1
    assert layer["cells"] == [[0, 0, 0, 0, 0, 0], [3, 1, 0, 2, 0, 1],
                              [-1, 2, 0, 1, 0, 0]]
    assert "tile_map_data" not in _node(doc, "Terrain")["props"]


def test_the_tileset_carries_kinds_collision_and_alternatives():
    ts = _export()["tilesets"]["res://tiles/kinds.tres"]
    assert ts["tile_size"] == [32, 32]
    assert ts["layers"]["custom_data"] == [{"name": "kind", "type": 4, "index": 0}]
    src = ts["sources"]["0"]
    assert src["texture"] == "res://assets/kinds.png"
    assert src["region_size"] == [32, 32]
    assert src["use_texture_padding"] is False
    tiles = {(t["ax"], t["ay"], t["alt"]): t for t in src["tiles"]}
    assert tiles[(0, 0, 0)]["custom"] == {"kind": "grass"}
    assert tiles[(1, 0, 0)]["collision"] == {"0": [{"points": [
        [-16, -16], [16, -16], [16, 16], [-16, 16]]}]}
    assert tiles[(2, 0, 0)]["atlas"] == {"size_in_atlas": [2, 1]}
    assert tiles[(2, 0, 1)]["props"] == {"flip_h": True}


def test_a_tileset_shared_by_two_layers_is_exported_once():
    files = _files()
    files["res://scenes/town.tscn"] = files["res://scenes/town.tscn"].replace(
        '[node name="Props"',
        '[node name="Deco" type="TileMapLayer" parent="."]\n'
        'tile_set = ExtResource("2_tiles")\n\n[node name="Props"')
    doc = _export(files)
    assert [lay["name"] for lay in doc["layers"]] == ["Terrain", "Deco"]
    assert doc["layers"][1]["cells"] == []
    assert list(doc["tilesets"]) == ["res://tiles/kinds.tres"]


def test_a_missing_tileset_is_a_warning_not_a_crash():
    files = _files()
    del files["res://tiles/kinds.tres"]
    doc = _export(files)
    assert doc["tilesets"] == {"res://tiles/kinds.tres": None}
    assert any("could not be read" in w for w in doc["warnings"])
    assert len(doc["layers"][0]["cells"]) == 3


def test_a_legacy_tilemap_is_named_in_warnings():
    files = _files()
    files["res://scenes/town.tscn"] += (
        '\n[node name="Old" type="TileMap" parent="."]\n'
        'layer_0/tile_data = PackedInt32Array(0, 0, 0)\n')
    assert any("legacy TileMap" in w for w in _export(files)["warnings"])


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
def test_instances_carry_their_scene_script_and_exported_props():
    doc = _export()
    kid = _node(doc, "Props/NPC_Kid")
    assert kid["instance"] == "res://scenes/npc.tscn"
    assert kid["script"] == "res://scripts/npc.gd"
    assert kid["type"] == "Node2D"
    assert kid["props"]["npc_id"] == "kid"
    assert kid["props"]["lines"] == ["hi", "bye"]      # read across lines
    # The vendor instances npc.tscn; its script is found two scenes down.
    assert _node(doc, "Vendor")["script"] == "res://scripts/npc.gd"


def test_global_position_adds_the_parent_chain():
    doc = _export()
    assert _node(doc, "Props/NPC_Kid")["global_position"] == [110, 70]
    assert "global_approx" not in _node(doc, "Props/NPC_Kid")
    under = _node(doc, "Spin/Under")
    assert under["global_position"] == [8, 0]
    assert under["global_approx"] is True


def test_root_script_groups_resources_and_strings():
    doc = _export()
    assert doc["root"] == "Town" and doc["uid"] == "uid://ctown"
    root = _node(doc, ".")
    assert root["script"] == "res://scripts/field.gd"
    assert root["props"] == {"display_name": 'Kingsburg "East"',
                             "is_world": False}
    assert _node(doc, "Props")["groups"] == ["props", "solid"]
    sign = _node(doc, "Sign")["props"]
    assert sign["label_settings"] == {"sub": "Label_sign", "type": "LabelSettings",
                                      "props": {"font_size": 8}}
    assert sign["modulate"] == [1, 0.5, 0.25, 1]
    assert sign["region"] == {"position": [0, 64], "size": [32, 32]}


def test_a_self_instancing_scene_does_not_loop():
    files = _files()
    files["res://scenes/npc.tscn"] = VENDOR.replace("npc.tscn", "vendor.tscn")
    doc = _export(files)
    assert _node(doc, "Vendor")["script"] is None


# ---------------------------------------------------------------------------
# The MCP tool
# ---------------------------------------------------------------------------
def _game(tmp: Path, cells=None) -> Path:
    g = tmp / "game"
    for res, text in _files(cells).items():
        p = g / res[len("res://"):]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    (g / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    return g


@pytest.fixture()
def mcp(root, monkeypatch):
    """The server module with its project root pointed at the fixture."""
    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.delenv("BGATE_SEAT", raising=False)
    import bgate_mcp.server as server
    return server


def _call(fn, **kwargs):
    """Through the @_tool wrapper, which is where the project root is set."""
    import anyio
    return anyio.run(lambda: fn(**kwargs))


def test_the_tool_returns_a_small_export_inline(mcp, root):
    out = _call(mcp.tscn_export, godot_project=str(_game(root)),
                scene="scenes/town.tscn")
    assert out["ok"], out
    assert (out["layers"], out["cells"], out["instances"]) == (1, 3, 2)
    assert out["export"]["scene"] == "res://scenes/town.tscn"


def test_the_tool_writes_a_big_export_and_refuses_it_inline(mcp, root):
    cells = [{"x": x, "y": y, "source": 0, "ax": 0, "ay": 0, "alt": 0}
             for x in range(60) for y in range(60)]
    game = _game(root, cells)
    big = _call(mcp.tscn_export, godot_project=str(game),
                scene="res://scenes/town.tscn")
    assert big["ok"] is False and "out_path" in big["error"]
    assert "export" not in big

    dest = root / "port" / "Maps" / "town.json"
    out = _call(mcp.tscn_export, godot_project=str(game),
                scene="res://scenes/town.tscn", out_path=str(dest))
    assert out["ok"] and out["cells"] == 3600
    assert json.loads(dest.read_text(encoding="utf-8"))["layers"][0]["cells"][0] \
        == [0, 0, 0, 0, 0, 0]


def test_the_tool_refuses_a_non_json_out_path(mcp, root):
    out = _call(mcp.tscn_export, godot_project=str(_game(root)),
                scene="scenes/town.tscn", out_path=str(root / "town.txt"))
    assert out["ok"] is False and ".json" in out["error"]
