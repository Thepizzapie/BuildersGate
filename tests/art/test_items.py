"""The item-art pipeline's pure logic — taxonomy, prompt build, variant grid.

No network, no disk: build_prompt / plan_variants / manifest are the typed
contract the MCP tools and Codex drive, so their guarantees (style invariant
present, grid is a clean cartesian product, slugs stable and unique) are what's
worth pinning.
"""
from __future__ import annotations

import pytest

from bgate_core.art import items


class TestTaxonomy:
    def test_every_class_maps_to_a_known_slot(self):
        for cls in items.ITEM_CLASSES.values():
            assert cls["slot"] in items.SLOTS
            assert isinstance(cls["worn"], bool)

    def test_worn_classes_target_fighter_layers(self):
        # Worn gear must land on a real equip layer, never inventory/projectile.
        for name, cls in items.ITEM_CLASSES.items():
            if cls["worn"]:
                assert cls["slot"] in ("main_hand", "off_hand", "head",
                                       "body", "feet"), name


class TestBuildPrompt:
    def test_carries_the_style_invariant(self):
        p = items.build_prompt("main_hand", "curved saber")
        assert items.STYLE in p
        assert "transparent background" in p
        assert "curved saber" in p

    def test_weaves_the_variant_axes(self):
        p = items.build_prompt("main_hand", "saber", material="damascus steel",
                               element="fire", tier="legendary")
        assert "damascus steel" in p
        assert items.ELEMENTS["fire"] in p
        assert items.TIERS["legendary"] in p

    def test_unknown_element_and_tier_pass_through(self):
        p = items.build_prompt("consumable", "vial", element="quantum",
                               tier="mythic")
        assert "quantum" in p
        assert "mythic" in p

    def test_empty_descriptor_rejected(self):
        with pytest.raises(ValueError):
            items.build_prompt("head", "   ")

    def test_unknown_class_rejected(self):
        with pytest.raises(ValueError):
            items.build_prompt("spaceship", "x-wing")

    def test_style_clause_rides_after_the_invariant(self):
        # The cross-leg rail: a character profile's style lands in the prompt,
        # AFTER the invariant — it augments the rail, never replaces it.
        clause = "hand-inked cel shading, thick outlines, muted jewel palette"
        p = items.build_prompt("main_hand", "saber", style_clause=clause)
        assert items.STYLE in p
        assert p.index(clause) > p.index(items.STYLE)

    def test_no_style_clause_is_byte_identical_to_before(self):
        base = items.build_prompt("main_hand", "saber")
        assert base == items.build_prompt("main_hand", "saber", style_clause="")
        assert base == items.build_prompt("main_hand", "saber",
                                          style_clause="   ")


class TestPlanVariants:
    def test_no_axes_is_one_item(self):
        specs = items.plan_variants("throwable", "bomb", "round iron bomb")
        assert len(specs) == 1
        assert specs[0]["name"] == "bomb"
        assert specs[0]["slot"] == "projectile"
        assert specs[0]["worn"] is False

    def test_cartesian_product(self):
        specs = items.plan_variants(
            "main_hand", "saber", "curved saber",
            materials=["iron", "steel"], tiers=["common", "legendary"])
        assert len(specs) == 4  # 2 materials x 2 tiers
        names = {s["name"] for s in specs}
        assert len(names) == 4  # all unique
        assert "saber_common_iron" in names
        assert "saber_legendary_steel" in names

    def test_each_spec_is_self_contained(self):
        [spec] = items.plan_variants("body", "cuirass", "plate cuirass",
                                     tiers=["epic"])
        for key in ("name", "item_class", "slot", "worn", "descriptor",
                    "prompt", "params"):
            assert key in spec
        assert items.TIERS["epic"] in spec["prompt"]

    def test_overlapping_axes_dont_duplicate_slugs(self):
        # Same effective slug from two axis combos must collapse, not collide.
        specs = items.plan_variants("feet", "boots", "leather boots",
                                    materials=["", "leather"])
        names = [s["name"] for s in specs]
        assert len(names) == len(set(names))

    def test_style_clause_reaches_every_variant(self):
        clause = "hand-inked cel shading"
        specs = items.plan_variants("main_hand", "saber", "curved saber",
                                    tiers=["common", "rare"],
                                    style_clause=clause)
        assert all(clause in s["prompt"] for s in specs)


class TestSpend:
    def test_estimate_scales_with_count_and_quality(self):
        from bgate_adapters.imagegen import IMAGE_PRICE_USD
        assert items.estimate_cost(10, "medium") == round(
            10 * IMAGE_PRICE_USD["medium"], 2)
        assert items.estimate_cost(0) == 0.0
        assert items.estimate_cost(-3) == 0.0  # never a negative estimate

    def test_unknown_quality_prices_as_medium(self):
        # An estimate must never block the work — unknowns price, not raise.
        assert items.estimate_cost(4, "hallucinated") == items.estimate_cost(
            4, "medium")

    def test_split_existing_partitions_the_plan(self):
        specs = items.plan_variants("main_hand", "saber", "curved saber",
                                    tiers=["common", "rare", "epic"])
        on_disk = {items.rel_manifest_path("saber_rare")}
        to_mint, skipped = items.split_existing(specs, on_disk.__contains__)
        assert [s["name"] for s in skipped] == ["saber_rare"]
        assert {s["name"] for s in to_mint} == {"saber_common", "saber_epic"}

    def test_split_existing_nothing_on_disk_mints_all(self):
        specs = items.plan_variants("throwable", "bomb", "iron bomb",
                                    tiers=["common"])
        to_mint, skipped = items.split_existing(specs, lambda _rel: False)
        assert to_mint == specs and skipped == []


class TestIndex:
    def test_upsert_replaces_not_appends(self):
        [spec] = items.plan_variants("head", "helm", "iron helm",
                                     tiers=["rare"])
        man = items.manifest(spec, items.rel_art_path("head", spec["name"]))
        index: dict = {}
        items.update_index(index, man)
        items.update_index(index, {**man, "descriptor": "re-minted helm"})
        assert len(index["items"]) == 1  # re-mint replaced, no duplicate
        assert index["items"][man["name"]]["descriptor"] == "re-minted helm"


class TestPaths:
    def test_art_path_groups_by_class(self):
        p = items.rel_art_path("head", "Iron Helm")
        assert p == ".bgate_out/art/items/head/iron_helm.png"

    def test_manifest_shape(self):
        [spec] = items.plan_variants("main_hand", "saber", "curved saber",
                                     tiers=["rare"])
        m = items.manifest(spec, items.rel_art_path("main_hand", spec["name"]))
        assert m["slot"] == "main_hand"
        assert m["worn"] is True
        assert m["sprite"].endswith(".png")
        assert m["params"]["tier"] == "rare"
