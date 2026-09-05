"""Editing a .tscn as text — the part that must never corrupt a scene.

The bar here is not "Godot probably opens it". It is that load_steps matches
the resource count, ids stay unique, node names stay unique among siblings, and
a removal takes the resource with it — because every one of those failures
shows up hours later as an engine error with no connection to the click that
caused it.
"""
from __future__ import annotations

import re

import pytest

from bgate_core.level import scenewire

SCENE = """[gd_scene load_steps=3 format=3 uid="uid://abc"]

[ext_resource type="Script" path="res://scripts/player.gd" id="1_player"]

[sub_resource type="RectangleShape2D" id="Rect_a"]
size = Vector2(16, 24)

[node name="Main" type="Node2D"]

[node name="Ground" type="StaticBody2D" parent="."]
position = Vector2(320, 330)

[node name="GroundVisual" type="ColorRect" parent="Ground"]
color = Color(0.18, 0.2, 0.24, 1)

[node name="Player" type="CharacterBody2D" parent="."]
script = ExtResource("1_player")
"""


def _load_steps(text: str) -> int:
    return int(re.search(r"load_steps=(\d+)", text).group(1))


def _consistent(text: str) -> None:
    """load_steps is ext + sub + 1 — the engine's own accounting."""
    parsed = scenewire.parse(text)
    assert _load_steps(text) == len(parsed["ext"]) + parsed["sub_count"] + 1
    ids = [e["id"] for e in parsed["ext"]]
    assert len(ids) == len(set(ids)), "duplicate ext_resource id"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_parse_finds_the_tree_and_the_root():
    p = scenewire.parse(SCENE)
    assert p["root"] == "Main"
    assert [n["name"] for n in p["nodes"]] == [
        "Main", "Ground", "GroundVisual", "Player"]
    assert p["nodes"][0]["parent"] is None
    assert scenewire.node_path(p["nodes"][2]) == "Ground/GroundVisual"


def test_a_non_scene_is_refused_rather_than_guessed_at():
    with pytest.raises(scenewire.WireError):
        scenewire.parse("just some text\n")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def test_a_png_becomes_a_sprite2d_with_its_texture_set():
    r = scenewire.wire(SCENE, "res://assets/hero.png")
    assert r["node_type"] == "Sprite2D"
    assert '[node name="Hero" type="Sprite2D" parent="."]' in r["text"]
    assert f'texture = ExtResource("{r["id"]}")' in r["text"]
    assert 'type="Texture2D" path="res://assets/hero.png"' in r["text"]
    _consistent(r["text"])


def test_a_spriteframes_tres_becomes_an_animatedsprite2d():
    r = scenewire.wire(SCENE, "res://assets/hero_frames.tres")
    assert r["node_type"] == "AnimatedSprite2D"
    assert f'sprite_frames = ExtResource("{r["id"]}")' in r["text"]
    _consistent(r["text"])


def test_an_ogg_becomes_an_audio_player():
    r = scenewire.wire(SCENE, "res://audio/hit.ogg")
    assert r["node_type"] == "AudioStreamPlayer2D"
    assert f'stream = ExtResource("{r["id"]}")' in r["text"]


def test_a_scene_is_instanced_not_typed():
    r = scenewire.wire(SCENE, "res://scenes/enemy.tscn")
    assert f'instance=ExtResource("{r["id"]}")' in r["text"]
    assert "type=" not in r["text"].rsplit("[node", 1)[1].split("]", 1)[0]
    _consistent(r["text"])


def test_load_steps_is_recomputed_not_incremented():
    """A scene whose header was already wrong gets FIXED, not preserved."""
    broken = SCENE.replace("load_steps=3", "load_steps=99")
    r = scenewire.wire(broken, "res://assets/hero.png")
    _consistent(r["text"])
    assert _load_steps(r["text"]) == 4          # 2 ext + 1 sub + 1


def test_wiring_the_same_asset_twice_reuses_the_resource():
    first = scenewire.wire(SCENE, "res://assets/hero.png")
    second = scenewire.wire(first["text"], "res://assets/hero.png")
    assert second["reused"] is True
    assert second["id"] == first["id"]
    assert second["text"].count('path="res://assets/hero.png"') == 1
    assert second["node"] == "Hero2"            # siblings must stay unique
    _consistent(second["text"])


def test_wiring_under_a_named_parent():
    r = scenewire.wire(SCENE, "res://assets/hero.png", parent="Ground")
    assert 'parent="Ground"' in r["text"].rsplit("[node", 1)[1]


def test_an_unknown_parent_is_refused_with_the_options_listed():
    with pytest.raises(scenewire.WireError) as exc:
        scenewire.wire(SCENE, "res://assets/hero.png", parent="Nope")
    assert "Ground/GroundVisual" in str(exc.value)


def test_a_non_res_path_is_refused():
    with pytest.raises(scenewire.WireError):
        scenewire.wire(SCENE, "assets/hero.png")


def test_an_unknown_file_kind_is_refused_rather_than_guessed():
    with pytest.raises(scenewire.WireError):
        scenewire.wire(SCENE, "res://notes/design.md")


def test_a_script_refuses_the_node_path_and_says_where_to_go():
    with pytest.raises(scenewire.WireError) as exc:
        scenewire.wire(SCENE, "res://scripts/enemy.gd")
    assert "attach_script" in str(exc.value)


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------
def test_attach_script_adds_the_line_to_an_existing_node():
    r = scenewire.attach_script(SCENE, "res://scripts/enemy.gd", node="Ground")
    body = r["text"].split('[node name="Ground"')[1].split("[node")[0]
    assert f'script = ExtResource("{r["id"]}")' in body
    _consistent(r["text"])


def test_replacing_a_script_drops_the_one_it_replaced():
    """Otherwise the old script stays an ext_resource and Atlas calls it live."""
    r = scenewire.attach_script(SCENE, "res://scripts/other.gd", node="Player")
    assert "res://scripts/player.gd" not in r["text"]
    assert r["dropped_resources"] == ["res://scripts/player.gd"]
    assert r["text"].count("script = ExtResource") == 1
    _consistent(r["text"])


# ---------------------------------------------------------------------------
# Unwiring
# ---------------------------------------------------------------------------
def test_unwire_removes_the_node_and_its_now_unused_resource():
    wired = scenewire.wire(SCENE, "res://assets/hero.png")["text"]
    out = scenewire.unwire(wired, "Hero")
    assert '[node name="Hero"' not in out["text"]
    assert "res://assets/hero.png" not in out["text"]
    assert out["dropped_resources"] == ["res://assets/hero.png"]
    _consistent(out["text"])


def test_unwire_keeps_a_resource_another_node_still_uses():
    a = scenewire.wire(SCENE, "res://assets/hero.png")["text"]
    b = scenewire.wire(a, "res://assets/hero.png")["text"]
    out = scenewire.unwire(b, "Hero2")
    assert "res://assets/hero.png" in out["text"]
    assert out["dropped_resources"] == []
    _consistent(out["text"])


def test_unwire_refuses_the_root():
    with pytest.raises(scenewire.WireError):
        scenewire.unwire(SCENE, ".")


def test_unwire_refuses_a_node_with_children():
    with pytest.raises(scenewire.WireError) as exc:
        scenewire.unwire(SCENE, "Ground")
    assert "child" in str(exc.value)


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
def test_apply_writes_and_leaves_the_previous_bytes_behind(tmp_path):
    scene = tmp_path / "main.tscn"
    scene.write_text(SCENE, encoding="utf-8")
    new = scenewire.wire(SCENE, "res://assets/hero.png")["text"]
    res = scenewire.apply(scene, new, root=tmp_path)
    assert res["written"] is True
    assert scene.read_text(encoding="utf-8") == new
    backup = tmp_path / res["backup"]
    assert backup.read_text(encoding="utf-8") == SCENE


def test_a_dry_run_touches_nothing(tmp_path):
    scene = tmp_path / "main.tscn"
    scene.write_text(SCENE, encoding="utf-8")
    res = scenewire.apply(scene, "REPLACED", root=tmp_path, dry_run=True)
    assert res["written"] is False
    assert scene.read_text(encoding="utf-8") == SCENE
    assert not scenewire.backup_dir(tmp_path).exists()


# ---------------------------------------------------------------------------
# Roles — what a node IS to someone building a scene
# ---------------------------------------------------------------------------
def test_a_resource_path_decides_the_role_over_the_node_type():
    """A Node2D is a character or a spawn point depending entirely on what
    hangs off it, so paths outrank classes."""
    assert scenewire.role_for("Node2D", resources=["res://assets/enemies/x.png"]) == "enemy"
    assert scenewire.role_for("Node2D", resources=["res://assets/props/x.png"]) == "prop"
    assert scenewire.role_for("Sprite2D", resources=["res://assets/characters/h.png"]) == "character"


def test_a_script_name_decides_when_there_are_no_resources():
    assert scenewire.role_for("Node", script="res://scripts/spawner.gd") == "controller"
    assert scenewire.role_for("Node2D", script="res://scripts/enemy_ai.gd") == "enemy"


def test_the_type_is_the_fallback_not_the_first_answer():
    assert scenewire.role_for("CanvasLayer") == "layer"
    assert scenewire.role_for("AudioStreamPlayer2D") == "audio"
    assert scenewire.role_for("CharacterBody2D") == "character"
    assert scenewire.role_for("Node2D") == "node"


def test_ui_is_a_place_in_the_tree_not_a_class():
    """A ColorRect standing in for a platform is a placeholder VISUAL; the same
    class under a CanvasLayer is the HUD. Only the tree tells them apart."""
    roles = {n["path"]: n["role"] for n in scenewire.outline(SCENE)}
    assert roles["Ground/GroundVisual"] == "visual"

    with_hud = scenewire.add_node(SCENE, name="HUD", node_type="CanvasLayer")["text"]
    with_hud = scenewire.add_node(with_hud, name="Score", node_type="Label",
                                  parent="HUD")["text"]
    with_hud = scenewire.add_node(with_hud, name="Bar", node_type="ColorRect",
                                  parent="HUD")["text"]
    roles = {n["path"]: n["role"] for n in scenewire.outline(with_hud)}
    assert roles["HUD"] == "layer"
    assert roles["HUD/Score"] == "ui"
    assert roles["HUD/Bar"] == "ui", "a rectangle under a CanvasLayer is the HUD"


def test_only_ambiguous_classes_are_demoted_out_of_ui():
    """A Label is never placeholder art, and a scripted Node2D whose script
    lives in scripts/ui/ is not a rectangle — demoting either would replace a
    right answer with a wrong one."""
    labelled = scenewire.add_node(SCENE, name="Caption", node_type="Label")["text"]
    roles = {n["path"]: n["role"] for n in scenewire.outline(labelled)}
    assert roles["Caption"] == "ui"

    scene = ('[gd_scene load_steps=2 format=3]\n\n'
             '[ext_resource type="Script" path="res://scripts/ui/combat_view.gd" id="1_v"]\n\n'
             '[node name="Combat" type="Node2D"]\n'
             'script = ExtResource("1_v")\n')
    assert scenewire.outline(scene)[0]["role"] == "ui"


def test_outline_reports_each_nodes_script_and_resources():
    wired = scenewire.wire(SCENE, "res://assets/hero.png", parent="Player")["text"]
    by_path = {n["path"]: n for n in scenewire.outline(wired)}
    assert by_path["Player"]["script"] == "res://scripts/player.gd"
    hero = by_path["Player/Hero"]
    assert [r["path"] for r in hero["resources"]] == ["res://assets/hero.png"]
    assert hero["resources"][0]["property"] == "texture"
    assert by_path["Player"]["properties"]["script"].startswith("ExtResource")


# ---------------------------------------------------------------------------
# Building — add, set, rename, reparent
# ---------------------------------------------------------------------------
def test_a_plain_node_needs_no_file_behind_it():
    """A scene is not only the assets in it: a CanvasLayer, a Timer, a grouping
    Node2D are all things a builder must be able to place."""
    r = scenewire.add_node(SCENE, name="HUD", node_type="CanvasLayer")
    assert '[node name="HUD" type="CanvasLayer" parent="."]' in r["text"]
    _consistent(r["text"])
    assert len(scenewire.parse(r["text"])["ext"]) == 1, "no resource was added"


def test_setting_and_clearing_a_property():
    r = scenewire.set_property(SCENE, "Player", "z_index", 5)
    body = r["text"].split('[node name="Player"')[1]
    assert "z_index = 5" in body
    back = scenewire.set_property(r["text"], "Player", "z_index", None)
    assert "z_index" not in back["text"].split('[node name="Player"')[1]


def test_a_property_value_that_is_not_a_godot_literal_is_refused():
    """A malformed value does not fail at save — it fails when the engine next
    loads the scene, pointing at a line nobody wrote by hand."""
    for bad in ("rm -rf /", "Vector2(1, 2); evil()", "some words"):
        with pytest.raises(scenewire.WireError):
            scenewire.set_property(SCENE, "Player", "position", bad)
    for good in ("Vector2(3, 4)", "true", "-2.5", '"a string"',
                 'ExtResource("1_player")', 'NodePath("../Camera2D")'):
        scenewire.set_property(SCENE, "Player", "position", good)


def test_a_property_name_that_is_not_a_name_is_refused():
    with pytest.raises(scenewire.WireError):
        scenewire.set_property(SCENE, "Player", "pos ition", 1)
    # Godot's own sub-property spelling must still work.
    scenewire.set_property(SCENE, "Player", "theme_override/font_size", 12)


def test_swapping_a_resource_repoints_the_node_and_drops_the_old_one():
    wired = scenewire.wire(SCENE, "res://assets/hero.png", parent="Player")["text"]
    node = scenewire.outline(wired)[-1]["path"]
    out = scenewire.swap_resource(wired, node, "res://assets/villain.png")
    assert "res://assets/villain.png" in out["text"]
    assert "res://assets/hero.png" not in out["text"], "the old one must not linger"
    assert out["dropped_resources"] == ["res://assets/hero.png"]
    _consistent(out["text"])


def test_swapping_reuses_a_resource_another_node_still_points_at():
    a = scenewire.wire(SCENE, "res://assets/hero.png", parent="Player")["text"]
    b = scenewire.wire(a, "res://assets/hero.png", parent="Ground")["text"]
    node = scenewire.outline(b)[-1]["path"]
    out = scenewire.swap_resource(b, node, "res://assets/villain.png")
    assert "res://assets/hero.png" in out["text"], "the other node still uses it"
    _consistent(out["text"])


def test_a_tres_declares_its_own_type():
    assert scenewire.resource_type_of(
        '[gd_resource type="TileSet" load_steps=2 format=3]') == "TileSet"
    assert scenewire.resource_type_of("not a resource") is None


def test_the_declared_resource_type_beats_the_suffix_guess():
    """Every .tres would otherwise be written as SpriteFrames, and an
    ext_resource with the wrong type loads as null — the node draws nothing."""
    guess = scenewire.wire(SCENE, "res://assets/office.tres")
    assert 'type="SpriteFrames"' in guess["text"]
    known = scenewire.wire(SCENE, "res://assets/office.tres", res_type="TileSet")
    assert 'type="TileSet"' in known["text"]


def test_renaming_repoints_every_child():
    r = scenewire.rename_node(SCENE, "Ground", "Platform")
    paths = [n["path"] for n in scenewire.outline(r["text"])]
    assert "Platform" in paths
    assert "Platform/GroundVisual" in paths and "Ground/GroundVisual" not in paths
    _consistent(r["text"])


def test_renaming_reports_nodepaths_it_did_not_rewrite():
    """Finding every NodePath reliably means understanding every property type.
    Leaving them alone is defensible; leaving them alone SILENTLY is not."""
    scene = SCENE + '\nremote_path = NodePath("../Ground")\n'
    r = scenewire.rename_node(scene, "Ground", "Platform")
    assert r["nodepath_references"] >= 1
    assert "NodePath" in r["summary"]


def test_renaming_refuses_a_name_a_sibling_already_has():
    with pytest.raises(scenewire.WireError):
        scenewire.rename_node(SCENE, "Ground", "Player")


def test_reparenting_moves_the_whole_subtree():
    r = scenewire.reparent(SCENE, "Ground", "Player")
    paths = [n["path"] for n in scenewire.outline(r["text"])]
    assert "Player/Ground" in paths and "Player/Ground/GroundVisual" in paths
    assert r["moved"] == 2          # Ground + GroundVisual
    _consistent(r["text"])


def test_reparenting_keeps_parents_declared_before_children():
    """Godot loads a .tscn top down; a child before its parent is a load error."""
    r = scenewire.reparent(SCENE, "Ground", "Player")
    order = [n["path"] for n in scenewire.outline(r["text"])]
    for i, path in enumerate(order):
        if "/" in path:
            assert path.rsplit("/", 1)[0] in order[:i]


def test_a_node_cannot_be_moved_inside_itself():
    with pytest.raises(scenewire.WireError):
        scenewire.reparent(SCENE, "Ground", "Ground/GroundVisual")
    with pytest.raises(scenewire.WireError):
        scenewire.reparent(SCENE, "Ground", "Ground")


def test_the_root_cannot_be_reparented_or_renamed():
    with pytest.raises(scenewire.WireError):
        scenewire.reparent(SCENE, ".", "Player")
    with pytest.raises(scenewire.WireError):
        scenewire.rename_node(SCENE, ".", "Other")


def test_a_recursive_delete_takes_the_children_with_it():
    r = scenewire.unwire(SCENE, "Ground", recursive=True)
    paths = [n["path"] for n in scenewire.outline(r["text"])]
    assert not [p for p in paths if p.startswith("Ground")]
    assert r["removed_count"] == 2  # Ground + GroundVisual
    _consistent(r["text"])


def test_a_non_recursive_delete_still_refuses_and_says_how():
    with pytest.raises(scenewire.WireError) as exc:
        scenewire.unwire(SCENE, "Ground")
    assert "recursive" in str(exc.value)


def test_building_a_scene_end_to_end_stays_loadable():
    """The sequence a person actually performs, with the invariants checked
    after every step rather than only at the end."""
    text = SCENE
    text = scenewire.add_node(text, name="HUD", node_type="CanvasLayer")["text"]
    _consistent(text)
    text = scenewire.add_node(text, name="Score", node_type="Label", parent="HUD")["text"]
    _consistent(text)
    text = scenewire.wire(text, "res://assets/hero.png", parent="Player")["text"]
    _consistent(text)
    node = [n["path"] for n in scenewire.outline(text) if n["name"] == "Hero"][0]
    text = scenewire.swap_resource(text, node, "res://assets/villain.png")["text"]
    _consistent(text)
    text = scenewire.set_property(text, "Player", "position", "Vector2(1, 2)")["text"]
    _consistent(text)
    text = scenewire.rename_node(text, "Player", "Hero")["text"]
    _consistent(text)
    text = scenewire.reparent(text, "Ground", "Hero")["text"]
    _consistent(text)
    text = scenewire.unwire(text, "Hero/Ground", recursive=True)["text"]
    _consistent(text)
    paths = [n["path"] for n in scenewire.outline(text)]
    assert "Hero" in paths and "HUD/Score" in paths
    assert not [p for p in paths if p.startswith("Ground")]


def test_repeated_wiring_stays_consistent(tmp_path):
    """Ten edits in a row; the header and the ids must still add up."""
    text = SCENE
    for i in range(10):
        text = scenewire.wire(text, f"res://assets/sheet_{i}.png")["text"]
        _consistent(text)
    assert len(scenewire.parse(text)["nodes"]) == 14


class TestALayerNoLongerProducedIsRemoved:
    """Replacing by name covers the run that makes the SAME layers again. It
    does not cover the run that makes FEWER: a level generated with decals and
    regenerated without them kept the old decal layer, still drawing — 42
    stains over a level that asked for none, in a scene that loads perfectly.
    Same family as the stacked-Ground defect, in the other direction."""

    BASE = ('[gd_scene load_steps=1 format=3]\n\n'
            '[node name="Root" type="Node2D"]\n')

    def _cells(self):
        return [{"x": 0, "y": 0, "source": 0, "ax": 0, "ay": 0, "alt": 0}]

    def _two_then_one(self, owns):
        c = self._cells()
        first = scenewire.wire_tilemap(
            self.BASE, "res://t.tres",
            [{"name": "Floor", "cells": c}, {"name": "Decals", "cells": c}],
            owns=owns)
        second = scenewire.wire_tilemap(
            first["text"], "res://t.tres", [{"name": "Floor", "cells": c}],
            owns=owns)
        return first, second

    def test_the_dropped_layer_goes(self):
        _, second = self._two_then_one(["Floor", "Decals"])
        assert 'name="Decals"' not in second["text"]
        assert 'name="Floor"' in second["text"]
        assert any(w["action"] == "remove" for w in second["layers"])

    def test_a_layer_not_claimed_is_left_alone(self):
        """`owns` is the generator's own list. Anything outside it belongs to
        somebody else and is not ours to delete."""
        _, second = self._two_then_one(["Floor"])
        assert 'name="Decals"' in second["text"]

    def test_without_owns_nothing_is_removed(self):
        c = self._cells()
        first = scenewire.wire_tilemap(
            self.BASE, "res://t.tres",
            [{"name": "Floor", "cells": c}, {"name": "Decals", "cells": c}])
        second = scenewire.wire_tilemap(
            first["text"], "res://t.tres", [{"name": "Floor", "cells": c}])
        assert 'name="Decals"' in second["text"]

    def test_a_claimed_name_held_by_another_node_type_survives(self):
        """Removing it would delete work nobody asked us to touch."""
        c = self._cells()
        first = scenewire.wire_tilemap(self.BASE, "res://t.tres",
                                       [{"name": "Floor", "cells": c}],
                                       owns=["Floor", "Decals"])
        text = first["text"] + '\n[node name="Decals" type="Sprite2D" parent="."]\n'
        second = scenewire.wire_tilemap(text, "res://t.tres",
                                        [{"name": "Floor", "cells": c}],
                                        owns=["Floor", "Decals"])
        assert 'type="Sprite2D"' in second["text"]
