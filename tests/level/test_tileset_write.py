"""Writing a Godot TileSet, and refusing to write a broken one.

The round trip here is cheap and real: `parse_tileset` already existed and is
what `level_generate` reads projects with, so a resource this module writes has
to survive its own reader. What that CANNOT catch is a misconception the reader
and writer share — neither of them owns the format — which is why the engine
gate in godot.inspect_tileset exists and why these tests do not pretend to
replace it.
"""
from __future__ import annotations

import pytest

from bgate_core.level import tilemap


def _one(**over):
    src = {"id": 0, "texture": "res://assets/tiles/ground.png",
           "tiles": [(0, 0), (1, 0), (0, 1)]}
    src.update(over)
    return [src]


class TestRoundTrip:
    def test_what_is_written_parses_back(self):
        text = tilemap.write_tileset(_one(), tile_size=(32, 32))
        back = tilemap.parse_tileset(text)
        assert back["tile_size"] == [32, 32]
        assert back["sources"][0]["texture"] == "res://assets/tiles/ground.png"
        assert back["sources"][0]["tiles"] == [(0, 0), (0, 1), (1, 0)]

    def test_byte_stable_across_runs(self):
        a = tilemap.write_tileset(_one(), tile_size=(16, 16))
        b = tilemap.write_tileset(_one(tiles=[(1, 0), (0, 1), (0, 0)]),
                                  tile_size=(16, 16))
        assert a == b, "coordinate order must not change the bytes"

    def test_a_source_can_be_appended_without_rewriting_the_file(self):
        """The prop-manifest seam: the atlas prop_generate installs has to
        become a SOURCE of the level's tileset, and rewriting the whole file
        would drop whatever the parser does not round-trip."""
        text = tilemap.write_tileset(_one(), tile_size=(32, 32),
                                     physics=True)
        got = tilemap.append_source(text, {
            "texture": "res://assets/tiles/props.png",
            "tiles": [(0, 0), (1, 0)],
            "sizes": {(1, 0): (1, 2)},
            "origins": {(0, 0): (0, -8)},
            "animation": {(0, 0): {"frames": 2, "speed": 6.0}}})
        assert got["reused"] is False and got["id"] == 1
        back = tilemap.parse_tileset(got["text"])
        assert sorted(back["sources"]) == [0, 1]
        assert back["sources"][1]["texture"] == "res://assets/tiles/props.png"
        assert back["sources"][0]["tiles"] == [(0, 0), (0, 1), (1, 0)]
        # the original bytes are still there, physics line included
        assert "physics_layer_0/collision_layer = 1" in got["text"]
        assert 'sources/1 = SubResource("TileSetAtlasSource_1")' in got["text"]

    def test_appending_the_same_texture_twice_reuses_the_source(self):
        text = tilemap.write_tileset(_one(), tile_size=(32, 32))
        one = tilemap.append_source(text, {
            "texture": "res://p.png", "tiles": [(0, 0)]})
        two = tilemap.append_source(one["text"], {
            "texture": "res://p.png", "tiles": [(0, 0)]})
        assert two["reused"] is True and two["id"] == one["id"]
        assert two["text"] == one["text"]

    def test_isometric_shape_survives(self):
        text = tilemap.write_tileset(_one(), tile_size=(32, 16),
                                     shape=tilemap.ISOMETRIC,
                                     layout=tilemap.DIAMOND_DOWN)
        back = tilemap.parse_tileset(text)
        assert back["shape"] == tilemap.ISOMETRIC
        assert back["layout"] == tilemap.DIAMOND_DOWN
        assert back["tile_size"] == [32, 16]

    def test_two_sources_keep_their_ids(self):
        text = tilemap.write_tileset(
            [{"id": 0, "texture": "res://a.png", "tiles": [(0, 0)]},
             {"id": 3, "texture": "res://b.png", "tiles": [(2, 2)]}],
            tile_size=(16, 16))
        back = tilemap.parse_tileset(text)
        assert sorted(back["sources"]) == [0, 3]
        assert back["sources"][3]["tiles"] == [(2, 2)]


class TestRefusals:
    def test_no_sources(self):
        with pytest.raises(tilemap.TileError, match="at least one"):
            tilemap.write_tileset([], tile_size=(16, 16))

    def test_a_source_defining_no_tiles(self):
        """Godot draws nothing for an undefined coordinate and reports
        nothing; level_generate would refuse a layer later with a coordinate
        list, which names the symptom rather than the mistake."""
        with pytest.raises(tilemap.TileError, match="defines no tiles"):
            tilemap.write_tileset(_one(tiles=[]), tile_size=(16, 16))

    def test_a_negative_tile_coordinate(self):
        with pytest.raises(tilemap.TileError, match="negative"):
            tilemap.write_tileset(_one(tiles=[(0, 0), (-1, 2)]),
                                  tile_size=(16, 16))

    def test_two_sources_sharing_an_id(self):
        with pytest.raises(tilemap.TileError, match="share one source id"):
            tilemap.write_tileset(
                [{"id": 1, "texture": "res://a.png", "tiles": [(0, 0)]},
                 {"id": 1, "texture": "res://b.png", "tiles": [(0, 0)]}],
                tile_size=(16, 16))

    def test_a_source_without_a_texture(self):
        with pytest.raises(tilemap.TileError, match="texture path"):
            tilemap.write_tileset(_one(texture=""), tile_size=(16, 16))

    def test_an_undrawable_tile_size(self):
        with pytest.raises(tilemap.TileError, match="not drawable"):
            tilemap.write_tileset(_one(), tile_size=(0, 16))


class TestCollision:
    """Godot's own format, learned by having Godot save a tileset and reading
    it back — nobody documents this and two guesses were wrong. The engine
    reported ZERO shapes for a resource that loaded fine, twice: once for an
    extra `polygons_count` line Godot never writes, and once because the
    collision map was dropped during source normalisation."""

    def _poly(self):
        return [[(-16, -16), (-7, -16), (-7, 16), (-16, 16)]]

    def test_no_polygons_count_line(self):
        """Godot infers the count and DROPS every polygon when the count is
        written explicitly — the resource still loads, reporting no shapes."""
        text = tilemap.write_tileset(
            [{"id": 0, "texture": "res://a.png", "tiles": [(0, 0)],
              "collision": {(0, 0): self._poly()}}],
            tile_size=(32, 32), physics=True)
        assert "polygons_count" not in text

    def test_several_polygons_per_tile(self):
        """A corridor is solid on two sides and an isolated tile on four; a
        ring is not one convex polygon."""
        text = tilemap.write_tileset(
            [{"id": 0, "texture": "res://a.png", "tiles": [(0, 0)],
              "collision": {(0, 0): [[(0, 0), (1, 0), (1, 1)],
                                     [(2, 2), (3, 2), (3, 3)]]}}],
            tile_size=(32, 32), physics=True)
        assert "polygon_0/points" in text and "polygon_1/points" in text

    def test_a_tile_with_no_polygon_is_walkable_by_omission(self):
        text = tilemap.write_tileset(
            [{"id": 0, "texture": "res://a.png", "tiles": [(0, 0), (1, 0)],
              "collision": {(0, 0): self._poly()}}],
            tile_size=(32, 32), physics=True)
        assert "1:0/0/physics_layer_0" not in text

    def test_no_physics_layer_unless_asked(self):
        text = tilemap.write_tileset(
            [{"id": 0, "texture": "res://a.png", "tiles": [(0, 0)]}],
            tile_size=(32, 32))
        assert "physics_layer" not in text
