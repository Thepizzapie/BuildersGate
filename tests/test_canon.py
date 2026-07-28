"""canon_check is the anti-hallucination gate — its false NEGATIVES are what
let lore rot, and its false POSITIVES are what make agents ignore it. Both
directions are tested here.
"""
from __future__ import annotations

import pytest

from bgate_core import canon, lore


@pytest.fixture()
def world(root):
    lore.add_entity(root, "faction", "The Ashen Order", summary="Zealots of the flame.",
                    status="canon")
    lore.add_entity(root, "character", "Sera Vane", summary="Their exiled general.",
                    status="canon")
    lore.add_entity(root, "place", "Cinder Vault", status="canon")
    lore.link(root, "Sera Vane", "exiled_from", "The Ashen Order")
    lore.add_fact(root, "The Ashen Order", "The Ashen Order worships the flame.",
                  locked=True)
    lore.add_fact(root, "Cinder Vault", "The Cinder Vault was sealed for seven years.",
                  locked=True)
    lore.add_fact(root, "Sera Vane", "Sera Vane was exiled after the siege.")
    return root


class TestClean:
    def test_consistent_text_passes(self, world):
        got = canon.check(world, "The Ashen Order worships the flame above all else.")
        assert got["verdict"] == "ok"
        assert got["flags"] == []

    def test_returns_canon_for_mentioned_entities(self, world):
        got = canon.check(world, "Sera Vane walks into the Cinder Vault.")
        assert {m["slug"] for m in got["mentions"]} == {"sera-vane", "cinder-vault"}
        assert len(got["canon"]) == 2

    def test_untracked_prose_is_clean(self, world):
        got = canon.check(world, "the wind moved through the empty street")
        assert got["verdict"] == "ok"


class TestPolarity:
    def test_flat_contradiction_of_locked_fact_is_conflict(self, world):
        got = canon.check(world, "The Ashen Order does not worship the flame.")
        assert got["verdict"] == "conflict"
        assert [f["code"] for f in got["flags"]] == ["polarity_conflict"]

    def test_unlocked_fact_contradiction_is_review_not_conflict(self, world):
        got = canon.check(world, "Sera Vane was not exiled after the siege.")
        assert got["verdict"] == "review"
        assert any(f["code"] == "polarity_conflict" for f in got["flags"])

    def test_unrelated_negation_does_not_flag(self, world):
        got = canon.check(world, "The Ashen Order worships the flame. It never rains here.")
        assert not any(f["code"] == "polarity_conflict" for f in got["flags"])


class TestNumeric:
    def test_number_disagreement_flags(self, world):
        got = canon.check(world, "The Cinder Vault was sealed for three years.")
        assert got["verdict"] == "conflict"
        flag = next(f for f in got["flags"] if f["code"] == "numeric_conflict")
        assert "7" in flag["message"] and "3" in flag["message"]

    def test_matching_number_passes(self, world):
        got = canon.check(world, "The Cinder Vault was sealed for seven years.")
        assert not any(f["code"] == "numeric_conflict" for f in got["flags"])

    def test_spelled_and_digit_forms_agree(self, world):
        got = canon.check(world, "The Cinder Vault was sealed for 7 years.")
        assert not any(f["code"] == "numeric_conflict" for f in got["flags"])


class TestStatus:
    def test_retired_entity_is_conflict(self, world):
        lore.update_entity(world, "sera-vane", status="retired")
        got = canon.check(world, "Sera Vane draws her blade.")
        assert got["verdict"] == "conflict"
        assert any(f["code"] == "retired_entity" for f in got["flags"])

    def test_draft_entity_is_review_only(self, world):
        lore.add_entity(world, "character", "Torv Ekkel", status="draft")
        got = canon.check(world, "Torv Ekkel waits at the gate.")
        assert got["verdict"] == "review"
        assert any(f["code"] == "draft_entity" for f in got["flags"])


class TestUnknownEntities:
    def test_invented_proper_noun_flags(self, world):
        got = canon.check(world, "Sera Vane rides for Grantham Keep at dawn.")
        flag = next(f for f in got["flags"] if f["code"] == "unknown_entity")
        assert "Grantham" in flag["name"]

    def test_sentence_opener_is_not_an_entity(self, world):
        got = canon.check(world, "The flame burns. Then the door opens. When it does, we run.")
        assert not any(f["code"] == "unknown_entity" for f in got["flags"])

    def test_known_entity_is_not_flagged_as_unknown(self, world):
        got = canon.check(world, "Sera Vane returns to the Cinder Vault.")
        assert not any(f["code"] == "unknown_entity" for f in got["flags"])


class TestHelpers:
    @pytest.mark.parametrize("text,expected", [
        ("the vault is sealed", False),
        ("the vault is not sealed", True),
        ("she cannot enter", True),
        ("she can't enter", True),
        ("nothing burns forever", False),
    ])
    def test_negation_detection(self, text, expected):
        assert canon.has_negation(text) is expected

    def test_numbers_normalize_across_forms(self):
        assert canon.numbers_in("seven years") == canon.numbers_in("7 years")
