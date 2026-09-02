"""The sheet in progress: its frames, its measurements, and what is NOT known.

The art seat's failure mode is a panel full of plausible green bars. So what is
pinned here is mostly refusal: a metadata key that is absent produces no row, a
sheet whose width is not a whole number of cells produces no frame strip, and a
per-frame measurement that only means something about a whole picture is not
reported per frame.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from bgate_core.art import artsheet


# ---------------------------------------------------------------------------
# Frame geometry — derived, never assumed
# ---------------------------------------------------------------------------
def test_a_row_of_square_cells_gives_its_frame_count():
    assert artsheet.frame_count("1536x512") == 3
    assert artsheet.frame_count("1024x512") == 2


def test_a_size_that_is_not_a_whole_number_of_cells_is_unknown():
    """A wrong split measures the seam between two frames and calls it a hole."""
    assert artsheet.frame_count("1500x512") is None


def test_one_square_image_is_not_a_one_frame_strip():
    """A portrait is a picture. Calling it "one frame" would let the panel draw
    a strip over something that has no strip."""
    assert artsheet.frame_count("1024x1024") is None


def test_garbage_size_is_unknown_not_zero():
    for bad in ("", "big", None, "512", "axb"):
        assert artsheet.frame_count(bad) is None


# ---------------------------------------------------------------------------
# Which revision is "in progress"
# ---------------------------------------------------------------------------
def test_a_superseded_revision_is_not_the_sheet_in_progress():
    """r4 and r5 are the same picture; showing r4 shows work already replaced."""
    picked = artsheet.pick([
        {"id": 2, "kind": "texture", "status": "superseded"},
        {"id": 1, "kind": "texture", "status": "approved"},
    ])
    assert picked["id"] == 1


def test_an_audio_revision_is_not_art():
    """An .mp3 in the art panel is what "newest artifact" gets you."""
    assert artsheet.pick([{"id": 9, "kind": "audio", "status": "approved"}]) is None


def test_no_art_at_all_is_none_not_an_empty_shell():
    assert artsheet.pick([]) is None
    out = artsheet.report(None, root=".")
    assert out["sheet"] is None and out["frames"] == [] and out["measures"] == []


# ---------------------------------------------------------------------------
# Measurements — reported, never invented
# ---------------------------------------------------------------------------
def test_an_absent_measurement_draws_no_row():
    """THE RULE THIS FILE EXISTS FOR. A missing key is not a zero — a sheet
    nobody audited must not read as a sheet that passed."""
    assert artsheet.measures({}) == []
    # The gate rides along with the number, so a reader never has to know which
    # threshold applied to which measurement. Added to measures() after this
    # expectation was written.
    assert artsheet.measures({"alpha": {"dirty_alpha": 0.02}}) == [
        {"label": "dirty alpha", "value": 0.02, "display": "0.020",
         "fraction": 0.02, "hi_is_good": False,
         "note": "transparent pixels still carrying RGB",
         "gate": 0.15, "passes": True}]


def test_a_measured_zero_is_a_row():
    """"Checked and clean" and "not checked" must not look the same, so a
    measured 0.0 is drawn and an absent one is not."""
    rows = artsheet.measures({"alpha": {"white_fringe": 0.0}})
    assert rows[0]["label"] == "white halo" and rows[0]["value"] == 0.0


def test_residual_chroma_is_skipped_when_it_was_never_checked():
    """chroma.audit reports None, not 0.0, when no key was passed."""
    rows = artsheet.measures({"alpha": {"residual_chroma": None,
                                        "dirty_alpha": 0.01}})
    assert [r["label"] for r in rows] == ["dirty alpha"]


def test_chroma_headroom_is_a_fraction_of_the_rgb_cube():
    """The scale is the cube's diagonal, so the bar is a real proportion rather
    than a ceiling somebody picked to make it look full."""
    row = artsheet.measures({"chroma": {"distance": 441.7, "name": "cyan"}})[0]
    assert row["label"] == "chroma headroom"
    assert row["hi_is_good"] is True
    assert row["fraction"] == pytest.approx(1.0, abs=0.01)
    # And it carries its own unit: three decimals belong to the fractions, not
    # to a length in RGB space.
    assert row["display"] == "442 / 442"


def test_a_boolean_is_not_a_measurement():
    """`True` is an int in Python and would have drawn a 1.0 bar."""
    assert artsheet.measures({"alpha": {"clean": True}}) == []


# ---------------------------------------------------------------------------
# Per-frame slicing
# ---------------------------------------------------------------------------
def _row_sheet(path: Path, cells: int, size: int = 64) -> None:
    im = Image.new("RGBA", (size * cells, size), (0, 0, 0, 0))
    for i in range(cells):
        # A blob in the middle of each cell: opaque art, transparent surround.
        for x in range(i * size + 16, i * size + 48):
            for y in range(16, 48):
                im.putpixel((x, y), (200, 40, 40, 255))
    im.save(path)


def test_each_cell_is_audited_separately(tmp_path):
    sheet = tmp_path / "walk_row.png"
    _row_sheet(sheet, 3)
    rows = artsheet.measure_frames(sheet, 3)
    assert [r["index"] for r in rows] == [0, 1, 2]
    assert all(r["dirty_alpha"] is not None for r in rows)


def test_border_opacity_is_never_reported_per_frame(tmp_path):
    """It asks "is the OUTER edge of this image opaque" — on a cell that
    detects the cut, not the art. Measured on the real project: 0.0 for the
    sheet and 0.20/0.43/0.22 for its three cells, all three "background bleed"
    on a sheet whose own border audit was clean."""
    sheet = tmp_path / "walk_row.png"
    _row_sheet(sheet, 2)
    rows = artsheet.measure_frames(sheet, 2)
    assert all("border_opaque" not in r for r in rows)
    assert not any(f.startswith("background bleed")
                   for r in rows for f in r["flags"])


def test_an_unreadable_image_measures_nothing_rather_than_clean(tmp_path):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not a png")
    assert artsheet.measure_frames(bad, 3) == []
    assert artsheet.measure_frames(tmp_path / "gone.png", 3) == []


def test_the_slice_is_cached_by_content(tmp_path):
    """The workspace polls; re-auditing unchanged bytes is pure cost."""
    sheet = tmp_path / "walk_row.png"
    _row_sheet(sheet, 2)
    first = artsheet.measure_frames(sheet, 2)
    first[0]["flags"].append("mutated by the caller")
    # The cache must hand back a copy, or one panel's edit becomes everyone's.
    assert artsheet.measure_frames(sheet, 2)[0]["flags"] == []


# ---------------------------------------------------------------------------
# The whole report
# ---------------------------------------------------------------------------
def test_the_pin_is_named_and_says_what_it_binds(tmp_path):
    out = artsheet.report({
        "id": 1, "logical_name": "wizard_hurt_row", "revision": 3,
        "path": "art/wizard_hurt_row.png", "status": "approved",
        "kind": "texture",
        "metadata": {"size": "1024x512",
                     "ref_pins": [{"name": "accounting-wizard", "revision": 1,
                                   "path": "/refs/accounting-wizard.r1.png"}]},
    }, root=tmp_path, slice_frames=False)
    assert out["pin"]["name"] == "accounting-wizard"
    assert out["pin"]["note"] == "approved — every frame conditions on this"
    assert out["sheet"]["frames"] == 2


def test_no_pin_is_none_not_an_empty_card():
    out = artsheet.report({"id": 1, "kind": "texture", "status": "approved",
                           "metadata": {"size": "512x512", "ref_pins": []}},
                          root=".", slice_frames=False)
    assert out["pin"] is None


class TestContractGrid:
    """frame_count under a declared cell — the four-corner game's 96x80
    cols x 2 grid is not a square strip, and the old rule went blind on
    exactly the sheets that project ships."""

    def test_a_declared_grid_counts_its_cells(self):
        assert artsheet.frame_count("384x160", cell=(96, 80), rows=2) == 8
        assert artsheet.frame_count("384x80", cell=(96, 80), rows=1) == 4

    def test_a_sheet_that_disagrees_with_its_contract_is_not_guessed(self):
        assert artsheet.frame_count("384x160", cell=(96, 80), rows=1) is None
        assert artsheet.frame_count("100x160", cell=(96, 80), rows=2) is None

    def test_without_a_contract_the_square_strip_rule_stands(self):
        assert artsheet.frame_count("1536x512") == 3
        assert artsheet.frame_count("384x160") is None
