"""ITEM 35 / ITEM 37 — the director reviewed art at contact-sheet size (150px
per frame) and passed a translucent second head and an upright rifle standing
alone. These tests measure the fix directly: the composed review image renders
each frame at >= 300px by default, tiles into pages when a sheet would
otherwise blow past a sane page size, and the candidate-vs-concept compare
composes at equal height with real measured deltas.
"""
from __future__ import annotations

import pytest

from bgate_core.art import spritekit as kit

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

BACK = (12, 12, 16, 255)
BODY = (190, 170, 120, 255)


def _figure_sheet(tmp_path, name, *, cell=(64, 96), columns=4, rows=1):
    cw, ch = cell
    sheet = Image.new("RGBA", (cw * columns, ch * rows), BACK)
    for r in range(rows):
        for c in range(columns):
            body = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            from PIL import ImageDraw
            d = ImageDraw.Draw(body)
            d.rectangle([cw // 4, ch // 3, 3 * cw // 4, ch - 4], fill=BODY)
            sheet.paste(body, (c * cw, r * ch), body)
    path = tmp_path / name
    sheet.save(path)
    return str(path)


class TestReviewFloor:
    def test_default_upscales_a_small_frame_past_the_review_floor(self, tmp_path):
        path = _figure_sheet(tmp_path, "small.png", cell=(64, 96), columns=4)
        out = tmp_path / "guides.png"
        got = kit.draw_guides(path, 4, out)          # default review_px=300
        assert got["ok"]
        assert got["frame_px"] >= kit.REVIEW_MIN_PX
        with Image.open(out) as drawn:
            # 4 native columns of 64px each -> at least 4*300 wide.
            assert drawn.width >= 4 * kit.REVIEW_MIN_PX

    def test_a_frame_already_past_the_floor_is_not_shrunk(self, tmp_path):
        path = _figure_sheet(tmp_path, "big.png", cell=(400, 500), columns=2)
        out = tmp_path / "guides.png"
        got = kit.draw_guides(path, 2, out)
        assert got["ok"]
        assert got["frame_px"] == 400          # untouched, already >= 300

    def test_a_custom_review_px_is_honoured(self, tmp_path):
        path = _figure_sheet(tmp_path, "small2.png", cell=(50, 80), columns=2)
        out = tmp_path / "guides.png"
        got = kit.draw_guides(path, 2, out, review_px=600)
        assert got["ok"]
        assert got["frame_px"] >= 600

    def test_a_tall_sheet_splits_into_pages(self, tmp_path):
        # 20 row-bands at a scale that pushes the combined canvas well past
        # _MAX_PAGE_DIM, so the split path has to fire.
        path = _figure_sheet(tmp_path, "tall.png", cell=(60, 90), columns=2,
                             rows=20)
        out = tmp_path / "guides.png"
        got = kit.draw_guides(path, 2, out, rows=20, review_px=300)
        assert got["ok"]
        assert len(got["pages"]) > 1
        for page in got["pages"]:
            with Image.open(page) as im:
                assert im.height <= kit._MAX_PAGE_DIM + 1


class TestConceptCompare:
    def test_composes_side_by_side_at_equal_height_with_measured_deltas(
            self, tmp_path):
        candidate = _figure_sheet(tmp_path, "candidate.png", cell=(64, 96),
                                  columns=1)
        concept = _figure_sheet(tmp_path, "concept.png", cell=(200, 300),
                                columns=1)
        out = tmp_path / "checks" / "compare.png"
        got = kit.concept_compare(candidate, concept, out)
        assert got["ok"] and out.exists()
        with Image.open(out) as drawn:
            assert drawn.height >= kit.REVIEW_MIN_PX
        measured = got["measured"]
        assert "palette_distance" in measured
        assert "ink_density" in measured
        assert measured["figure_height_ratio"] is not None
