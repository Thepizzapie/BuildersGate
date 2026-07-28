"""What a scene LOOKS like — the geometry the viewport is only as good as.

Every test here is a way the picture goes silently wrong. A missed `centered`
flag offsets half the scene by half a sprite; a missed parent rotation makes
children drift; drawing a whole sheet instead of one atlas region puts a
twelve-frame strip where a character belongs. None of those throw. They just
render something plausible and wrong, which is the worst thing a viewport can
do — so the maths is pinned rather than eyeballed.
"""
from __future__ import annotations


import pytest

from bgate_core import scenedraw


def _draw(text, *, sizes=None, reads=None, viewport=(640, 360)):
    sizes = sizes or {}
    reads = reads or {}
    return scenedraw.draw_list(
        text,
        read=lambda p: reads.get(p),
        size_of=lambda p: sizes.get(p),
        rel_of=lambda p: p.replace("res://", "game/") if p else None,
        viewport=viewport)


def _by_path(out):
    return {i["path"]: i for i in out["items"]}


HEADER = '[gd_scene load_steps=2 format=3]\n\n'


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------
def test_a_childs_position_is_relative_to_its_parent():
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="A" type="Node2D" parent="."]\nposition = Vector2(100, 50)\n\n'
             '[node name="B" type="Node2D" parent="A"]\nposition = Vector2(10, 5)\n')
    at = _by_path(_draw(scene))
    assert (at["A"]["x"], at["A"]["y"]) == (100, 50)
    assert (at["A/B"]["x"], at["A/B"]["y"]) == (110, 55)


def test_a_rotated_parent_carries_its_children_round_with_it():
    """The offset must be ROTATED by the parent before it is added. Skipping
    that is the classic drift bug — everything looks right until something is
    rotated, then children sit in the wrong place with no error anywhere."""
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="A" type="Node2D" parent="."]\nrotation = 1.5707963\n\n'
             '[node name="B" type="Node2D" parent="A"]\nposition = Vector2(10, 0)\n')
    b = _by_path(_draw(scene))["A/B"]
    assert b["x"] == pytest.approx(0, abs=1e-3)
    assert b["y"] == pytest.approx(10, abs=1e-3)


def test_a_scaled_parent_scales_its_childs_offset():
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="A" type="Node2D" parent="."]\nscale = Vector2(2, 3)\n\n'
             '[node name="B" type="Node2D" parent="A"]\nposition = Vector2(10, 10)\n')
    at = _by_path(_draw(scene))
    assert (at["A/B"]["x"], at["A/B"]["y"]) == (20, 30)
    assert (at["A/B"]["sx"], at["A/B"]["sy"]) == (2, 3)


def test_an_invisible_parent_hides_the_whole_subtree():
    """Godot hides descendants; showing them would be a viewport that disagrees
    with the game about what is on screen."""
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="A" type="Node2D" parent="."]\nvisible = false\n\n'
             '[node name="B" type="Sprite2D" parent="A"]\n')
    at = _by_path(_draw(scene))
    assert at["A"]["visible"] is False
    assert at["A/B"]["visible"] is False, "a child of a hidden node is hidden"


# ---------------------------------------------------------------------------
# Paint order
# ---------------------------------------------------------------------------
def test_z_index_orders_the_paint_and_ties_keep_declaration_order():
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="Back" type="Sprite2D" parent="."]\nz_index = -1\n\n'
             '[node name="Mid1" type="Sprite2D" parent="."]\n\n'
             '[node name="Mid2" type="Sprite2D" parent="."]\n\n'
             '[node name="Front" type="Sprite2D" parent="."]\nz_index = 5\n')
    order = [i["path"] for i in _draw(scene)["items"]]
    assert order.index("Back") < order.index("Mid1")
    assert order.index("Mid1") < order.index("Mid2"), "ties keep file order"
    assert order[-1] == "Front"


# ---------------------------------------------------------------------------
# What each node draws
# ---------------------------------------------------------------------------
def test_a_sprite_draws_its_whole_texture_and_is_centred():
    scene = (HEADER + '[ext_resource type="Texture2D" path="res://a/hero.png" id="1_h"]\n\n'
             '[node name="Root" type="Node2D"]\n\n'
             '[node name="S" type="Sprite2D" parent="."]\ntexture = ExtResource("1_h")\n')
    d = _by_path(_draw(scene, sizes={"res://a/hero.png": (64, 32)}))["S"]["draw"]
    assert d["kind"] == "image" and d["size"] == [64, 32]
    assert d["region"] == [0, 0, 64, 32]
    assert d["centered"] is True, "Sprite2D centres on its position by default"
    assert d["rel"] == "game/a/hero.png"


def test_centered_false_is_honoured():
    scene = (HEADER + '[ext_resource type="Texture2D" path="res://a/hero.png" id="1_h"]\n\n'
             '[node name="Root" type="Node2D"]\n\n'
             '[node name="S" type="Sprite2D" parent="."]\n'
             'texture = ExtResource("1_h")\ncentered = false\n')
    d = _by_path(_draw(scene, sizes={"res://a/hero.png": (64, 32)}))["S"]["draw"]
    assert d["centered"] is False


def test_an_animated_sprite_draws_ONE_frame_not_the_whole_sheet():
    """The single most visible way this can be wrong: a twelve-frame strip
    rendered where one character belongs."""
    frames = (
        '[gd_resource type="SpriteFrames" load_steps=3 format=3]\n\n'
        '[ext_resource type="Texture2D" path="res://a/sheet.png" id="1"]\n\n'
        '[sub_resource type="AtlasTexture" id="atlas_0"]\n'
        'atlas = ExtResource("1")\nregion = Rect2(0, 0, 96, 80)\n\n'
        '[sub_resource type="AtlasTexture" id="atlas_1"]\n'
        'atlas = ExtResource("1")\nregion = Rect2(96, 0, 96, 80)\n\n'
        '[resource]\nanimations = [{\n"frames": [{\n"duration": 1.0,\n'
        '"texture": SubResource("atlas_0")\n}],\n"loop": true,\n"name": &"idle"\n}]\n')
    scene = (HEADER + '[ext_resource type="SpriteFrames" path="res://a/f.tres" id="1_f"]\n\n'
             '[node name="Root" type="Node2D"]\n\n'
             '[node name="A" type="AnimatedSprite2D" parent="."]\n'
             'sprite_frames = ExtResource("1_f")\n')
    d = _by_path(_draw(scene, reads={"res://a/f.tres": frames}))["A"]["draw"]
    assert d["kind"] == "image"
    assert d["region"] == [0, 0, 96, 80], "one cell, not the strip"
    assert d["size"] == [96, 80]
    assert d["rel"] == "game/a/sheet.png"


def test_an_animated_sprite_with_nothing_assigned_says_so():
    """Most of these are assigned by a script at run time. Silence would read
    as a broken viewport rather than as the accurate answer it is."""
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="A" type="AnimatedSprite2D" parent="."]\n')
    d = _by_path(_draw(scene))["A"]["draw"]
    assert d["kind"] == "marker" and "SpriteFrames" in d["reason"]


def test_a_control_is_sized_by_its_anchor_offsets_not_a_size_property():
    """Placeholder art in these projects is mostly ColorRects; a viewport that
    cannot place them shows an empty stage."""
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="Bar" type="ColorRect" parent="."]\n'
             'offset_left = 12.0\noffset_top = 20.0\n'
             'offset_right = 112.0\noffset_bottom = 60.0\n'
             'color = Color(1, 0, 0, 1)\n')
    it = _by_path(_draw(scene))["Bar"]
    assert (it["x"], it["y"]) == (12, 20)
    assert it["draw"]["size"] == [100, 40]
    assert it["draw"]["color"] == [1.0, 0.0, 0.0, 1.0]
    assert it["draw"]["kind"] == "rect"


def test_a_camera_carries_the_viewport_rectangle():
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="Cam" type="Camera2D" parent="."]\nposition = Vector2(320, 180)\n')
    it = _by_path(_draw(scene, viewport=(640, 360)))["Cam"]
    assert it["draw"]["kind"] == "camera"
    assert (it["x"], it["y"]) == (320, 180)


def test_bodies_and_bare_nodes_are_still_listed_so_they_can_be_moved():
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="Body" type="CharacterBody2D" parent="."]\n\n'
             '[node name="Shape" type="CollisionShape2D" parent="Body"]\n')
    at = _by_path(_draw(scene))
    assert at["Body"]["draw"]["kind"] == "body"
    assert at["Body/Shape"]["draw"]["kind"] == "body"


# ---------------------------------------------------------------------------
# SpriteFrames parsing
# ---------------------------------------------------------------------------
def test_first_frame_returns_none_for_anything_it_cannot_read():
    assert scenedraw.first_frame("") is None
    assert scenedraw.first_frame("[gd_resource type=\"TileSet\"]") is None


def test_first_frame_follows_the_animation_not_just_the_first_atlas():
    """The first animation's first frame is what an unplayed AnimatedSprite2D
    shows — not whichever AtlasTexture happens to be declared first."""
    tres = (
        '[gd_resource type="SpriteFrames" format=3]\n\n'
        '[ext_resource type="Texture2D" path="res://s.png" id="1"]\n\n'
        '[sub_resource type="AtlasTexture" id="atlas_0"]\n'
        'atlas = ExtResource("1")\nregion = Rect2(0, 0, 32, 32)\n\n'
        '[sub_resource type="AtlasTexture" id="atlas_1"]\n'
        'atlas = ExtResource("1")\nregion = Rect2(32, 0, 32, 32)\n\n'
        '[resource]\nanimations = [{\n"frames": [{\n'
        '"texture": SubResource("atlas_1")\n}],\n"name": &"walk"\n}]\n')
    got = scenedraw.first_frame(tres)
    assert got == {"sheet": "res://s.png", "region": [32.0, 0.0, 32.0, 32.0]}


# ---------------------------------------------------------------------------
# Instanced scenes
# ---------------------------------------------------------------------------
# A scene built out of individual, editable components is built out of instanced
# children — and an instanced child carries no `type=`, so every branch in the
# resolver used to fall through and it drew as a bare, unlabelled dot. Forty
# desks placed that way rendered as an empty frame.
PROP = (
    '[gd_scene load_steps=2 format=3]\n\n'
    '[ext_resource type="Texture2D" path="res://desk.png" id="1_d"]\n\n'
    '[node name="Prop" type="Node2D"]\n\n'
    '[node name="Art" type="Sprite2D" parent="."]\n'
    'position = Vector2(0, -8)\ntexture = ExtResource("1_d")\n')

FLOOR = (
    '[gd_scene load_steps=2 format=3]\n\n'
    '[ext_resource type="PackedScene" path="res://prop.tscn" id="1_p"]\n\n'
    '[node name="Floor" type="Node2D"]\n\n'
    '[node name="Characters" type="Node2D" parent="."]\n\n'
    '[node name="Desk_01" parent="Characters" instance=ExtResource("1_p")]\n'
    'position = Vector2(100, 50)\n')


def _floor():
    return _draw(FLOOR, sizes={"res://desk.png": (32, 32)},
                 reads={"res://prop.tscn": PROP})


def test_an_instanced_scene_brings_its_art_with_it():
    """Without this the desk is an invisible marker: the art it draws lives in
    another file, so nothing in THIS scene's text says there is a picture."""
    at = _by_path(_floor())
    art = at["Characters/Desk_01/Art"]
    assert art["draw"]["kind"] == "image"
    assert art["draw"]["rel"] == "game/desk.png"
    # 100,50 from the instance + 0,-8 from inside prop.tscn.
    assert (art["x"], art["y"]) == (100, 42)


def test_an_instances_insides_are_marked_as_belonging_to_it():
    """`of` is what lets a click on the sprite select the instance, the way
    Godot does — there is no line in this file for prop.tscn's own nodes."""
    at = _by_path(_floor())
    assert at["Characters/Desk_01/Art"]["of"] == "Characters/Desk_01"
    assert "of" not in at["Characters/Desk_01"]
    assert at["Characters/Desk_01"]["instance"] == "res://prop.tscn"


def test_an_instance_whose_insides_all_draw_nothing_still_says_so():
    """The normal case in these projects: prop.tscn's sprite gets its texture
    from a script at load. Opening it must not turn "blank, and here is why"
    into a node that silently draws nothing and is reported nowhere."""
    blank = ('[gd_scene format=3]\n\n[node name="Prop" type="Node2D"]\n\n'
             '[node name="Art" type="Sprite2D" parent="."]\n')
    at = _by_path(_draw(FLOOR, reads={"res://prop.tscn": blank}))
    desk = at["Characters/Desk_01"]
    assert desk["drawn"] == 0
    assert "nothing in it draws" in desk["draw"]["reason"]
    assert _by_path(_floor())["Characters/Desk_01"]["drawn"] == 1


def test_an_instance_that_cannot_be_read_says_which_one():
    at = _by_path(_draw(FLOOR))          # no `reads`, so prop.tscn is missing
    draw = at["Characters/Desk_01"]["draw"]
    assert draw["kind"] == "marker"
    assert "prop.tscn" in draw["reason"]


def test_a_scene_that_instances_itself_stops_instead_of_recursing():
    """A cycle here is an infinite walk, and the file that causes it is a file
    someone can save by accident. It opens once — that is what the engine would
    show — and the copy inside the copy says why it went no further."""
    loop = (
        '[gd_scene load_steps=2 format=3]\n\n'
        '[ext_resource type="PackedScene" path="res://loop.tscn" id="1_l"]\n\n'
        '[node name="Loop" type="Node2D"]\n\n'
        '[node name="Again" parent="." instance=ExtResource("1_l")]\n')
    at = _by_path(_draw(loop, reads={"res://loop.tscn": loop}))
    assert "itself" in at["Again/Again"]["draw"]["reason"]
    assert "Again/Again/Again" not in at


def test_an_instances_nodes_paint_after_the_instance_not_at_the_end():
    """Order decides paint order for everything sharing z=0. Appending the
    insides at the end of the list would put every prop's art on top of every
    wall, whatever the file says."""
    order = [i["path"] for i in _floor()["items"]]
    assert order.index("Characters/Desk_01/Art") == \
        order.index("Characters/Desk_01") + 1


# ---------------------------------------------------------------------------
# Viewport
# ---------------------------------------------------------------------------
def test_the_viewport_comes_from_the_project_not_a_guess():
    text = ("[display]\n\nwindow/size/viewport_width=640\n"
            "window/size/viewport_height=360\n")
    assert scenedraw.viewport_of(text) == (640, 360)
    assert scenedraw.viewport_of("") == scenedraw.DEFAULT_VIEWPORT
