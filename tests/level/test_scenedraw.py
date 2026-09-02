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

from bgate_core.level import scenedraw


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
# Overrides on nodes inside an instance
# ---------------------------------------------------------------------------
# THE REASON A DRESSED FLOOR RENDERED AS FIVE HUNDRED CROSSES.
#
# Opening the instance was never the gap — the tests above prove that has
# always worked. The gap is that in these projects the instanced scene is a
# BLANK: prop.tscn ships an empty Sprite2D called Art, and which picture it
# wears is decided by the HOST file as a Godot override block with no `type=`.
# Read separately both files are correct and neither draws. Godot composes
# them; so must this, or the endpoint reports "nothing in it draws in the file"
# — true, and useless — for every prop on the floor.
BLANK_PROP = (
    '[gd_scene format=3]\n\n'
    '[node name="Prop" type="Node2D"]\n\n'
    '[node name="Art" type="Sprite2D" parent="."]\n')

DRESSED = (
    '[gd_scene load_steps=3 format=3]\n\n'
    '[ext_resource type="PackedScene" path="res://prop.tscn" id="1_p"]\n'
    '[ext_resource type="Texture2D" path="res://cabinet.png" id="2_c"]\n\n'
    '[node name="Floor" type="Node2D"]\n\n'
    '[node name="Cab_00" parent="." instance=ExtResource("1_p")]\n'
    'position = Vector2(64, 64)\n\n'
    '[node name="Art" parent="Cab_00" index="0"]\n'
    'texture = ExtResource("2_c")\n'
    'offset = Vector2(-13.5, -30)\n')


def _dressed(**kw):
    return _draw(DRESSED, sizes={"res://cabinet.png": (32, 64)},
                 reads={"res://prop.tscn": BLANK_PROP}, **kw)


def test_an_override_dresses_the_node_inside_the_instance():
    at = _by_path(_dressed())
    art = at["Cab_00/Art"]
    assert art["draw"]["kind"] == "image", art["draw"]
    assert art["draw"]["rel"] == "game/cabinet.png"
    assert art["draw"]["offset"] == [-13.5, -30.0]
    assert at["Cab_00"]["drawn"] == 1
    assert "nothing in it draws" not in at["Cab_00"]["draw"].get("reason", "")


def test_an_override_block_is_a_patch_not_a_second_node():
    """It has no type, so emitting it too produced a second item on the same
    path that drew nothing — and the viewport hit-tested against whichever of
    the two it reached first."""
    paths = [i["path"] for i in _dressed()["items"]]
    assert paths.count("Cab_00/Art") == 1
    # ...and it is not counted as a child of the instance either.
    assert _by_path(_dressed())["Cab_00"]["children"] == 1


def test_one_props_override_does_not_leak_onto_the_next():
    """The parsed source scene is cached per request and the patch is written
    into it, so a shared copy would put the first cabinet's texture on all of
    them. The cache has to hand out its own copy."""
    two = DRESSED + (
        '\n[node name="Cab_01" parent="." instance=ExtResource("1_p")]\n'
        'position = Vector2(128, 64)\n')
    at = _by_path(_draw(two, sizes={"res://cabinet.png": (32, 64)},
                        reads={"res://prop.tscn": BLANK_PROP}))
    assert at["Cab_00/Art"]["draw"]["kind"] == "image"
    assert at["Cab_01/Art"]["draw"]["kind"] == "marker", \
        "Cab_01 has no override of its own and must stay undressed"


def test_the_instanced_scene_is_parsed_once_however_many_instances():
    """281 props over three .tscn files. This endpoint exists so that panning
    is not a network operation; it must not become a disk one."""
    reads: list[str] = []

    def read(path):
        reads.append(path)
        return {"res://prop.tscn": BLANK_PROP}.get(path)

    many = DRESSED + "".join(
        f'\n[node name="Cab_{n:02d}" parent="." instance=ExtResource("1_p")]\n'
        f'position = Vector2({n * 32}, 64)\n' for n in range(1, 30))
    scenedraw.draw_list(many, read=read, size_of=lambda p: (32, 64),
                        rel_of=lambda p: p.replace("res://", "game/"))
    assert reads.count("res://prop.tscn") == 1, reads


def test_a_typed_node_added_under_an_instance_stays_a_node():
    """Godot lets you add a NEW child beside an instance's own contents, and
    that block does carry a type. Only the typeless ones are patches."""
    added = DRESSED + (
        '\n[node name="Glow" type="Sprite2D" parent="Cab_00"]\n'
        'texture = ExtResource("2_c")\n')
    at = _by_path(_draw(added, sizes={"res://cabinet.png": (32, 64)},
                        reads={"res://prop.tscn": BLANK_PROP}))
    assert at["Cab_00/Glow"]["draw"]["kind"] == "image"
    assert "of" not in at["Cab_00/Glow"], "it lives in THIS file and is editable"


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------
# A floor lit warm over the bullpen and cold over the server aisle rendered as
# one even grey, because the viewport drew albedo and nothing else. The whole
# difference on this project is one CanvasModulate over ~44 point lights.
LIGHT_SCENE = (
    '[gd_scene load_steps=2 format=3]\n\n'
    '[ext_resource type="Texture2D" path="res://cookie.tres" id="1_c"]\n\n'
    '[node name="Fluoro" type="PointLight2D"]\n'
    'scale = Vector2(1, 0.5)\n'
    'color = Color(0.8, 0.93, 0.86, 1)\n'
    'energy = 1.45\n'
    'texture = ExtResource("1_c")\n'
    'texture_scale = 1.55\n')

COOKIE = (
    '[gd_resource type="GradientTexture2D" load_steps=2 format=3]\n\n'
    '[sub_resource type="Gradient" id="g"]\n'
    'offsets = PackedFloat32Array(0, 0.62, 1)\n'
    'colors = PackedColorArray(1, 1, 1, 1, 1, 1, 1, 0.3, 1, 1, 1, 0)\n\n'
    '[resource]\ngradient = SubResource("g")\nwidth = 256\nheight = 256\n'
    'fill = 1\n')


def test_a_canvas_modulate_rides_on_the_payload_not_a_node():
    """It multiplies the whole canvas, so it is not any one node's rectangle —
    and a preview that drops it is a preview of a different scene."""
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="Night" type="CanvasModulate" parent="."]\n'
             'color = Color(0.3, 0.33, 0.4, 1)\n')
    out = _draw(scene)
    assert out["tint"] == [0.3, 0.33, 0.4, 1.0]
    assert _by_path(out)["Night"]["draw"]["kind"] == "tint"


def test_an_invisible_canvas_modulate_does_not_tint():
    scene = (HEADER + '[node name="Root" type="Node2D"]\n\n'
             '[node name="Night" type="CanvasModulate" parent="."]\n'
             'visible = false\ncolor = Color(0.3, 0.33, 0.4, 1)\n')
    assert _draw(scene)["tint"] is None


def test_a_light_cookie_is_a_gradient_resource_not_an_image():
    """Every fixture in these projects points at a GradientTexture2D .tres —
    there is no file for the browser to fetch, which is why 44 lit rooms
    reported `no light texture assigned`. Send the ramp instead."""
    out = _draw(LIGHT_SCENE, reads={"res://cookie.tres": COOKIE})
    d = _by_path(out)["."]["draw"]
    assert d["kind"] == "light"
    assert d["gradient"] == [[0.0, 1.0], [0.62, 0.3], [1.0, 0.0]]
    assert d["size"] == [256, 256]
    assert d["color"] == [0.8, 0.93, 0.86, 1.0]
    assert d["energy"] == 1.45 and d["scale"] == 1.55


def test_a_light_with_an_unreadable_cookie_is_still_a_light():
    d = _by_path(_draw(LIGHT_SCENE))["."]["draw"]
    assert d["kind"] == "light" and d["gradient"], \
        "a fixture must not vanish because its texture is a format we cannot read"


def test_an_instanced_lights_own_squash_survives_the_instance():
    """light_fluoro_panel.tscn sets scale.y = 0.5 so the pool lands on the
    isometric floor plane instead of reading as a sphere in the air. That
    scale belongs to the PICTURE, not to the node in the host file — folding it
    into the host would write it into the parent .tscn the first time anyone
    nudged the light."""
    floor = (
        '[gd_scene load_steps=2 format=3]\n\n'
        '[ext_resource type="PackedScene" path="res://fluoro.tscn" id="1_f"]\n\n'
        '[node name="Floor" type="Node2D"]\n\n'
        '[node name="Lamp" parent="." instance=ExtResource("1_f")]\n'
        'position = Vector2(128, 140)\n')
    at = _by_path(_draw(floor, reads={"res://fluoro.tscn": LIGHT_SCENE,
                                      "res://cookie.tres": COOKIE}))
    lamp = at["Lamp"]
    assert lamp["draw"]["kind"] == "light"
    assert lamp["draw"]["local"]["sy"] == 0.5
    assert (lamp["sx"], lamp["sy"]) == (1.0, 1.0), \
        "the host node's own transform is what a drag reads and writes"


# ---------------------------------------------------------------------------
# Viewport
# ---------------------------------------------------------------------------
def test_the_viewport_comes_from_the_project_not_a_guess():
    text = ("[display]\n\nwindow/size/viewport_width=640\n"
            "window/size/viewport_height=360\n")
    assert scenedraw.viewport_of(text) == (640, 360)
    assert scenedraw.viewport_of("") == scenedraw.DEFAULT_VIEWPORT


# ---------------------------------------------------------------------------
# Content scale — the factor between a world pixel and a pixel the player sees
# ---------------------------------------------------------------------------
# THIS IS THE NUMBER THAT MADE ATLAS LOOK WRONG THREE TIMES RUNNING, and the
# measurement below is why the module is confident about it rather than
# reasoning about it. `godot --resolution 1280x720 scenes/floor_tut.tscn` was
# captured, the viewport was rendered over the same 640x360 world rectangle at
# this factor, and the two frames were registered against each other:
#
#   frame-to-frame registration        scale 1.000, offset (-1, 0) px
#   floor lattice, both captures       128 x 64 screen px  (64x32 world at x2)
#   DeskCrtSE_028, both captures       93 x 83 px          ratio 1.0000
#   OfficeChairSE_007/_027             ratio 0.983 / 1.035 (one 0.01 sweep step)
#
# So the geometry was never wrong. What differs between the panel and the
# engine is one global factor, and these pin where it comes from.
DOWNSIZING_PROJECT = (
    "[display]\n\n"
    "window/size/viewport_width=640\n"
    "window/size/viewport_height=360\n"
    "window/size/window_width_override=1280\n"
    "window/size/window_height_override=720\n"
    'window/stretch/mode="canvas_items"\n'
    'window/stretch/scale_mode="integer"\n')


def test_a_stretched_project_presents_every_canvas_item_at_the_window_ratio():
    assert scenedraw.content_scale(DOWNSIZING_PROJECT) == 2.0


def test_a_project_that_does_not_stretch_presents_one_to_one():
    for mode in ('"disabled"', '"viewport"'):
        text = DOWNSIZING_PROJECT.replace('"canvas_items"', mode)
        assert scenedraw.content_scale(text) == 1.0, \
            "only canvas_items scales the DRAWING; viewport renders small " \
            "and blits, which does not change a canvas item's size"


def test_the_old_2d_alias_still_counts_as_canvas_items():
    text = DOWNSIZING_PROJECT.replace('"canvas_items"', '"2d"')
    assert scenedraw.content_scale(text) == 2.0


def test_integer_scale_mode_floors_the_factor():
    """A 1000x700 window over a 640x360 stage is 1.56x of room and exactly 1x
    of picture — `integer` refuses the fraction and pillarboxes the rest. A
    viewport reporting 1.56 would be reporting a size nothing is drawn at."""
    text = (DOWNSIZING_PROJECT
            .replace("window_width_override=1280", "window_width_override=1000")
            .replace("window_height_override=720", "window_height_override=700"))
    assert scenedraw.content_scale(text) == 1.0
    loose = text.replace('scale_mode="integer"', 'scale_mode="fractional"')
    assert scenedraw.content_scale(loose) == pytest.approx(1.5625)


def test_no_window_override_means_no_stretch():
    text = ("[display]\n\nwindow/size/viewport_width=640\n"
            "window/size/viewport_height=360\n"
            'window/stretch/mode="canvas_items"\n')
    assert scenedraw.content_scale(text) == 1.0


def test_an_empty_or_unreadable_project_file_is_one_not_a_crash():
    assert scenedraw.content_scale("") == 1.0
    assert scenedraw.content_scale("garbage = [") == 1.0


def test_the_draw_list_carries_the_scale_so_the_panel_can_name_it():
    """Without this key the viewport cannot tell the game's 100% from the
    editor's, and says "100%" while showing half the size the player sees —
    which is the whole reason the panel was reported as mis-scaling props."""
    out = scenedraw.draw_list(
        HEADER + '[node name="Root" type="Node2D"]\n',
        read=lambda p: None, size_of=lambda p: None, rel_of=lambda p: None,
        viewport=(640, 360), scale=2.0)
    assert out["scale"] == 2.0
    bare = _draw(HEADER + '[node name="Root" type="Node2D"]\n')
    assert bare["scale"] == 1.0, "a project that does not stretch is 1, not 0"
