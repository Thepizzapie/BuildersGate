"""sprite_fit / sprite_family_check: one standing height per character, on
every sheet, and a family that disagrees is named."""
from __future__ import annotations

from PIL import Image, ImageDraw

from bgate_core.art import spritefit


def _sheet(path, cell, figures, colour=(200, 40, 40)):
    """A strip of cells, each holding a rectangle `h` tall and `w` wide with
    its feet on `feet` (from the top) - the figure as a box."""
    cw, ch = cell
    img = Image.new("RGBA", (cw * len(figures), ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, f in enumerate(figures):
        w, h, feet = f["w"], f["h"], f.get("feet", ch - 3)
        x0 = i * cw + (cw - w) // 2
        d.rectangle([x0, feet - h, x0 + w - 1, feet - 1], fill=colour + (255,))
    img.save(path)
    return path


def test_fit_scales_every_frame_by_the_anchors_factor_and_keeps_offsets(tmp_path):
    src = _sheet(tmp_path / "chuco_battle.png", (64, 96), [
        {"w": 20, "h": 50},                       # idle, 50px: the anchor
        {"w": 30, "h": 60},                       # attack, arm up: taller
        {"w": 20, "h": 40, "feet": 96 - 3 - 10},  # a jump: feet 10px off the row
    ])
    r = spritefit.fit_sheet(src, tmp_path / "out.png", cell=(64, 96), standing_px=90)
    assert r["ok"] and r["scale"] == 1.8 and r["upscaled"] is True
    assert r["overflow"] == [1]                              # 60*1.8=108 > the 96 cell
    hs = [f["height_after"] for f in r["report"]]
    assert hs[0] == 90 and hs[1] == 96
    assert r["report"][0]["feet_row"] == 93
    # the jump keeps its air: 10px * 1.8 = 18px above the feet row
    assert r["report"][2]["feet_row"] == 93 - 18
    m = spritefit.measure(tmp_path / "out.png", (64, 96))
    assert m["frames"][0]["height"] == 90


def test_fit_reports_an_overflowing_frame_instead_of_cropping_it(tmp_path):
    src = _sheet(tmp_path / "s.png", (64, 96), [{"w": 20, "h": 50}, {"w": 60, "h": 60}])
    r = spritefit.fit_sheet(src, tmp_path / "o.png", cell=(64, 96), standing_px=90)
    assert r["overflow"] == [1]
    m = spritefit.measure(tmp_path / "o.png", (64, 96))
    assert m["frames"][1]["width"] <= 64 and m["frames"][1]["height"] <= 96
    # nothing was cut: the box is whole (its aspect survives the shrink)
    f = m["frames"][1]
    assert abs(f["width"] / f["height"] - 1.0) < 0.1


def test_fit_can_widen_the_cell_and_snap_to_a_palette(tmp_path):
    src = _sheet(tmp_path / "s.png", (64, 96), [{"w": 20, "h": 50}], colour=(201, 39, 41))
    r = spritefit.fit_sheet(src, tmp_path / "o.png", cell=(96, 96), src_cell=(64, 96),
                            standing_px=90, palette=[(200, 40, 40), (0, 0, 0)])
    assert r["cell"] == [96, 96] and r["src_cell"] == [64, 96]
    with Image.open(tmp_path / "o.png") as im:
        assert im.size == (96, 96)
        colours = {px[:3] for px in im.convert("RGBA").getdata() if px[3]}
    assert colours == {(200, 40, 40)}


def test_fit_refuses_a_big_upscale_and_an_empty_anchor(tmp_path):
    import pytest
    src = _sheet(tmp_path / "s.png", (64, 96), [{"w": 10, "h": 20}])
    with pytest.raises(spritefit.FitError, match="upscale"):
        spritefit.fit_sheet(src, tmp_path / "o.png", cell=(64, 96), standing_px=90)
    empty = Image.new("RGBA", (64, 96), (0, 0, 0, 0))
    empty.save(tmp_path / "e.png")
    with pytest.raises(spritefit.FitError, match="anchor"):
        spritefit.fit_sheet(tmp_path / "e.png", tmp_path / "o.png", cell=(64, 96), standing_px=50)


def test_family_check_names_the_member_that_is_a_different_size_or_colour(tmp_path):
    a = _sheet(tmp_path / "chuco_battle.png", (96, 96), [{"w": 30, "h": 90}])
    b = _sheet(tmp_path / "chuco_ow.png", (32, 32), [{"w": 12, "h": 28}])       # not the same unit
    c = _sheet(tmp_path / "chuco_cast.png", (96, 96), [{"w": 30, "h": 64}])      # 30% short
    d = _sheet(tmp_path / "chuco_alt.png", (96, 96), [{"w": 30, "h": 90}], colour=(20, 200, 60))
    r = spritefit.family_check([
        {"path": a, "cell": (96, 96), "label": "battle"},
        {"path": c, "cell": (96, 96), "label": "cast"},
        {"path": d, "cell": (96, 96), "label": "alt"},
    ], standing_px=90)
    by = {(f["code"], f["sheet"]) for f in r["findings"]}
    assert ("height_mismatch", "cast") in by
    assert ("height_mismatch", "battle") not in by
    assert ("palette_drift", "alt") in by
    assert r["ok"] is False
    # a family with no contract holds its members to their own median
    r2 = spritefit.family_check([{"path": a, "cell": (96, 96)}, {"path": c, "cell": (96, 96)}])
    assert r2["target_height"] in (64, 90)


def test_fit_to_band_shrinks_an_oversized_enemy_in_place(tmp_path):
    p = _sheet(tmp_path / "boss.png", (400, 400), [{"w": 200, "h": 320, "feet": 380}])
    r = spritefit.fit_to_band(p, max_height=192)
    assert r["changed"] and r["height_after"] == 192 and r["height_before"] == 320
    assert spritefit.fit_to_band(p, max_height=192)["changed"] is False
