"""The rig sidecar: what a person says about a sheet, and what survives.

These tests are about TRUST. A sidecar that silently accepts a frame index past
the end of the sheet, or two contradictory anchors for the same slot, produces
a SpriteFrames that loads and a weapon that hangs in the wrong place — the
worst kind of failure, because everything reports success.
"""
from __future__ import annotations

import json

import pytest
from PIL import Image

from bgate_core import rigmap


GRID = {"cell_w": 32, "cell_h": 32, "cols": 4, "rows": 2}


def _rig(**over):
    base = {
        "grid": GRID,
        "fps": 12,
        "animations": [{"name": "walk", "frames": [0, 1, 2, 3]}],
        "labels": [{"slot": "main_hand", "frame": 0, "x": 10, "y": 12}],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def test_sidecar_never_ends_in_an_image_suffix(tmp_path):
    """Godot's importer would generate an .import for hero.png.rig.json."""
    p = rigmap.sidecar_path(tmp_path / "hero_sheet.png")
    assert p.name == "hero_sheet.rig.json"
    assert p.parent == tmp_path


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def test_slot_and_animation_names_fold_to_one_form():
    assert rigmap.slot_name(" Main Hand ") == "main_hand"
    assert rigmap.anim_name("Walk-Cycle") == "walk_cycle"
    with pytest.raises(rigmap.RigError):
        rigmap.slot_name("   ")


def test_grid_must_tile_the_sheet():
    with pytest.raises(rigmap.RigError) as exc:
        rigmap.normalise(_rig(), sheet_size=(100, 64))
    assert "does not tile" in str(exc.value)
    # The same grid against the size it was written for is fine.
    rigmap.normalise(_rig(), sheet_size=(128, 64))


def test_a_frame_past_the_last_cell_is_refused():
    with pytest.raises(rigmap.RigError) as exc:
        rigmap.normalise(_rig(animations=[{"name": "walk", "frames": [0, 99]}]))
    assert "past the last cell" in str(exc.value)


def test_two_anchors_for_one_slot_on_one_frame_is_a_contradiction():
    """Not "last one wins" — a stamper picking a winner silently is the bug."""
    with pytest.raises(rigmap.RigError):
        rigmap.normalise(_rig(labels=[
            {"slot": "main_hand", "frame": 0, "x": 10, "y": 12},
            {"slot": "main_hand", "frame": 0, "x": 20, "y": 4},
        ]))
    # Two DIFFERENT slots on one frame is normal.
    out = rigmap.normalise(_rig(labels=[
        {"slot": "main_hand", "frame": 0, "x": 10, "y": 12},
        {"slot": "off_hand", "frame": 0, "x": 20, "y": 4},
    ]))
    assert len(out["labels"]) == 2


def test_anchor_outside_the_cell_is_refused():
    with pytest.raises(rigmap.RigError):
        rigmap.normalise(_rig(labels=[
            {"slot": "head", "frame": 0, "x": 40, "y": 4}]))   # cell is 32 wide


def test_animation_needs_frames_and_a_unique_name():
    with pytest.raises(rigmap.RigError):
        rigmap.normalise(_rig(animations=[{"name": "walk", "frames": []}]))
    with pytest.raises(rigmap.RigError):
        rigmap.normalise(_rig(animations=[
            {"name": "walk", "frames": [0]}, {"name": "Walk", "frames": [1]}]))


def test_death_does_not_loop_by_default():
    out = rigmap.normalise(_rig(animations=[
        {"name": "death", "frames": [0, 1]}, {"name": "idle", "frames": [2]}]))
    assert out["animations"][0]["loop"] is False
    assert out["animations"][1]["loop"] is True


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
def test_missing_sidecar_is_an_empty_rig_not_an_error(tmp_path):
    data = rigmap.load(tmp_path / "nothing.png")
    assert data["labels"] == [] and data["grid"] is None


def test_corrupt_sidecar_raises_rather_than_discarding_labels(tmp_path):
    sheet = tmp_path / "hero.png"
    rigmap.sidecar_path(sheet).write_text("{not json", encoding="utf-8")
    with pytest.raises(rigmap.RigError):
        rigmap.load(sheet)


def test_save_round_trips_and_stamps_a_time(tmp_path):
    sheet = tmp_path / "hero.png"
    Image.new("RGBA", (128, 64)).save(sheet)
    saved = rigmap.save(sheet, _rig(), sheet_size=(128, 64))
    assert saved["updated_at"]
    on_disk = json.loads(rigmap.sidecar_path(sheet).read_text(encoding="utf-8"))
    assert on_disk["labels"][0]["slot"] == "main_hand"
    assert rigmap.load(sheet)["animations"][0]["frames"] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# What downstream reads
# ---------------------------------------------------------------------------
def test_anchors_map_to_gear_anchors_with_authored_provenance():
    from bgate_core.gear import MEASURED_SOURCES

    data = rigmap.normalise(_rig(labels=[
        {"slot": "main_hand", "frame": 5, "x": 9, "y": 3}]))
    anchors = rigmap.anchors_for(data, "Main Hand")
    assert len(anchors) == 1
    a = anchors[0]
    assert (a.row, a.col) == (1, 1)          # frame 5 of a 4-wide grid
    assert (a.x, a.y) == (9.0, 3.0)
    assert a.source == rigmap.AUTHORED
    # Authored COUNTS as measured now: rigmap has always documented it as
    # outranking every inferred source, but gear.py's ladder never learned it,
    # so Anchor.measured was False for the one anchor a person explicitly
    # placed. (This test used to pin that gap as if it were the contract.)
    assert a.source in MEASURED_SOURCES
    assert a.measured is True


def test_coverage_names_the_frames_a_slot_is_missing_from():
    data = rigmap.normalise(_rig())
    cov = rigmap.coverage(data)
    assert cov["played"] == [0, 1, 2, 3]
    assert cov["missing"]["main_hand"] == [1, 2, 3]


def test_coverage_ignores_padding_cells_nobody_plays():
    """Frames 4-7 exist but no animation names them; they are not a gap."""
    data = rigmap.normalise(_rig(
        animations=[{"name": "idle", "frames": [0]}]))
    assert rigmap.coverage(data)["missing"]["main_hand"] == []


# ---------------------------------------------------------------------------
# Godot export
# ---------------------------------------------------------------------------
def test_spriteframes_regions_follow_the_grid_not_the_queue():
    """The whole reason this writer exists: a multi-ROW sheet must export."""
    data = rigmap.normalise(_rig(animations=[
        {"name": "walk", "frames": [4, 5]}]))     # second row
    text = rigmap.spriteframes_text(data, "hero.png", "assets/sprites")
    assert 'path="res://assets/sprites/hero.png"' in text
    assert "region = Rect2(0, 32, 32, 32)" in text
    assert "region = Rect2(32, 32, 32, 32)" in text
    assert 'load_steps=4' in text                  # 2 atlases + texture + 1


def test_spriteframes_emits_one_atlas_per_referenced_frame():
    data = rigmap.normalise(_rig(animations=[
        {"name": "a", "frames": [0, 1, 0]}, {"name": "b", "frames": [1]}]))
    text = rigmap.spriteframes_text(data, "hero.png", "assets")
    assert text.count("[sub_resource") == 2        # frames 0 and 1, deduplicated


def test_spriteframes_is_byte_stable_for_an_unchanged_rig():
    data = rigmap.normalise(_rig())
    a = rigmap.spriteframes_text(data, "hero.png", "assets")
    b = rigmap.spriteframes_text(rigmap.normalise(_rig()), "hero.png", "assets")
    assert a == b


def test_spriteframes_refuses_a_rig_with_no_animations():
    data = rigmap.normalise(_rig(animations=[]))
    with pytest.raises(rigmap.RigError):
        rigmap.spriteframes_text(data, "hero.png", "assets")


def test_autoslice_refuses_a_cell_that_does_not_tile():
    assert rigmap.autoslice((128, 64), (32, 32)) == GRID
    with pytest.raises(rigmap.RigError):
        rigmap.autoslice((128, 64), (30, 32))


def test_rows_as_animations_covers_every_cell_once():
    anims = rigmap.rows_as_animations(GRID, ["idle", "walk"])
    assert [a["name"] for a in anims] == ["idle", "walk"]
    assert sorted(f for a in anims for f in a["frames"]) == list(range(8))


# ---------------------------------------------------------------------------
# The runtime offsets carrier
# ---------------------------------------------------------------------------
class TestOffsetsJson:
    def test_entries_follow_play_order_not_sheet_order(self):
        data = rigmap.normalise(_rig(
            animations=[{"name": "swing", "frames": [2, 1, 0]}],
            labels=[{"slot": "main_hand", "frame": 0, "x": 1, "y": 2},
                    {"slot": "main_hand", "frame": 2, "x": 5, "y": 6}]))
        got = rigmap.offsets_json(data, "main_hand")
        assert got["cell"] == [32, 32]
        # Play order is 2, 1, 0 — and frame 1 has no label, so null, not (0,0).
        assert got["animations"]["swing"] == [[5.0, 6.0], None, [1.0, 2.0]]

    def test_other_slots_do_not_leak_in(self):
        data = rigmap.normalise(_rig(
            labels=[{"slot": "main_hand", "frame": 0, "x": 1, "y": 2},
                    {"slot": "muzzle", "frame": 0, "x": 9, "y": 9}]))
        got = rigmap.offsets_json(data, "muzzle")
        assert got["slot"] == "muzzle"
        assert got["animations"]["walk"][0] == [9.0, 9.0]

    def test_a_rig_without_grid_reports_no_cell(self):
        data = rigmap.normalise({
            "animations": [], "labels": [], "fps": 10})
        assert rigmap.offsets_json(data)["cell"] is None
