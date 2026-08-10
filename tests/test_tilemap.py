"""TileMapLayer — two undocumented binary/text formats, read correctly or not at all.

The failure mode here is never an exception. A wrong stride reads plausible
garbage; a signed/unsigned slip puts tiles 65,000 units away; the square
projection applied to an isometric map renders a tidy grid that is confidently
wrong. So every one of those is pinned against bytes rather than eyeballed.
"""
from __future__ import annotations

import base64
import struct

import pytest

from bgate_core import tilemap


def _packed(cells, version=0):
    blob = struct.pack("<H", version)
    for x, y, source, ax, ay, alt in cells:
        blob += struct.pack("<HHHHHH", x & 0xFFFF, y & 0xFFFF, source,
                            ax & 0xFFFF, ay & 0xFFFF, alt)
    return base64.b64encode(blob).decode()


TILESET = """[gd_resource type="TileSet" format=3]

[ext_resource type="Texture2D" path="res://tiles/floor.png" id="1_f"]
[ext_resource type="Texture2D" path="res://tiles/wall.png" id="2_w"]

[sub_resource type="TileSetAtlasSource" id="TileSetAtlasSource_a"]
texture = ExtResource("1_f")
texture_region_size = Vector2i(64, 32)
0:0/0 = 0

[sub_resource type="TileSetAtlasSource" id="TileSetAtlasSource_b"]
texture = ExtResource("2_w")
texture_region_size = Vector2i(64, 96)
texture_origin = Vector2i(0, -32)
0:0/0 = 0

[resource]
tile_shape = 1
tile_layout = 5
tile_size = Vector2i(64, 32)
sources/0 = SubResource("TileSetAtlasSource_a")
sources/7 = SubResource("TileSetAtlasSource_b")
"""


# ---------------------------------------------------------------------------
# tile_map_data
# ---------------------------------------------------------------------------
def test_cells_decode_at_twelve_bytes_each():
    packed = _packed([(0, 0, 39, 0, 0, 0), (1, 2, 7, 3, 4, 0)])
    cells = tilemap.decode_cells(packed)
    assert len(cells) == 2
    assert cells[0] == {"x": 0, "y": 0, "source": 39, "ax": 0, "ay": 0, "alt": 0}
    assert cells[1] == {"x": 1, "y": 2, "source": 7, "ax": 3, "ay": 4, "alt": 0}


def test_negative_coordinates_survive_the_uint16_storage():
    """Stored unsigned; a map extending left or up would otherwise render
    sixty-five thousand tiles away from everything else."""
    cells = tilemap.decode_cells(_packed([(-3, -1, 0, -2, 0, 0)]))
    assert (cells[0]["x"], cells[0]["y"]) == (-3, -1)
    assert cells[0]["ax"] == -2


def test_empty_and_header_only_data_are_empty_not_errors():
    assert tilemap.decode_cells("") == []
    assert tilemap.decode_cells(_packed([])) == []


def test_a_truncated_cell_is_refused_rather_than_half_read():
    blob = base64.b64decode(_packed([(1, 1, 0, 0, 0, 0)]))
    with pytest.raises(tilemap.TileError) as exc:
        tilemap.decode_cells(base64.b64encode(blob[:-3]).decode())
    assert "whole number" in str(exc.value)


def test_non_base64_is_refused():
    with pytest.raises(tilemap.TileError):
        tilemap.decode_cells("!!!! not base64 !!!!")


# ---------------------------------------------------------------------------
# encode_cells — the write half
# ---------------------------------------------------------------------------
def test_encode_round_trips_through_decode():
    cells = [{"x": -3, "y": 4, "source": 7, "ax": 2, "ay": -1, "alt": 0},
             {"x": 0, "y": 0, "source": 0, "ax": 0, "ay": 0, "alt": 0}]
    assert tilemap.decode_cells(tilemap.encode_cells(cells)) == sorted(
        cells, key=lambda c: (c["y"], c["x"]))


def test_encode_is_byte_identical_regardless_of_input_order():
    """Same level, same bytes — otherwise re-running a generator on the same
    seed produces a diff of the whole scene."""
    a = [(0, 0, 1, 0, 0), (5, 2, 1, 0, 0), (3, 9, 1, 0, 0)]
    assert tilemap.encode_cells(a) == tilemap.encode_cells(list(reversed(a)))


def test_encode_takes_the_five_value_lists_layer_draw_hands_back():
    ts = tilemap.parse_tileset(TILESET)
    out = tilemap.layer_draw(_packed([(1, 1, 0, 0, 0, 0)]), ts)
    assert tilemap.decode_cells(tilemap.encode_cells(out["cells"])) == [
        {"x": 1, "y": 1, "source": 0, "ax": 0, "ay": 0, "alt": 0}]


def test_a_coordinate_outside_int16_is_refused_not_wrapped():
    with pytest.raises(tilemap.TileError) as exc:
        tilemap.encode_cells([{"x": 70000, "y": 0, "source": 0}])
    assert "int16" in str(exc.value)


def test_two_cells_on_one_coordinate_are_refused():
    """A layer holds one tile per coordinate; the loser would silently vanish."""
    with pytest.raises(tilemap.TileError) as exc:
        tilemap.encode_cells([{"x": 1, "y": 1, "source": 0},
                              {"x": 1, "y": 1, "source": 3}])
    assert "(1, 1)" in str(exc.value)


def test_encoding_nothing_is_a_valid_empty_layer():
    assert tilemap.decode_cells(tilemap.encode_cells([])) == []


# ---------------------------------------------------------------------------
# The TileSet
# ---------------------------------------------------------------------------
def test_the_tileset_resolves_sources_to_textures_and_regions():
    ts = tilemap.parse_tileset(TILESET)
    assert ts["tile_size"] == [64, 32]
    assert ts["shape"] == tilemap.ISOMETRIC
    assert ts["layout"] == tilemap.DIAMOND_DOWN
    assert ts["sources"][0]["texture"] == "res://tiles/floor.png"
    assert ts["sources"][0]["region"] == [64, 32]
    # Source ids are NOT indexes — a tileset numbers them however it likes.
    assert ts["sources"][7]["texture"] == "res://tiles/wall.png"
    assert ts["sources"][7]["region"] == [64, 96]
    assert ts["sources"][7]["origin"] == [0, -32]


def test_the_tiles_an_atlas_defines_are_listed():
    """A cell pointing at a coordinate the atlas never declared draws nothing
    and reports nothing, so a generator has to be able to ask what exists."""
    ts = tilemap.parse_tileset(TILESET)
    assert ts["sources"][0]["tiles"] == [(0, 0)]


def test_per_tile_property_lines_are_not_mistaken_for_tiles():
    text = TILESET.replace("0:0/0 = 0\n",
                           "0:0/0 = 0\n0:0/0/physics_layer_0/polygon_0/points = "
                           "PackedVector2Array(0, 0)\n2:1/0 = 0\n")
    ts = tilemap.parse_tileset(text)
    assert ts["sources"][0]["tiles"] == [(0, 0), (2, 1)]


def test_the_tile_list_stays_out_of_the_draw_payload():
    ts = tilemap.parse_tileset(TILESET)
    out = tilemap.layer_draw(_packed([(0, 0, 0, 0, 0, 0)]), ts)
    assert "tiles" not in out["sources"][0]
    assert out["sources"][0]["texture"] == "res://tiles/floor.png"


def test_a_source_with_no_texture_is_dropped_not_guessed():
    text = TILESET.replace('texture = ExtResource("2_w")\n', "")
    ts = tilemap.parse_tileset(text)
    assert 0 in ts["sources"] and 7 not in ts["sources"]


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------
def test_a_square_map_is_a_multiply():
    at = tilemap.cell_center(2, 3, shape=tilemap.SQUARE,
                             layout=tilemap.DIAMOND_RIGHT, tile_size=[16, 16])
    assert at == (2 * 16 + 8, 3 * 16 + 8)


def test_an_isometric_diamond_down_map_is_a_diamond():
    """The square formula here renders a tidy grid that is confidently wrong."""
    kw = dict(shape=tilemap.ISOMETRIC, layout=tilemap.DIAMOND_DOWN,
              tile_size=[64, 32])
    assert tilemap.cell_center(0, 0, **kw) == (32.0, 16.0)
    assert tilemap.cell_center(1, 0, **kw) == (64.0, 32.0)
    assert tilemap.cell_center(0, 1, **kw) == (0.0, 32.0)
    assert tilemap.cell_center(1, 1, **kw) == (32.0, 48.0)


def test_the_isometric_origin_is_the_diamonds_centre_not_its_corner():
    """The half-cell term, pinned. Dropping it is invisible on the tile side.

    `bounds` is derived from cell_center, so a layer missing the term stays
    perfectly self-consistent and only disagrees with the NODES standing on it
    — whose positions come out of the .tscn already in engine coordinates. The
    symptom was "the editor is off positioning-wise": every floor tile drew
    half a diamond up and left of the desks on it. Cell (0,0) of a 64x32 iso
    tileset is centred at (32, 16), exactly as cell (0,0) of a square one is
    centred at (w/2, h/2).
    """
    iso = tilemap.cell_center(0, 0, shape=tilemap.ISOMETRIC,
                              layout=tilemap.DIAMOND_DOWN, tile_size=[64, 32])
    square = tilemap.cell_center(0, 0, shape=tilemap.SQUARE,
                                 layout=tilemap.DIAMOND_DOWN, tile_size=[64, 32])
    assert iso == square == (32.0, 16.0)


def test_diamond_right_runs_the_other_diagonal():
    kw = dict(shape=tilemap.ISOMETRIC, layout=tilemap.DIAMOND_RIGHT,
              tile_size=[64, 32])
    assert tilemap.cell_center(1, 0, **kw) == (64.0, 0.0)
    assert tilemap.cell_center(0, 1, **kw) == (64.0, 32.0)


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------
def test_layer_draw_sends_only_the_sources_it_uses():
    ts = tilemap.parse_tileset(TILESET)
    out = tilemap.layer_draw(_packed([(0, 0, 0, 0, 0, 0), (1, 1, 0, 0, 0, 0)]), ts)
    assert list(out["sources"]) == [0], "source 7 is not placed anywhere"
    assert out["cells"] == [[0, 0, 0, 0, 0], [1, 1, 0, 0, 0]]
    assert out["skipped"] == 0


def test_a_cell_naming_a_source_the_tileset_lacks_is_counted_not_drawn():
    ts = tilemap.parse_tileset(TILESET)
    out = tilemap.layer_draw(_packed([(0, 0, 0, 0, 0, 0), (1, 0, 99, 0, 0, 0)]), ts)
    assert len(out["cells"]) == 1
    assert out["skipped"] == 1, "a missing source must be reported, not ignored"


def test_bounds_cover_the_whole_placed_area():
    ts = tilemap.parse_tileset(TILESET)
    out = tilemap.layer_draw(_packed([(0, 0, 0, 0, 0, 0), (4, 4, 0, 0, 0, 0)]), ts)
    b = tilemap.bounds(out["cells"], shape=out["shape"], layout=out["layout"],
                       tile_size=out["tile_size"])
    # Shifted by the half-cell the isometric projection was missing: a layer's
    # area starts at the top corner of cell (0,0), which is (0, 0) when the
    # cell's CENTRE is at (w/2, h/2).
    assert b == (0.0, 0.0, 64.0, 160.0)


def test_bounds_of_nothing_is_none():
    assert tilemap.bounds([], shape=0, layout=4, tile_size=[16, 16]) is None


# ---------------------------------------------------------------------------
# Through the draw list
# ---------------------------------------------------------------------------
def test_a_tilemap_node_becomes_a_tiles_draw_item():
    from bgate_core import scenedraw

    scene = ('[gd_scene load_steps=2 format=3]\n\n'
             '[ext_resource type="TileSet" path="res://t.tres" id="1_t"]\n\n'
             '[node name="Root" type="Node2D"]\n\n'
             '[node name="Ground" type="TileMapLayer" parent="."]\n'
             f'tile_map_data = PackedByteArray("{_packed([(0,0,0,0,0,0),(1,1,0,0,0,0)])}")\n'
             'tile_set = ExtResource("1_t")\n')
    out = scenedraw.draw_list(
        scene,
        read=lambda p: TILESET if p == "res://t.tres" else None,
        size_of=lambda p: (64, 32),
        rel_of=lambda p: p.replace("res://", "game/"))
    d = next(i for i in out["items"] if i["name"] == "Ground")["draw"]
    assert d["kind"] == "tiles"
    assert len(d["cells"]) == 2
    assert d["sources"]["0"]["rel"] == "game/tiles/floor.png"
    assert d["shape"] == tilemap.ISOMETRIC and d["layout"] == tilemap.DIAMOND_DOWN


def test_a_tilemap_with_no_tileset_says_so_rather_than_vanishing():
    from bgate_core import scenedraw

    scene = ('[gd_scene load_steps=1 format=3]\n\n'
             '[node name="Root" type="Node2D"]\n\n'
             '[node name="Ground" type="TileMapLayer" parent="."]\n')
    out = scenedraw.draw_list(scene, read=lambda p: None,
                              size_of=lambda p: None, rel_of=lambda p: None)
    d = next(i for i in out["items"] if i["name"] == "Ground")["draw"]
    assert d["kind"] == "marker" and "TileSet" in d["reason"]


def test_the_client_projection_matches_the_server_one():
    """cellCenter in sceneview.js mirrors cell_center here. If they drift, the
    tiles land somewhere the bounds do not cover and framing goes wrong."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "bgate_ui" / "static"
          / "sceneview.js").read_text(encoding="utf-8")
    assert "function cellCenter(" in js
    # The half-cell term has to be on BOTH sides or the tiles land somewhere the
    # bounds do not cover — and, worse, somewhere the props do not stand.
    assert "(x - y) * w / 2 + w / 2" in js and "(x + y) * h / 2 + h / 2" in js
    assert "(x + y) * w / 2 + w / 2" in js and "(y - x) * h / 2 + h / 2" in js
