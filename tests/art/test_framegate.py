"""framegate: the identity gate FIRES on a wrong/ghosted/duplicate/limby
frame and stays quiet on a clean one. Synthetic PIL images only - no model,
no network."""
from __future__ import annotations

from PIL import Image, ImageDraw

from bgate_core.art import framegate as fg


def _figure(size=(160, 240), *, body=(200, 40, 40), w=40, h=140, top=60,
           extra=None):
    """A simple humanoid blob: a body rectangle, feet near the bottom.
    `extra` is an optional (box, rgba) drawn as a second, disjoint blob."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = (size[0] - w) // 2
    d.rectangle([x0, top, x0 + w - 1, top + h - 1], fill=body + (255,))
    if extra:
        box, rgba = extra
        d.rectangle(box, fill=rgba)
    return img


def _anchor():
    return fg.anchor_band(_figure())


# ---------------------------------------------------------------------------
# Clean frames pass
# ---------------------------------------------------------------------------

def test_clean_frame_matching_the_anchor_passes():
    anchor = _anchor()
    frame = _figure()  # identical shape/palette to the anchor
    v = fg.frame_verdict(frame, anchor)
    assert v["ok"], v["failed"]


def test_clean_frame_with_a_raised_arm_still_passes():
    anchor = _anchor()
    # same body, plus a small connected notch near the shoulder - not a
    # separate component, so it must not trip second_head/component_count.
    frame = _figure(h=150, top=50)
    v = fg.frame_verdict(frame, anchor)
    assert v["ok"], v["failed"]


# ---------------------------------------------------------------------------
# ITEM 5 — ghosts and doubling FIRE
# ---------------------------------------------------------------------------

def test_translucent_second_blob_fires_translucency_ghost():
    anchor = _anchor()
    # A translucent (alpha ~150) cyan blob well clear of the main figure -
    # the sniper death-frame ghost, at a size that clears the threshold.
    frame = _figure(extra=([120, 20, 150, 45], (0, 220, 220, 150)))
    v = fg.frame_verdict(frame, anchor)
    assert not v["ok"]
    assert "translucency_ghost" in v["failed"]


def test_second_head_above_shoulder_line_fires():
    anchor = _anchor()
    # An OPAQUE extra blob near the top of the frame (within the top-35%
    # band), big enough and disjoint from the body - a doubled head.
    frame = _figure(extra=([20, 10, 55, 40], (0, 220, 220, 255)))
    v = fg.frame_verdict(frame, anchor)
    assert not v["ok"]
    assert "second_head" in v["failed"]


def test_detached_object_the_size_of_the_figure_fires_component_count():
    anchor = _anchor()
    # A second solid body-sized blob elsewhere in the frame (death_3: the
    # rifle standing upright alone, no figure at all - here modelled as a
    # same-size second component next to the real one).
    frame = _figure(extra=([5, 60, 45, 200], (10, 10, 10, 255)))
    v = fg.frame_verdict(frame, anchor)
    assert not v["ok"]
    assert "component_count" in v["failed"]


# ---------------------------------------------------------------------------
# Near-duplicate adjacent frames
# ---------------------------------------------------------------------------

def test_near_identical_adjacent_frames_fire_near_duplicate():
    anchor = _anchor()
    a = _figure()
    b = _figure()  # byte-identical
    v = fg.frame_verdict(a, anchor, adjacent=b, adjacent_name="walk/1")
    assert not v["ok"]
    assert "near_duplicate" in v["failed"]


def test_clearly_different_adjacent_frames_do_not_fire_near_duplicate():
    anchor = _anchor()
    a = _figure()
    b = _figure(w=70, h=90, top=140)  # a crouched, wide, low pose
    v = fg.frame_verdict(a, anchor, adjacent=b, adjacent_name="walk/1")
    assert "near_duplicate" not in v["failed"]


# ---------------------------------------------------------------------------
# Wrong character / wrong palette (the EXIT 67 collision itself)
# ---------------------------------------------------------------------------

def test_wrong_palette_frame_fires():
    anchor = _anchor()
    wrong = _figure(body=(20, 140, 230))  # a different character's blue
    v = fg.frame_verdict(wrong, anchor)
    assert not v["ok"]
    assert "wrong_palette" in v["failed"]


def test_shape_outlier_fires_on_a_wildly_different_silhouette():
    anchor = _anchor()  # 40w x 140h, ratio 3.5
    wide_short = _figure(w=140, h=30, top=180)
    v = fg.frame_verdict(wide_short, anchor)
    assert not v["ok"]
    assert "shape_outlier" in v["failed"]


# ---------------------------------------------------------------------------
# ITEM 6 — props that grow limbs
# ---------------------------------------------------------------------------

def _prop(size=(120, 120), *, w=60, h=40, extra=None):
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0, y0 = (size[0] - w) // 2, (size[1] - h) // 2
    d.rectangle([x0, y0, x0 + w - 1, y0 + h - 1], fill=(90, 90, 90, 255))
    if extra:
        for box in extra:
            d.rectangle(box, fill=(90, 90, 90, 255))
    return img


def test_prop_that_grows_limbs_fires_prop_grew_limbs():
    anchor = fg.anchor_band(_prop())          # a squat rectangle, aspect ~1.5
    # fire_1: the gas pump turret grows an "arm" and a "leg" - two thin
    # DISJOINT protrusions, which also drives the component count up.
    grew = _prop(extra=[[10, 55, 25, 100], [95, 10, 110, 55]])
    v = fg.frame_verdict(grew, anchor, subject_class="prop")
    assert not v["ok"]
    assert "prop_grew_limbs" in v["failed"]


def test_prop_holding_its_shape_does_not_fire_prop_grew_limbs():
    anchor = fg.anchor_band(_prop())
    same = _prop()
    v = fg.frame_verdict(same, anchor, subject_class="prop")
    assert "prop_grew_limbs" not in v["failed"]


# ---------------------------------------------------------------------------
# gate_sheet: per-frame verdicts over a whole set, cycle-aware
# ---------------------------------------------------------------------------

def test_gate_sheet_reports_per_frame_verdicts_and_overall_failure(tmp_path):
    anchor_path = tmp_path / "anchor.png"
    _figure().save(anchor_path)

    good_path = tmp_path / "walk_0.png"
    _figure().save(good_path)
    ghost_path = tmp_path / "walk_1.png"
    _figure(extra=([120, 20, 150, 45], (0, 220, 220, 150))).save(ghost_path)

    result = fg.gate_sheet(
        {"walk/0": str(good_path), "walk/1": str(ghost_path)},
        str(anchor_path), cycles={"walk": ["walk/0", "walk/1"]})
    assert not result["ok"]
    assert result["failed"] == ["walk/1"]
    assert result["frames"]["walk/0"]["ok"]
    assert not result["frames"]["walk/1"]["ok"]
    assert "translucency_ghost" in result["frames"]["walk/1"]["failed"]


def test_is_fall_risk_matches_known_doubling_triggers():
    assert fg.is_fall_risk("death/2", "collapsing backward")
    assert fg.is_fall_risk("relocate/0", "falling into position")
    assert not fg.is_fall_risk("walk/0", "striding forward")


# ---------------------------------------------------------------------------
# Provenance (ITEMS 2/3): stale-anchor frames are named, not trusted
# ---------------------------------------------------------------------------

def test_write_and_read_provenance_roundtrip(tmp_path):
    p = tmp_path / "pose_walk_0.png"
    _figure().save(p)
    doc = fg.write_provenance(p, anchor_hash="abc123", prompt="walk pose")
    assert doc["anchor_hash"] == "abc123"
    back = fg.read_provenance(p)
    assert back["anchor_hash"] == "abc123"
    assert back["prompt"] == "walk pose"


def test_missing_provenance_reads_as_none_not_an_error(tmp_path):
    p = tmp_path / "unlabelled.png"
    _figure().save(p)
    assert fg.read_provenance(p) is None


def test_stale_frames_fires_on_missing_and_mismatched_provenance(tmp_path):
    fresh = tmp_path / "pose_08_walk_0.png"
    _figure().save(fresh)
    fg.write_provenance(fresh, anchor_hash="hash-current")

    stale = tmp_path / "pose_05_walk_1.png"
    _figure().save(stale)
    fg.write_provenance(stale, anchor_hash="hash-old-anchor")

    unlabelled = tmp_path / "pose_unlabelled.png"
    _figure().save(unlabelled)

    result = fg.stale_frames(
        {"walk/0": str(fresh), "walk/1": str(stale), "walk/2": str(unlabelled)},
        "hash-current")
    names = {r["name"] for r in result}
    assert names == {"walk/1", "walk/2"}


def test_file_hash_is_stable_and_content_addressed(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    _figure().save(a)
    _figure().save(b)
    assert fg.file_hash(a) == fg.file_hash(b)  # identical pixels -> identical hash
    diff = tmp_path / "c.png"
    _figure(body=(10, 200, 10)).save(diff)
    assert fg.file_hash(a) != fg.file_hash(diff)


# ---------------------------------------------------------------------------
# ITEM 9 — conform a stamp to the pinned palette, flag ink-weight mismatch
# ---------------------------------------------------------------------------

def test_conform_stamp_snaps_colours_to_the_anchor_palette():
    stamp = _figure(body=(198, 41, 43))  # close to, but not exactly, the anchor red
    anchor_palette = [(200, 40, 40)]
    out = fg.conform_stamp(stamp, anchor_palette, anchor_stroke=fg.median_stroke_width(stamp))
    colours = set(fg.palette_of(out["image"]))
    assert colours <= {(200, 40, 40)}


def test_conform_stamp_flags_a_thin_ink_line_against_a_thick_anchor():
    thick_anchor = _figure(w=60, h=140)
    anchor_stroke = fg.median_stroke_width(thick_anchor)
    thin_line = Image.new("RGBA", (160, 240), (0, 0, 0, 0))
    d = ImageDraw.Draw(thin_line)
    d.line([(20, 20), (20, 220)], fill=(200, 40, 40, 255), width=1)
    out = fg.conform_stamp(thin_line, [(200, 40, 40)], anchor_stroke=anchor_stroke)
    assert out["line_weight_flag"] is True


def test_conform_stamp_does_not_flag_matching_stroke_width():
    a = _figure()
    b = _figure()
    stroke = fg.median_stroke_width(a)
    out = fg.conform_stamp(b, [(200, 40, 40)], anchor_stroke=stroke)
    assert out["line_weight_flag"] is False
