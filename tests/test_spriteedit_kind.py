"""What an agent leaves behind is not all art.

The sprite picker walks the tree because hand-painted art has no artifact row —
which also means it picks up every contact sheet, before/after pair and zoomed
crop an agent made to look at its own work. In a real project those outnumber
the deliverables (2,101 art vs 736 review+test in the one this was written
against), and a picker that mixes them is a picker you scroll.

These assert the rule's TWO failure directions, which are not equally bad:
classifying a screenshot as art costs a scroll; classifying a real sheet as a
screenshot hides someone's work behind a filter. So the rule is conservative and
the last case here is the one that matters.
"""
from bgate_ui.routes.spriteedit import _kind


def test_review_artefacts_are_not_art():
    for rel in [
        "art/item357/review_hr_bard.png",
        "art/item394/before_after_copier_attack_ne_row.png",
        "art/item386/contact_copier_ne.png",
        "art/item394/mmbot_attack_se0_zoom.png",
        "art/item384/copier_mimic_onion_row0.png",
        "art/checks/chroma_wizard.png",
        "art/item1/compare_a_b.png",
        "tmp/scratch.png",
    ]:
        assert _kind(rel) == "review", rel


def test_test_fixtures_are_their_own_kind():
    assert _kind("tests/item136/frames/game/walk.png") == "test"


def test_deliverables_stay_art():
    """The direction that must not regress: a real sheet hidden from the picker
    is work somebody cannot open."""
    for rel in [
        "game/assets/characters/accounting_wizard_idle.png",
        "game/assets/props/derived/crate.png",
        "art/item386/final_copier_mimic.png",
        "game/assets/tiles/live/office_floor.png",
        "art/item346/v2/hr_bard_sheet.png",
    ]:
        assert _kind(rel) == "art", rel


def test_unrecognised_is_art_not_review():
    """Conservative by design — an unknown name is a deliverable until proven
    otherwise, because the cost of the two mistakes is not symmetric."""
    assert _kind("art/item999/some_new_naming_scheme.png") == "art"
