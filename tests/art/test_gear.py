"""Worn-gear placeholders — grid, anchors, stamping, coverage.

The failure this module exists to prevent is a weapon that VANISHES for most of
a moveset, and the failure it could itself introduce is a placeholder stamped a
few pixels off the hand (or worse, a guess reported as a measurement). So the
tests are built on synthetic sheets whose hand positions are known exactly:
every anchor assertion is against a number the fixture chose, not against art.

The grid cases are drawn from real layouts. A dense 4x2 attack sheet is easy;
the one that breaks naive detection is a "block" sheet — two frames stacked
VERTICALLY on a wide canvas, three of four columns entirely empty — where band
counting says one column and the truth is four.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from PIL import Image

from bgate_core.art import gear

CELL_W, CELL_H = 96, 80
COLS, ROWS = 4, 2

# The known hand positions the fixtures draw to, cell-local. Row 1 mirrors row 0
# the way a facing-flipped view does, so side-bias handling gets exercised.
HANDS = {
    (0, 0): (74, 46), (0, 1): (70, 30), (0, 2): (78, 28), (0, 3): (72, 44),
    (1, 0): (22, 46), (1, 1): (26, 30), (1, 2): (18, 28), (1, 3): (24, 44),
}


def _blank(cols=COLS, rows=ROWS) -> Image.Image:
    return Image.new("RGBA", (CELL_W * cols, CELL_H * rows), (0, 0, 0, 0))


def _gear_sheet(reach: int, color=(200, 40, 40, 255)) -> Image.Image:
    """An aligned sheet: only the weapon, drawn AT the hand in every cell.

    `reach` is how far the blade runs past the grip — the axis along which two
    weapons differ. Their masks overlap only at the grip, which is exactly the
    property measure_anchors leans on.
    """
    img = _blank()
    px = img.load()
    for (row, col), (hx, hy) in HANDS.items():
        ox, oy = col * CELL_W, row * CELL_H
        step = 1 if hx > CELL_W / 2 else -1
        for i in range(-1, reach):                     # blade, away from centre
            for dy in (-1, 0, 1):
                cx, cy = hx + i * step, hy + dy
                if not (0 <= cx < CELL_W and 0 <= cy < CELL_H):
                    continue           # aligned sheets never spill their cell
                px[ox + cx, oy + cy] = color
    return img


def _body_sheet(hand_rgb=(250, 190, 120)) -> Image.Image:
    """A body: torso block, a head blob up top in the SAME colour as the hands
    (the real character's face is skin too — the head is the trap the inference
    has to step around), and one hand blob per frame at the known position."""
    img = _blank()
    d = Image.new("RGBA", img.size, (0, 0, 0, 0))
    px = img.load()
    for (row, col), (hx, hy) in HANDS.items():
        ox, oy = col * CELL_W, row * CELL_H
        for y in range(20, 66):                        # torso
            for x in range(40, 58):
                px[ox + x, oy + y] = (60, 70, 110, 255)
        for y in range(6, 20):                         # head, top-centre
            for x in range(42, 56):
                px[ox + x, oy + y] = (*hand_rgb, 255)
        for y in range(hy - 4, hy + 5):                # hand at the known spot
            for x in range(hx - 4, hx + 5):
                px[ox + x, oy + y] = (*hand_rgb, 255)
        # the arm, joining torso to hand, so the hand is not an island
        step = 1 if hx > CELL_W / 2 else -1
        x = 49
        while (x < hx) if step > 0 else (x > hx):
            px[ox + x, oy + hy] = (60, 70, 110, 255)
            x += step
    del d
    return img


@pytest.fixture
def body():
    return _body_sheet()


@pytest.fixture
def gear_sheets():
    # Three "weapons" of different reach: they agree only at the grip.
    return [_gear_sheet(6), _gear_sheet(18), _gear_sheet(30)]


# ---------------------------------------------------------------------------
class TestGridDetection:
    def test_dense_horizontal_sheet(self, body):
        grid = gear.detect_grid(body)
        assert (grid.cell_w, grid.cell_h) == (CELL_W, CELL_H)
        assert (grid.cols, grid.rows) == (COLS, ROWS)

    def test_sparse_vertical_block_layout(self):
        """A 1-frame action: column 0 only, two view rows. Counting content
        bands would say cols=1; the true grid is still 4 columns wide."""
        img = _blank()
        px = img.load()
        for row in range(ROWS):
            for y in range(10, 70):
                for x in range(30, 66):
                    px[x, row * CELL_H + y] = (90, 90, 120, 255)
        grid = gear.detect_grid(img)
        assert (grid.cell_w, grid.cell_h) == (CELL_W, CELL_H)
        assert (grid.cols, grid.rows) == (COLS, ROWS)

    def test_four_view_rows(self, body):
        """Throw sheets carry four view rows on the same cell size."""
        tall = Image.new("RGBA", (CELL_W * COLS, CELL_H * 4), (0, 0, 0, 0))
        for r in range(4):
            tall.paste(body.crop((0, 0, CELL_W * COLS, CELL_H)), (0, r * CELL_H))
        grid = gear.detect_grid(tall)
        assert (grid.cell_w, grid.cell_h, grid.cols, grid.rows) == (CELL_W, CELL_H, COLS, 4)

    def test_explicit_cell_overrides_detection(self, body):
        grid = gear.detect_grid(body, cell=(48, 40))
        assert (grid.cols, grid.rows) == (8, 4)

    def test_explicit_cell_must_tile(self, body):
        with pytest.raises(ValueError):
            gear.detect_grid(body, cell=(50, 40))

    def test_blank_sheet_is_one_cell(self):
        grid = gear.detect_grid(_blank(1, 1))
        assert (grid.cols, grid.rows) == (1, 1)


# ---------------------------------------------------------------------------
class TestAnchorExtraction:
    def test_intersection_lands_on_the_known_hand(self, gear_sheets):
        grid = gear.Grid(CELL_W, CELL_H, COLS, ROWS)
        anchors = gear.measure_anchors(gear_sheets, grid)
        assert len(anchors) == len(HANDS)
        for a in anchors:
            hx, hy = HANDS[(a.row, a.col)]
            assert abs(a.x - hx) <= 3 and abs(a.y - hy) <= 2, (a, hx, hy)
            assert a.source == gear.MEASURED
            assert a.measured is True
            assert a.support == 3

    def test_single_sheet_is_flagged_weaker(self, gear_sheets):
        grid = gear.Grid(CELL_W, CELL_H, COLS, ROWS)
        anchors = gear.measure_anchors(gear_sheets[:1], grid)
        # One sheet cannot isolate a grip — it measures the whole weapon, and
        # must not claim otherwise.
        assert {a.source for a in anchors} == {gear.MEASURED_SINGLE}
        assert all(a.measured for a in anchors)

    def test_empty_cells_get_no_anchor(self):
        grid = gear.Grid(CELL_W, CELL_H, COLS, ROWS)
        sheet = _blank()
        px = sheet.load()
        for y in range(40, 46):
            for x in range(70, 78):
                px[x, y] = (255, 0, 0, 255)
        anchors = gear.measure_anchors([sheet], grid)
        assert [(a.row, a.col) for a in anchors] == [(0, 0)]

    def test_side_bias_is_read_off_the_art(self, gear_sheets):
        grid = gear.Grid(CELL_W, CELL_H, COLS, ROWS)
        bias = gear.anchor_side_bias(gear.measure_anchors(gear_sheets, grid), grid)
        assert bias == {0: 1, 1: -1}


class TestInference:
    def test_inferred_anchors_are_labelled_and_close(self, body, gear_sheets):
        grid = gear.Grid(CELL_W, CELL_H, COLS, ROWS)
        measured = gear.measure_anchors(gear_sheets, grid)
        palette = gear.pool_hand_palette([(body, measured)], grid)
        bias = gear.anchor_side_bias(measured, grid)
        inferred = gear.infer_anchors(body, grid, palette=palette, side_bias=bias)
        assert len(inferred) == len(HANDS)
        for a in inferred:
            assert not a.measured
            assert a.source in (gear.INFERRED_HAND, gear.INFERRED_SILHOUETTE)
            hx, hy = HANDS[(a.row, a.col)]
            assert abs(a.x - hx) <= 6 and abs(a.y - hy) <= 6, (a, hx, hy)

    def test_validation_reports_error_in_pixels(self, body, gear_sheets):
        grid = gear.Grid(CELL_W, CELL_H, COLS, ROWS)
        measured = gear.measure_anchors(gear_sheets, grid)
        palette = gear.pool_hand_palette([(body, measured)], grid)
        bias = gear.anchor_side_bias(measured, grid)
        stats = gear.validate_inference(body, grid, measured,
                                        palette=palette, side_bias=bias)
        assert stats["n"] == len(HANDS)
        assert stats["cell"] == [CELL_W, CELL_H]
        assert stats["median_px"] is not None and stats["median_px"] < 8


class TestNaming:
    def test_matches_the_format_string_the_game_loads(self):
        # "res://assets/items/main_hand/animations/%s_%s.png" % [weapon, action]
        assert gear.layer_sheet_name("keyboard_blade", "main_hand_swing") == \
            "keyboard_blade_main_hand_swing.png"
        assert gear.throwable_sheet_name("throw_one_hand") == \
            "placeholder_throw_one_hand.png"

    def test_dual_wield_drives_two_layers(self):
        assert gear.layer_actions_for("dual_wield_swing") == \
            ("dual_wield_main", "dual_wield_off")
        assert gear.layer_actions_for("punch") == ("punch",)
        assert gear.body_action_for("dual_wield_off") == "dual_wield_swing"

    def test_body_actions_read_off_disk(self, tmp_path: Path):
        for name in ("pm_paladin_idle.png", "pm_paladin_main_hand_swing.png",
                     "other_walk.png"):
            _blank(1, 1).save(tmp_path / name)
        assert gear.body_actions(tmp_path, "pm_paladin") == \
            ["idle", "main_hand_swing"]


class TestMarker:
    def test_plain_png_is_not_a_placeholder(self, tmp_path: Path):
        # Real art NAMED placeholder_* still counts as real: the marker is in
        # the file, not the filename.
        p = tmp_path / "placeholder_throw_one_hand.png"
        _blank(1, 1).save(p)
        assert not gear.is_placeholder(p)


