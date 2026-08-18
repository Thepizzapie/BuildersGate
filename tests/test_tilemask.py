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


class TestNormaliseEdges:
    """The defect that survived every other check: generated tiles draw each
    edge at a DIFFERENT depth, so neighbours disagree about where the floor
    stops and the boundary bulges. Measured on real art: one-void tiles
    61-82% floor, two-void 28-73%."""

    def _set(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (16 * 3, 16), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # full floor, then two edge tiles drawn at WILDLY different insets
        d.rectangle([0, 0, 15, 15], fill=GREY)
        d.rectangle([16, 0, 31, 15], fill=GREY)
        d.rectangle([16, 0, 18, 15], fill=GREEN)          # thin void band
        d.rectangle([32, 0, 47, 15], fill=GREY)
        d.rectangle([32, 0, 43, 15], fill=GREEN)          # fat void band
        return img

    def test_every_tile_ends_up_with_the_same_inset(self):
        import numpy as np
        img = self._set()
        got = tilemask.detect(img, tile_size=(16, 16), bits=4)
        floor = max(got["terrains"].values(), key=lambda t: t["colour"][0])
        colours = [got["terrains"]["a"]["colour"], got["terrains"]["b"]["colour"]]
        table = dict(floor["table"])
        norm = tilemask.normalise_edges(img, table, list(range(16)),
                                        tile_size=(16, 16), colours=colours)
        assert norm["ok"] is True
        arr = np.asarray(norm["image"].convert("RGB")).astype(int)
        lum = arr.mean(axis=2)
        by_voids = {}
        for m in range(16):
            tx, ty = norm["table"][m]
            frac = float((lum[ty*16:(ty+1)*16, tx*16:(tx+1)*16] > 60).mean())
            by_voids.setdefault(4 - bin(m).count("1"), []).append(frac)
        for voids, fracs in by_voids.items():
            assert max(fracs) - min(fracs) < 0.12, (voids, fracs)

    def test_it_refuses_without_a_full_floor_tile_to_build_from(self):
        img = self._set()
        got = tilemask.detect(img, tile_size=(16, 16), bits=4)
        colours = [got["terrains"]["a"]["colour"], got["terrains"]["b"]["colour"]]
        out = tilemask.normalise_edges(img, {3: (0, 0)}, list(range(16)),
                                       tile_size=(16, 16), colours=colours)
        assert out["ok"] is False and "full-floor" in out["reason"]


class TestCollisionPolygons:
    def test_a_fully_open_tile_has_no_collider(self):
        polys = tilemask.collision_polygons([N | E | S | W], tile_size=(32, 32))
        assert polys[N | E | S | W] == []

    def test_one_band_per_void_side(self):
        polys = tilemask.collision_polygons([N | E | S, N | S, 0],
                                            tile_size=(32, 32))
        assert len(polys[N | E | S]) == 1        # void on W only
        assert len(polys[N | S]) == 2            # corridor: void E and W
        assert len(polys[0]) == 4                # isolated: void all round

    def test_coordinates_are_centred_on_the_tile(self):
        """Godot's tile collision space puts (0,0) at the MIDDLE."""
        [band] = tilemask.collision_polygons([N | E | S], tile_size=(32, 32))[N | E | S]
        xs = [p[0] for p in band]
        assert min(xs) == -16.0, band


class TestVoidIsAHole:
    """The defect that made every room read as a protrusion and every one-cell
    corridor as a black crack: the void band was painted with the void
    TERRAIN, opaque, so the floor layer covered the wall layer beneath it.
    Nothing in the tileset or the scene could show that — it only appears on a
    composited map, which is why it survived the engine gate."""

    def _set(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (16 * 3, 16), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 15, 15], fill=GREY)
        d.rectangle([16, 0, 31, 15], fill=GREY)
        d.rectangle([16, 0, 18, 15], fill=GREEN)
        d.rectangle([32, 0, 47, 15], fill=GREY)
        d.rectangle([32, 0, 43, 15], fill=GREEN)
        return img

    def _norm(self, **kw):
        img = self._set()
        got = tilemask.detect(img, tile_size=(16, 16), bits=4)
        colours = [got["terrains"]["a"]["colour"],
                   got["terrains"]["b"]["colour"]]
        floor = max(got["terrains"].values(), key=lambda t: t["colour"][0])
        return tilemask.normalise_edges(img, dict(floor["table"]),
                                        list(range(16)), tile_size=(16, 16),
                                        colours=colours, **kw)

    def test_carving_makes_the_band_a_hole(self):
        import numpy as np
        out = self._norm(clear=True)
        a = np.asarray(out["image"])
        assert (a[..., 3] == 0).any(), "nothing was carved"
        # the fully-open tile keeps all four sides
        tx, ty = out["table"][N | E | S | W]
        assert (a[ty*16:(ty+1)*16, tx*16:(tx+1)*16, 3] == 255).all()
        # an isolated tile is void on all four sides, so its rim is a hole
        tx, ty = out["table"][0]
        assert a[ty*16, tx*16, 3] == 0

    def test_the_default_stays_opaque_because_the_wall_fill_is_behind_it(self):
        """Settled by a screenshot of the running game, not by the sheet: with a
        wall FILL behind the floor layer the band is the wall face, and carving
        it punches through to the clear colour — a light seam traced around
        every room."""
        import numpy as np
        a = np.asarray(self._norm()["image"])
        assert (a[..., 3] == 255).all(), "the default must stay opaque"


class TestColliderIsNotTheArtLip:
    """These were one constant. Reducing the drawn lip to 0.1 — correct, because
    the wall layer draws the wall — would have shrunk every collider to three
    pixels, and the player would walk most of a tile into stone."""

    def test_the_collider_does_not_follow_edge_inset(self):
        assert tilemask.COLLIDER_INSET > tilemask.EDGE_INSET
        [band] = tilemask.collision_polygons([N | E | S],
                                             tile_size=(32, 32))[N | E | S]
        width = max(p[0] for p in band) - min(p[0] for p in band)
        assert width == round(32 * tilemask.COLLIDER_INSET)

    def test_a_wall_layer_asks_for_the_whole_tile(self):
        polys = tilemask.collision_polygons([N | E | S | W, 0],
                                            tile_size=(32, 32), full=True)
        for mask, got in polys.items():
            assert len(got) == 1, mask
            assert got[0] == [(-16.0, -16.0), (16.0, -16.0),
                              (16.0, 16.0), (-16.0, 16.0)], mask


class TestDiagonalsAreWhySixteenIsNotEnough:
    """The defect: the shadow band along a wall BREAKS at every step in a room's
    outline. A 4-bit mask cannot say "floor north and east, void at the
    north-east corner", so there is no tile to draw there and the boundary has a
    notch. The corner is a nibble out of the tile — pure geometry — so all 47
    can be built from a 16-tile generation rather than asking a model for
    corners it draws badly."""

    def _built(self, masks):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (16 * 3, 16), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 15, 15], fill=GREY)
        d.rectangle([16, 0, 31, 15], fill=GREY)
        d.rectangle([16, 0, 18, 15], fill=GREEN)
        d.rectangle([32, 0, 47, 15], fill=GREY)
        d.rectangle([32, 0, 43, 15], fill=GREEN)
        got = tilemask.detect(img, tile_size=(16, 16), bits=4)
        colours = [got["terrains"]["a"]["colour"], got["terrains"]["b"]["colour"]]
        floor = max(got["terrains"].values(), key=lambda t: t["colour"][0])
        return tilemask.normalise_edges(img, dict(floor["table"]), masks,
                                        tile_size=(16, 16), colours=colours,
                                        clear=True)

    def test_all_forty_seven_masks_come_out(self):
        from bgate_core import autotile
        masks = autotile.blob47_masks()
        out = self._built(masks)
        assert out["ok"] is True
        assert len(out["table"]) == 47
        assert out["image"].size[0] // 16 == 8, "blob47 packs eight wide"

    def test_a_missing_corner_bit_carves_that_corner(self):
        import numpy as np
        from bgate_core.autotile import E, N, NE, S, W
        both = N | E | S | W
        out = self._built([both | NE, both])        # with the corner, without
        a = np.asarray(out["image"])
        tx, ty = out["table"][both]                 # NE bit clear -> nibble
        assert a[ty*16, tx*16 + 15, 3] == 0, "the north-east corner is void"
        tx, ty = out["table"][both | NE]            # NE bit set -> unbroken
        assert a[ty*16, tx*16 + 15, 3] == 255

    def test_a_four_bit_set_is_not_notched_at_its_corners(self):
        """The bug this inference exists for: in a 4-bit set the corner bits are
        absent, not clear, so "the NE bit is unset" must not carve — it notched
        all four corners out of every tile, the fully open one included."""
        import numpy as np
        from bgate_core.autotile import E, N, S, W
        out = self._built(list(range(16)))
        a = np.asarray(out["image"])
        tx, ty = out["table"][N | E | S | W]
        assert (a[ty*16:(ty+1)*16, tx*16:(tx+1)*16, 3] == 255).all()

    def test_a_corner_bit_is_ignored_when_its_sides_are_not_floor(self):
        """canonical8's rule, and the tile has to agree with it: a north-east
        corner only describes a shape when north AND east are both filled."""
        import numpy as np
        from bgate_core.autotile import E, N, NE, canonical8
        assert canonical8(NE) == 0
        out = self._built([N | NE])                 # east is void anyway
        a = np.asarray(out["image"])
        tx, ty = out["table"][N | NE]
        assert (a[ty*16:(ty+1)*16, tx*16 + 15, 3] == 0).all(), \
            "the whole east band is void, corner or not"
        assert E  # keeps the import honest
