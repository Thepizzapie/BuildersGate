"""Row and sheet auditing — spritekit section 6.

Every case here is one of the faults that reached a human as a screenshot with
red lines drawn on it by hand, rebuilt as a synthetic so the measurement that
should have caught it is pinned. A test that passes on a clean row and fails on
the broken one is the whole contract: an audit that fires on everything gets
switched off, and an audit that fires on nothing was never running.
"""
from __future__ import annotations

import pytest

from bgate_core.art import spritekit as kit

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402


BACK = (12, 12, 16, 255)
BODY = (190, 170, 120, 255)
HEAD = (170, 175, 180, 255)
DARK = (40, 40, 48, 255)


def figure(cell_w: int, cell_h: int, *, scale: float = 1.0, foot: int = 0,
           facing: int = 1, label: bool = False) -> Image.Image:
    """One figure on a flat backdrop: body block, head block, dark head detail.

    `foot` lifts the feet off the cell floor (the ground-line fault), `scale`
    grows the whole figure (the draw-size fault) and `facing` puts the head's
    dark detail on one side or the other (the yaw fault).
    """
    img = Image.new("RGBA", (cell_w, cell_h), BACK)
    draw = ImageDraw.Draw(img)
    h = int(cell_h * 0.62 * scale)
    w = int(cell_w * 0.34 * scale)
    cx = cell_w // 2
    bottom = cell_h - 6 - foot
    body_top = bottom - h
    draw.rectangle([cx - w // 2, body_top, cx + w // 2, bottom], fill=BODY)
    head_h = int(h * 0.34)
    hw = int(w * 0.9)
    draw.rectangle([cx - hw // 2, body_top - head_h, cx + hw // 2, body_top],
                   fill=HEAD)
    # The facing cue: a dark recess on one side of the head.
    side = cx + facing * (hw // 4)
    draw.rectangle([side - hw // 8, body_top - head_h + head_h // 4,
                    side + hw // 8, body_top - head_h // 4], fill=DARK)
    if label:
        draw.rectangle([4, 4, 30, 12], fill=(255, 255, 255, 255))
    return img


def row(tmp_path, specs, name="row.png", *, bands=1):
    """Paste `specs` (list of kwargs, or list-of-lists for a grid) into a sheet."""
    if bands == 1:
        specs = [specs]
    cw, ch = 120, 200
    sheet = Image.new("RGBA", (cw * len(specs[0]), ch * len(specs)), BACK)
    for r, band in enumerate(specs):
        for c, spec in enumerate(band):
            sheet.paste(figure(cw, ch, **spec), (c * cw, r * ch))
    path = tmp_path / name
    sheet.save(path)
    return str(path)


class TestCleanRow:
    """The audit has to be silent on art that is right, or nobody leaves it on."""

    def test_a_correct_row_reports_nothing(self, tmp_path):
        path = row(tmp_path, [{}, {}, {}, {}])
        got = kit.row_report(path, 4)
        assert got["flagged"] == [], got["bands"][0]["findings"]

    def test_a_head_bob_is_not_a_finding(self, tmp_path):
        # What a walk IS: the head rises and falls, the feet stay planted.
        path = row(tmp_path, [{}, {"scale": 1.03}, {}, {"scale": 1.03}])
        got = kit.row_report(path, 4)
        assert "foot_drift" not in _kinds(got["bands"][0])


class TestGroundLine:
    def test_feet_off_the_line_are_caught(self, tmp_path):
        path = row(tmp_path, [{"foot": 30}, {"foot": 28}, {}, {}])
        band = kit.row_report(path, 4)["bands"][0]
        assert "foot_drift" in _kinds(band)
        assert band["foot_drift"] > kit.FOOT_DRIFT_MAX

    def test_the_fault_is_measured_in_the_rows_own_coordinates(self, tmp_path):
        # Row 2 of a sheet is 200px lower down the canvas than row 1 and is not
        # thereby broken. A single ground line for the whole image would flag it.
        path = row(tmp_path, [[{}, {}, {}, {}], [{}, {}, {}, {}]], bands=2)
        got = kit.row_report(path, 4, 2)
        assert got["flagged"] == [], got


class TestDrawSize:
    def test_jitter_is_drift_but_not_a_ramp(self, tmp_path):
        path = row(tmp_path, [{}, {"scale": 1.25}, {}, {"scale": 0.95}])
        band = kit.row_report(path, 4)["bands"][0]
        assert "size_drift" in _kinds(band)
        assert "size_ramp" not in _kinds(band)

    def test_a_monotonic_ramp_names_the_compounding(self, tmp_path):
        # The screenshot: the figure grows steadily left to right because each
        # one was drawn from the canvas so far.
        path = row(tmp_path, [{"scale": s} for s in (0.85, 0.95, 1.1, 1.3)])
        band = kit.row_report(path, 4)["bands"][0]
        assert "size_ramp" in _kinds(band)
        ramp = [f for f in band["findings"] if f["kind"] == "size_ramp"][0]
        assert ramp["value"] >= kit.TREND_RHO
        assert "image_sprites" in ramp["note"]


class TestFacing:
    def test_one_figure_yawed_the_other_way(self, tmp_path):
        path = row(tmp_path, [{"facing": -1}, {}, {}, {}])
        band = kit.row_report(path, 4)["bands"][0]
        assert "facing_flip" in _kinds(band)
        flip = [f for f in band["findings"] if f["kind"] == "facing_flip"][0]
        assert flip["frames"] == ["0"]

    def test_a_consistent_row_does_not_flip(self, tmp_path):
        path = row(tmp_path, [{"facing": -1}] * 4)
        assert "facing_flip" not in _kinds(kit.row_report(path, 4)["bands"][0])

    def test_skew_sign_tracks_the_side_the_detail_is_on(self, tmp_path):
        left = kit.cell_stats(figure(120, 200, facing=-1))["skew"]
        right = kit.cell_stats(figure(120, 200, facing=1))["skew"]
        assert left < -kit.FACING_SKEW_MIN < kit.FACING_SKEW_MIN < right


class TestStrayInk:
    def test_a_label_on_the_canvas_is_found(self, tmp_path):
        path = row(tmp_path, [{}, {}, {}, {"label": True}])
        band = kit.row_report(path, 4)["bands"][0]
        assert "stray_ink" in _kinds(band)
        assert [f for f in band["findings"]
                if f["kind"] == "stray_ink"][0]["frames"] == ["3"]


class TestEmptyCells:
    def test_a_column_with_no_figure(self, tmp_path):
        cw, ch = 120, 200
        sheet = Image.new("RGBA", (cw * 4, ch), BACK)
        for c in range(3):
            sheet.paste(figure(cw, ch), (c * cw, 0))
        path = tmp_path / "short.png"
        sheet.save(path)
        band = kit.row_report(str(path), 4)["bands"][0]
        assert "empty_cell" in _kinds(band)


class TestAcrossRows:
    def test_rows_that_disagree_on_size(self, tmp_path):
        path = row(tmp_path, [[{}] * 4, [{"scale": 1.3}] * 4], bands=2)
        got = kit.row_report(path, 4, 2)
        assert "sheet_size_drift" in {f["kind"] for f in got["findings"]}

    def test_a_row_carrying_a_colour_the_sheet_does_not(self, tmp_path):
        # The sheet where two rows grow glowing eyes and the others do not.
        cw, ch = 120, 200
        sheet = Image.new("RGBA", (cw * 4, ch * 4), BACK)
        for r in range(4):
            for c in range(4):
                cell = figure(cw, ch)
                if r == 1:
                    ImageDraw.Draw(cell).rectangle(
                        [cw // 2 - 24, 34, cw // 2 + 24, 62],
                        fill=(0, 220, 255, 255))
                sheet.paste(cell, (c * cw, r * ch))
        path = tmp_path / "eyes.png"
        sheet.save(path)
        got = kit.row_report(str(path), 4, 4)
        finding = [f for f in got["findings"] if f["kind"] == "band_palette"]
        assert finding and finding[0]["frames"] == ["row1"], got["findings"]


class TestUnkeyedInput:
    def test_a_raw_generation_measures_without_alpha(self, tmp_path):
        # The moment this is worth running is before anything has been keyed —
        # while a re-roll is one cheap call, not a re-run of the whole assembly.
        path = row(tmp_path, [{"foot": 30}, {}, {}, {}])
        assert Image.open(path).convert("RGBA").getchannel("A").getextrema() == (255, 255)
        band = kit.row_report(path, 4)["bands"][0]
        assert "foot_drift" in _kinds(band)

    def test_a_keyed_row_measures_the_same_way(self, tmp_path):
        path = row(tmp_path, [{"foot": 30}, {}, {}, {}], name="keyed.png")
        keyed = Image.open(path).convert("RGBA")
        pixels = keyed.load()
        for y in range(keyed.height):
            for x in range(keyed.width):
                if pixels[x, y][:3] == BACK[:3]:
                    pixels[x, y] = (0, 0, 0, 0)
        keyed.save(tmp_path / "keyed2.png")
        band = kit.row_report(str(tmp_path / "keyed2.png"), 4)["bands"][0]
        assert "foot_drift" in _kinds(band)


class TestGuides:
    def test_it_writes_an_image_with_a_line_per_row(self, tmp_path):
        path = row(tmp_path, [[{"foot": 30}, {}, {}, {}], [{}] * 4], bands=2)
        out = tmp_path / "guides.png"
        got = kit.draw_guides(path, 4, out, 2)
        assert got["ok"] and out.exists()
        assert got["guides"] == 8
        with Image.open(out) as drawn:
            assert drawn.size == Image.open(path).size
            # The guides are red and the synthetic has no red in it, so any red
            # pixel at all is proof the overlay landed.
            assert any(p[0] > 200 and p[1] < 80 and p[2] < 80
                       for p in drawn.convert("RGB").getdata())

    def test_an_empty_sheet_does_not_raise(self, tmp_path):
        blank = Image.new("RGBA", (400, 200), BACK)
        path = tmp_path / "blank.png"
        blank.save(path)
        got = kit.draw_guides(str(path), 4, tmp_path / "g.png")
        assert got["ok"] and got["guides"] == 0


class TestTrend:
    def test_monotonic_reads_one(self):
        assert kit._trend([1, 2, 3, 4]) == 1.0
        assert kit._trend([4, 3, 2, 1]) == -1.0

    def test_noise_does_not(self):
        assert abs(kit._trend([2, 1, 4, 3])) < kit.TREND_RHO

    def test_too_few_to_say(self):
        assert kit._trend([1, 9]) == 0.0


def _kinds(band: dict) -> set[str]:
    return {f["kind"] for f in band["findings"]}
