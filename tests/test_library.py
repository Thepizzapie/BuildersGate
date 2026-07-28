"""The asset library — families, sheets, and the one fact that was missing.

"Approved" and "on disk" are not "shipping". The library's whole reason to
exist is that third column, so most of what is pinned here is USAGE: that a
sheet reached only through a SpriteFrames still counts, that a path the game
BUILDS at runtime still counts, and that a family nothing reaches says so
instead of looking identical to one the game loads every frame.
"""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bgate_core import library, screenmap
from bgate_ui.app import app


# ---------------------------------------------------------------------------
# Family detection
# ---------------------------------------------------------------------------
def test_the_longest_shared_prefix_wins():
    """prop_copier must not collapse into prop the moment a second prop lands."""
    out = library.group_stems([
        "prop_copier_ne", "prop_copier_se",
        "prop_conference_table_ne", "prop_conference_table_se",
        "prop_dead_plant", "prop_trash_can", "prop_water_cooler",
    ])
    assert out["prop_copier_ne"] == "prop_copier"
    assert out["prop_conference_table_se"] == "prop_conference_table"


def test_a_one_word_prefix_is_a_category_not_a_family():
    """`prop` is what the directory is ABOUT. It is not any asset's name."""
    out = library.group_stems([
        "prop_dead_plant", "prop_trash_can", "prop_water_cooler",
        "prop_potted_plant"])
    assert set(out.values()) == {"prop_dead_plant", "prop_trash_can",
                                 "prop_water_cooler", "prop_potted_plant"}


def test_a_one_word_prefix_over_one_word_variants_is_a_family():
    """The counter-case: hero_idle + hero_walk really are one character."""
    assert set(library.group_stems(["hero_idle", "hero_walk"]).values()) == {"hero"}


def test_a_tiny_sub_family_is_absorbed_by_the_family_it_extends():
    """The two dual-wield frames agree on a longer prefix than the other nine —
    left alone that makes a second 'weapon' out of two frames of the first."""
    stems = [f"coffeepot_flail_{a}" for a in
             ("idle", "walk", "ko", "punch", "kick", "block", "duck", "jump",
              "hurt")] + ["coffeepot_flail_dual_wield_main",
                          "coffeepot_flail_dual_wield_off"]
    assert set(library.group_stems(stems).values()) == {"coffeepot_flail"}


def test_an_action_set_becomes_one_family():
    out = library.group_stems([
        "pm_paladin_idle", "pm_paladin_walk", "pm_paladin_ko"])
    assert set(out.values()) == {"pm_paladin"}


def test_a_lone_file_is_its_own_family():
    assert library.group_stems(["office_tileset"]) == {
        "office_tileset": "office_tileset"}


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
def _map(screens, nodes, edges):
    return {"screens": screens, "nodes": nodes, "edges": edges,
            "orphans": [], "missing": []}


def test_usage_follows_a_spriteframes_hop_to_the_sheet():
    """A sheet reached only through a .tres is shipping, not dead."""
    use = library.usage_index(_map(
        screens=[{"id": "res://main.tscn"}],
        nodes={"res://main.tscn": {"label": "main"},
               "res://hero.tres": {"label": "hero.tres"},
               "res://hero.png": {"label": "hero.png"}},
        edges=[{"from": "res://main.tscn", "to": "res://hero.tres", "via": "scene"},
               {"from": "res://hero.tres", "to": "res://hero.png", "via": "tres"}]))
    assert use["res://hero.tres"] == ["main"]
    assert use["res://hero.png"] == ["main"], "the sheet behind the .tres is used"


def test_usage_names_every_screen_that_reaches_an_asset():
    use = library.usage_index(_map(
        screens=[{"id": "res://a.tscn"}, {"id": "res://b.tscn"}],
        nodes={"res://a.tscn": {"label": "a"}, "res://b.tscn": {"label": "b"},
               "res://shared.png": {"label": "shared.png"}},
        edges=[{"from": "res://a.tscn", "to": "res://shared.png", "via": "scene"},
               {"from": "res://b.tscn", "to": "res://shared.png", "via": "script"}]))
    assert use["res://shared.png"] == ["a", "b"]


def test_a_cycle_does_not_hang_the_walk():
    use = library.usage_index(_map(
        screens=[{"id": "res://m.tscn"}],
        nodes={"res://m.tscn": {"label": "m"}, "res://x.tres": {"label": "x"},
               "res://y.tres": {"label": "y"}},
        edges=[{"from": "res://m.tscn", "to": "res://x.tres", "via": "scene"},
               {"from": "res://x.tres", "to": "res://y.tres", "via": "tres"},
               {"from": "res://y.tres", "to": "res://x.tres", "via": "tres"}]))
    assert use["res://y.tres"] == ["m"]


def test_a_broken_map_yields_no_usage_rather_than_raising():
    assert library.usage_index({"error": "no project.godot"}) == {}


# ---------------------------------------------------------------------------
# Runtime-built paths (the screenmap half)
# ---------------------------------------------------------------------------
@pytest.fixture()
def game(root):
    """A Godot project whose loader builds one path and preloads another."""
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    props = root / "assets" / "props"
    props.mkdir(parents=True)
    for name in ("desk", "chair", "lamp"):
        Image.new("RGBA", (32, 32), (200, 40, 40, 255)).save(props / f"{name}.png")
    chars = root / "assets" / "characters" / "hero"
    chars.mkdir(parents=True)
    for action in ("idle", "walk"):
        Image.new("RGBA", (128, 64), (10, 90, 200, 255)).save(
            chars / f"hero_{action}.png")
    lonely = root / "assets" / "unused"
    lonely.mkdir()
    Image.new("RGBA", (16, 16)).save(lonely / "nobody_loves_me.png")

    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "floor.gd").write_text(
        'extends Node2D\n'
        'func place(p):\n'
        '\tvar tex := "res://assets/props/%s.png" % p\n'
        '\tvar hero := preload("res://assets/characters/hero/hero_idle.png")\n',
        encoding="utf-8")
    scenes = root / "scenes"
    scenes.mkdir()
    (scenes / "floor.tscn").write_text(
        '[gd_scene load_steps=2 format=3]\n\n'
        '[ext_resource type="Script" path="res://scripts/floor.gd" id="1_f"]\n\n'
        '[node name="Floor" type="Node2D"]\n'
        'script = ExtResource("1_f")\n', encoding="utf-8")
    return root


def test_a_runtime_built_path_marks_every_file_it_can_name(game):
    """Matching only exact literals reported whole prop libraries as dead."""
    smap = screenmap.scan(game)
    template = [e for e in smap["edges"] if e["via"] == "template"]
    assert {e["to"] for e in template} == {
        "res://assets/props/desk.png",
        "res://assets/props/chair.png",
        "res://assets/props/lamp.png",
    }
    assert not any("props" in o for o in smap["orphans"])


def test_a_template_can_only_ever_name_files_that_exist(game):
    smap = screenmap.scan(game)
    for edge in smap["edges"]:
        if edge["via"] == "template":
            assert smap["nodes"][edge["to"]]["exists"] is True


def test_a_template_does_not_reach_out_of_its_directory(game):
    """"%s" stands for one path SEGMENT — it must not match across a slash."""
    (game / "assets" / "props" / "nested").mkdir()
    Image.new("RGBA", (8, 8)).save(
        game / "assets" / "props" / "nested" / "deep.png")
    smap = screenmap.scan(game)
    assert not any(e["to"].endswith("nested/deep.png")
                   for e in smap["edges"] if e["via"] == "template")


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def test_scan_groups_a_family_and_picks_the_sheet_as_its_cover(game):
    data = library.scan(game)
    hero = next(f for f in data["families"] if f["label"] == "hero")
    assert hero["count"] == 2
    assert hero["category"] == "characters"
    assert hero["cover_size"] == [128, 64]
    assert sorted(m["variant"] for m in hero["members"]) == ["idle", "walk"]


def test_scan_reports_which_screens_reach_a_family_and_which_files_do_not(game):
    data = library.scan(game)
    hero = next(f for f in data["families"] if f["label"] == "hero")
    assert hero["in_use"] is True and hero["used_by"] == ["floor"]
    # Only hero_idle is preloaded; hero_walk reaches nothing, and saying so is
    # the entire point — a half-wired family looks fine at the family level.
    assert hero["unused"] == ["assets/characters/hero/hero_walk.png"]

    lonely = next(f for f in data["families"] if f["label"] == "nobody_loves_me")
    assert lonely["in_use"] is False and lonely["used_by"] == []


def test_scan_counts_rigged_members(game):
    from bgate_core import rigmap
    sheet = game / "assets" / "characters" / "hero" / "hero_idle.png"
    rigmap.save(sheet, {"grid": {"cell_w": 64, "cell_h": 64, "cols": 2, "rows": 1},
                        "labels": [{"slot": "main_hand", "frame": 0, "x": 4, "y": 4}]},
                sheet_size=(128, 64))
    data = library.scan(game)
    hero = next(f for f in data["families"] if f["label"] == "hero")
    assert hero["rigged"] == 1
    member = next(m for m in hero["members"] if m["variant"] == "idle")
    assert member["rig"]["slots"] == ["main_hand"]
    assert member["rig"]["frames"] == 2


def test_the_rig_sidecar_is_not_itself_an_asset(game):
    from bgate_core import rigmap
    sheet = game / "assets" / "characters" / "hero" / "hero_idle.png"
    rigmap.save(sheet, {"grid": {"cell_w": 64, "cell_h": 64, "cols": 2, "rows": 1}},
                sheet_size=(128, 64))
    data = library.scan(game)
    assert not any(m["name"].endswith(".rig.json")
                   for f in data["families"] for m in f["members"])


def test_stats_add_up(game):
    data = library.scan(game)
    st = data["stats"]
    assert st["families"] == len(data["families"])
    assert st["files"] == sum(f["count"] for f in data["families"])
    assert st["in_use"] + st["unused"] == st["families"]


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(game, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(game))
    with TestClient(app) as c:
        yield c


def test_the_endpoint_serves_families_with_usage(client):
    d = client.get("/api/assets/library", params={"force": True}).json()
    hero = next(f for f in d["families"] if f["label"] == "hero")
    assert hero["used_by"] == ["floor"]
    assert d["stats"]["shown"] == len(d["families"])


def test_the_endpoint_filters_by_category_and_query(client):
    only = client.get("/api/assets/library",
                      params={"force": True, "category": "characters"}).json()
    assert {f["category"] for f in only["families"]} == {"characters"}
    q = client.get("/api/assets/library", params={"q": "nobody"}).json()
    assert [f["label"] for f in q["families"]] == ["nobody_loves_me"]


def test_one_family_can_be_refetched_by_key(client):
    d = client.get("/api/assets/library", params={"force": True}).json()
    key = next(f["key"] for f in d["families"] if f["label"] == "hero")
    one = client.get("/api/assets/family", params={"key": key}).json()
    assert one["label"] == "hero" and len(one["members"]) == 2
    assert client.get("/api/assets/family",
                      params={"key": "nope::nope"}).status_code == 404


def test_a_project_with_no_artifacts_still_has_a_library(client):
    """Hand-drawn art has no artifact row; it must not vanish from the shelf."""
    d = client.get("/api/assets/library", params={"force": True}).json()
    assert d["families"]
    assert all(f["review_status"] is None for f in d["families"])


# ---------------------------------------------------------------------------
# The UI actually ships it
# ---------------------------------------------------------------------------
def test_the_library_panel_is_loaded_and_owns_the_assets_view():
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "bgate_ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/assetlib.js"' in html
    assert 'id="asset-lib-root"' in html
    assert 'assets: "AssetLib"' in html, "the assets view must activate the panel"
    js = (static / "assetlib.js").read_text(encoding="utf-8")
    assert "/api/assets/library" in js
    # The review queue is still reachable — this replaced the grid, not the
    # approve/reject workflow behind it.
    assert 'id="asset-grid"' in html
    assert "openAssetDrawer" in js
