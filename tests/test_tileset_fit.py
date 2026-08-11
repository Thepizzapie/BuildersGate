"""Which wall layouts an atlas source can actually draw.

The level workflow template used to ship a hardcoded
``res://assets/tiles/main.tres`` and ``wall_layout="blob47"``. No project has
ever had that file, and the owner's project has 55 atlas sources of which not
one holds the 47 tiles blob47 addresses -- so the one card whose entire promise
is "this generates a level" failed on its generate node in every project it was
ever opened in.

``level_generate`` refuses that correctly and names the missing coordinates.
These tests cover the arithmetic that now runs BEFORE the template is built, so
the card offers a layout the tileset can satisfy instead of one it cannot.

A tile COUNT cannot answer this, which is the whole point of the cases below.
"""
from __future__ import annotations

from bgate_ui.routes.scenewire import _source_fit


def _block(cols: int, rows: int, ox: int = 0, oy: int = 0) -> list:
    """A solid cols x rows run of tiles, the way a normal sheet is authored."""
    return [(ox + x, oy + y) for y in range(rows) for x in range(cols)]


def test_a_36_tile_block_draws_grid16_but_not_blob47():
    # The owner's office tileset: 36 tiles, 18 wide. Enough for the 16-mask
    # layout, nowhere near the 47-mask one, and a count-based check that only
    # asked "more than 16?" would happily offer blob47 and fail at generate.
    fit = _source_fit(40, _block(18, 2))
    assert fit["layouts"] == ["grid16", "solid"]
    assert fit["columns"] == 18
    assert (fit["atlas_x"], fit["atlas_y"]) == (0, 0)


def test_a_full_sheet_offers_the_richest_layout_first():
    # 48 tiles in an 8-wide run covers all 47 masks. Richest first, because a
    # caller taking [0] wants the most detailed wall it can draw, not whichever
    # name happened to sort first.
    fit = _source_fit(0, _block(8, 6))
    assert fit["layouts"] == ["blob47", "grid16", "solid"]


def test_a_narrow_sheet_fits_only_because_columns_travels_with_it():
    # 50 tiles authored 5 wide. blob47 fits -- but ONLY when the walk is told
    # the run is 5 columns, which is why `columns` is part of the answer rather
    # than something the caller assumes. At the argument default of 8 this same
    # source runs off the right edge on the very first row and draws nothing
    # there, silently. The verdict and the width are one fact, not two.
    tiles = _block(5, 10)
    assert len(tiles) == 50
    fit = _source_fit(3, tiles)
    assert "blob47" in fit["layouts"]
    assert fit["columns"] == 5

    have = set(tiles)
    walk_at_8 = [(i % 8, i // 8) for i in range(47)]
    assert not all(c in have for c in walk_at_8), (
        "the default width must be what breaks this sheet, or the test proves "
        "nothing about why columns is reported")


def test_a_gap_in_the_run_disqualifies_the_layout():
    # Godot's .tres lists the tiles it defines and says nothing about the ones
    # it does not. A hole inside the run draws nothing, silently, in exactly the
    # place the wall shape is most complicated.
    tiles = [t for t in _block(8, 6) if t != (3, 2)]
    fit = _source_fit(1, tiles)
    assert "blob47" not in fit["layouts"]
    assert "grid16" in fit["layouts"]  # the hole sits past mask 15


def test_the_origin_is_the_sources_own_corner_not_zero():
    # A sheet packed into the middle of a texture starts where it starts. The
    # template passes these straight to wall_atlas_x/_y, so an assumed 0 aims
    # the whole layout at empty atlas space.
    fit = _source_fit(7, _block(8, 6, ox=4, oy=9))
    assert (fit["atlas_x"], fit["atlas_y"]) == (4, 9)
    assert "blob47" in fit["layouts"]


def test_a_single_tile_source_can_only_be_solid():
    fit = _source_fit(2, [(0, 0)])
    assert fit["layouts"] == ["solid"]


def test_an_empty_source_offers_nothing_rather_than_guessing():
    fit = _source_fit(9, [])
    assert fit["layouts"] == []
    assert fit["tiles"] == 0
