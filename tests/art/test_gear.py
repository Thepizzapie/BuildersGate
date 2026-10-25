"""Worn-gear placeholders: grid detection, stamping, palette conform.

The failure this module exists to prevent is a weapon that VANISHES for most of
a moveset, and the failure it could itself introduce is a placeholder stamped a
few pixels off the hand. So the tests are built on synthetic sheets whose hand
positions are known exactly, not on art.

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
# the way a facing-flipped view does.
HANDS = {
    (0, 0): (74, 46), (0, 1): (70, 30), (0, 2): (78, 28), (0, 3): (72, 44),
    (1, 0): (22, 46), (1, 1): (26, 30), (1, 2): (18, 28), (1, 3): (24, 44),
}


def _blank(cols=COLS, rows=ROWS) -> Image.Image:
    return Image.new("RGBA", (CELL_W * cols, CELL_H * rows), (0, 0, 0, 0))


def _body_sheet(hand_rgb=(250, 190, 120)) -> Image.Image:
    """A body: torso block, a head blob up top in the same colour as the hands,
    and one hand blob per frame at the known position."""
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


class TestStampGenerated:
    """ITEM 9 — a GENERATED weapon composited at the anchor is conformed to
    the pinned palette first, and an ink-weight mismatch is reported."""

    def _weapon(self, color=(210, 90, 20, 255), width=6):
        img = Image.new("RGBA", (30, 30), (0, 0, 0, 0))
        for y in range(30):
            for x in range(width):
                img.putpixel((10 + x, y), color)
        return img

    def test_composited_pixels_are_snapped_to_the_anchor_palette(self):
        grid = gear.Grid(CELL_W, CELL_H, COLS, ROWS)
        sheet = _blank()
        anchor = gear.Anchor(0, 0, 70.0, 40.0, gear.MEASURED, 3)
        weapon = self._weapon(color=(198, 41, 43, 255))  # near, not exact, red
        gear.stamp_generated(sheet, grid, anchor, weapon,
                             anchor_palette=[(200, 40, 40)], anchor_stroke=6.0)
        mask = gear.cell_mask(sheet, grid, 0, 0)
        assert mask.count > 0
        colours = {p[:3] for p in sheet.getdata() if p[3] > 8}
        assert colours <= {(200, 40, 40)}

    def test_a_thin_ink_line_against_a_thick_anchor_is_flagged(self):
        grid = gear.Grid(CELL_W, CELL_H, COLS, ROWS)
        sheet = _blank()
        anchor = gear.Anchor(0, 0, 70.0, 40.0, gear.MEASURED, 3)
        thin_weapon = self._weapon(width=1)
        out = gear.stamp_generated(sheet, grid, anchor, thin_weapon,
                                   anchor_palette=[(210, 90, 20)],
                                   anchor_stroke=6.0)
        assert out["line_weight_flag"] is True

    def test_no_pinned_palette_still_composites_without_conforming(self):
        grid = gear.Grid(CELL_W, CELL_H, COLS, ROWS)
        sheet = _blank()
        anchor = gear.Anchor(0, 0, 70.0, 40.0, gear.MEASURED, 3)
        weapon = self._weapon()
        out = gear.stamp_generated(sheet, grid, anchor, weapon)
        assert out["line_weight_flag"] is False
        assert gear.cell_mask(sheet, grid, 0, 0).count > 0


class TestConformToPalette:
    """EXIT 67 postmortem item 9: a GENERATED weapon icon must conform to the
    pinned bible palette before item_to_spriteframes rides it into combat.
    stamp_generated was never built; conform_to_palette is the stand-in."""

    PALETTE = [(20, 20, 20), (200, 40, 40), (240, 240, 240)]

    def _off_palette_icon(self, tmp_path: Path) -> Path:
        # A flat, limited-palette icon (so quantisation is lossless-visible)
        # drawn entirely in colours that are NOT in PALETTE.
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        for y in range(8, 24):
            for x in range(8, 24):
                img.putpixel((x, y), (90, 160, 30, 255))  # off-palette green
        p = tmp_path / "icon.png"
        img.save(p)
        return p

    def test_quantises_every_opaque_pixel_onto_the_pinned_palette(self, tmp_path):
        icon = self._off_palette_icon(tmp_path)
        result = gear.conform_to_palette(icon, self.PALETTE)
        assert result["ok"] is True
        assert result["changed"] > 0.0
        with Image.open(icon).convert("RGBA") as img:
            seen = {px[:3] for px in img.getdata() if px[3] > 8}
        assert seen and seen.issubset(set(self.PALETTE))

    def test_stroke_width_adds_a_visible_outline(self, tmp_path):
        icon = self._off_palette_icon(tmp_path)
        before_bbox = Image.open(icon).getbbox()
        result = gear.conform_to_palette(icon, self.PALETTE, stroke_width=2)
        assert result["ok"] is True
        assert result["stroked"] is True
        with Image.open(icon) as after:
            after_bbox = after.getbbox()
        # The outline grows the opaque bounding box outward on every side.
        assert after_bbox[0] < before_bbox[0] and after_bbox[1] < before_bbox[1]
        assert after_bbox[2] > before_bbox[2] and after_bbox[3] > before_bbox[3]

    def test_missing_icon_fails_without_raising(self, tmp_path):
        result = gear.conform_to_palette(tmp_path / "nope.png", self.PALETTE)
        assert result["ok"] is False
        assert "no icon" in result["error"]

    def test_empty_palette_leaves_the_file_untouched(self, tmp_path):
        icon = self._off_palette_icon(tmp_path)
        before = icon.read_bytes()
        result = gear.conform_to_palette(icon, [])
        assert result["ok"] is False
        assert icon.read_bytes() == before
