"""Reading a tile sheet: which tile answers which neighbour shape.

Everything here is a synthetic sheet with masks chosen up front, because the
question is whether the reader recovers what was drawn — a generated sheet can
only ever say "this looks plausible".

The one measured lesson worth keeping: the first version of the detector
sampled a probe box 10% inside the tile, and on real art it read a tile whose
western pixel column is grass as pure interior stone, because the transition
strip is two pixels wide. The seam check caught the DETECTOR, not the art. Both
now read the boundary, so a disagreement between them means something.
"""
from __future__ import annotations

import pytest

from bgate_core import tilemask
from bgate_core.autotile import E, N, S, W

GREEN = (90, 150, 40, 255)
GREY = (130, 125, 125, 255)


def _tile(draw, tx, ty, size, body, sides):
    """One tile: body colour, with `sides` painted as the OTHER terrain."""
    other = GREY if body == GREEN else GREEN
    x0, y0 = tx * size, ty * size
    draw.rectangle([x0, y0, x0 + size - 1, y0 + size - 1], fill=body)
    band = max(2, size // 8)
    if "N" in sides:
        draw.rectangle([x0, y0, x0 + size - 1, y0 + band - 1], fill=other)
    if "S" in sides:
        draw.rectangle([x0, y0 + size - band, x0 + size - 1, y0 + size - 1],
                       fill=other)
    if "W" in sides:
        draw.rectangle([x0, y0, x0 + band - 1, y0 + size - 1], fill=other)
    if "E" in sides:
        draw.rectangle([x0 + size - band, y0, x0 + size - 1, y0 + size - 1],
                       fill=other)


def _sheet(layout, size=16):
    """layout: [(body, sides)] laid left to right."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size * len(layout), size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i, (body, sides) in enumerate(layout):
        _tile(draw, i, 0, size, body, sides)
    return img


ALL = N | E | S | W


class TestDetect:
    def test_a_pure_tile_reads_as_every_side_the_same(self):
        got = tilemask.detect(_sheet([(GREY, ""), (GREEN, "")]),
                              tile_size=(16, 16), bits=4)
        assert got["ok"] is True
        masks = {t["mask"] for t in got["tiles"]}
        assert masks == {ALL}

    def test_an_edge_tile_drops_that_side(self):
        # stone with grass along its west edge = every side but W
        got = tilemask.detect(_sheet([(GREY, "W"), (GREEN, "")]),
                              tile_size=(16, 16), bits=4)
        stone = [t for t in got["tiles"] if t["at"] == [0, 0]][0]
        assert stone["mask"] == ALL & ~W
        assert stone["zones"]["W"] is False and stone["zones"]["E"] is True

    def test_a_corner_drops_two_sides(self):
        got = tilemask.detect(_sheet([(GREY, "NW"), (GREEN, "")]),
                              tile_size=(16, 16), bits=4)
        stone = [t for t in got["tiles"] if t["at"] == [0, 0]][0]
        assert stone["mask"] == ALL & ~(N | W)

    def test_both_terrains_come_back(self):
        """A grass/stone sheet holds two autotile sets sharing one atlas, and
        a game wants whichever it is painting with."""
        got = tilemask.detect(_sheet([(GREY, "W"), (GREEN, "E")]),
                              tile_size=(16, 16), bits=4)
        assert set(got["terrains"]) == {"a", "b"}
        assert got["terrains"]["a"]["table"] and got["terrains"]["b"]["table"]

    def test_duplicates_become_alternatives_not_an_exception(self):
        """Terrain.from_table RAISES on a collision, and a sheet drawing two
        pure-grass tiles is completely normal."""
        got = tilemask.detect(_sheet([(GREY, ""), (GREY, ""), (GREEN, "")]),
                              tile_size=(16, 16), bits=4)
        stone = got["terrains"]["a"] if got["terrains"]["a"]["colour"][1] < 140 \
            else got["terrains"]["b"]
        assert len(stone["table"]) == 1
        assert stone["alts"][ALL], "the second pure tile is an alternative"

    def test_art_that_does_not_separate_is_refused_not_guessed(self):
        """One terrain with shading is not two terrains, and a mask table
        built from it puts the wrong sprite at every seam while every
        downstream number stays self-consistent."""
        got = tilemask.detect(_sheet([(GREY, ""), (GREY, "")]),
                              tile_size=(16, 16), bits=4)
        assert got["ok"] is False
        assert "do not separate" in got["reason"]

    def test_a_sheet_too_small_to_hold_a_tile_raises(self):
        from PIL import Image
        with pytest.raises(tilemask.MaskError, match="holds no"):
            tilemask.detect(Image.new("RGBA", (8, 8)), tile_size=(16, 16))


class TestSynthesise:
    def test_a_missing_mask_is_filled_by_flipping_its_mirror(self):
        # only a west-edge tile exists; the east-edge one is a flip away
        sheet = _sheet([(GREY, "W"), (GREEN, "")])
        got = tilemask.detect(sheet, tile_size=(16, 16), bits=4)
        stone = got["terrains"]["a"]
        table = stone["table"]
        want_east = ALL & ~E
        syn = tilemask.synthesise(sheet, table, [want_east],
                                  tile_size=(16, 16))
        assert want_east in syn["table"]
        assert syn["made"][want_east][1] == "h"

    def test_what_cannot_be_flipped_is_reported_missing(self):
        sheet = _sheet([(GREY, ""), (GREEN, "")])
        got = tilemask.detect(sheet, tile_size=(16, 16), bits=4)
        table = got["terrains"]["a"]["table"]
        syn = tilemask.synthesise(sheet, table, [ALL & ~(N | E | S | W)],
                                  tile_size=(16, 16))
        assert syn["still_missing"] == [0]


class TestSeams:
    def _colours(self, got):
        return [got["terrains"]["a"]["colour"], got["terrains"]["b"]["colour"]]

    def test_tiles_whose_terrain_continues_are_clean(self):
        sheet = _sheet([(GREY, ""), (GREEN, "")])
        got = tilemask.detect(sheet, tile_size=(16, 16), bits=4)
        stone = got["terrains"]["a"]
        rep = tilemask.seam_report(sheet, stone["table"], tile_size=(16, 16),
                                   colours=self._colours(got))
        assert rep["findings"] == []

    def test_a_join_that_changes_terrain_is_flagged(self):
        """mask says stone continues east; the tile to its east shows grass on
        its western edge, so the ground does not continue across the join."""
        sheet = _sheet([(GREY, ""), (GREY, "W"), (GREEN, "")])
        got = tilemask.detect(sheet, tile_size=(16, 16), bits=4)
        stone = got["terrains"]["a"]
        table = dict(stone["table"])
        table.update({m: c for m, c in stone["table"].items()})
        rep = tilemask.seam_report(sheet, table, tile_size=(16, 16),
                                   colours=self._colours(got), limit=0.05)
        assert rep["checked"] > 0
