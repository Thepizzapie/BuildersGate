"""The traversal driver holds an input block the way a thumb does.

THE BUG THIS FILE EXISTS FOR (EXIT 67 r2, item #5, three attempts and $14):
the driver released every action and pressed the block's actions again on
every physics frame. Godot 4.4 buffers Input.action_press/release a frame,
so a controller polling is_action_just_pressed saw the jump only when the
block ENDED - two frames late, on the wrong ground - and a six-block program
read as "only the first two applied". Three agents diagnosed the controller,
the level geometry and finally the harness; the director's escalation was
right and this is the test that would have caught it in an afternoon.

The in-engine test builds the smallest project that can express the bug: a
floor with a gap, a polled-input controller with the busy query, and a
destination Area2D on the far side. The route is three blocks (run, run+jump,
run). With hold semantics the jump fires INSIDE its block and the body lands
in the destination; with the old re-press-every-frame driver it fired after
the block and fell in the gap.
"""
from __future__ import annotations

import json

import pytest

from bgate_adapters import godot
from bgate_core.level import traversal

needs_godot = pytest.mark.skipif(
    not godot.available()["available"], reason="Godot not installed")

PLAYER_GD = """extends CharacterBody2D
const SPEED := 260.0
const JUMP := -520.0
const GRAVITY := 1400.0

func _physics_process(delta: float) -> void:
\tif not is_on_floor():
\t\tvelocity.y += GRAVITY * delta
\telif Input.is_action_just_pressed("jump"):
\t\tvelocity.y = JUMP
\tvelocity.x = Input.get_axis("move_left", "move_right") * SPEED
\tmove_and_slide()

# THE TRAVERSAL CONTRACT: no scripted moves here, so the honest answer is a
# constant - which is exactly what the r2 player returned when three agents
# read it as the bug.
func is_in_scripted_move() -> bool:
\treturn false
"""

# A floor from x=0..300, a 120 px gap, a floor from x=420..900 with the
# destination volume on it. The player (32x48) starts on a marker at x=40.
SCENE_TSCN = """[gd_scene load_steps=6 format=3]

[ext_resource type="Script" path="res://player.gd" id="1_p"]

[sub_resource type="RectangleShape2D" id="floor_a"]
size = Vector2(300, 32)

[sub_resource type="RectangleShape2D" id="floor_b"]
size = Vector2(480, 32)

[sub_resource type="RectangleShape2D" id="body"]
size = Vector2(32, 48)

[sub_resource type="RectangleShape2D" id="goal"]
size = Vector2(120, 80)

[node name="Gym" type="Node2D"]

[node name="FloorA" type="StaticBody2D" parent="."]
position = Vector2(150, 316)

[node name="Shape" type="CollisionShape2D" parent="FloorA"]
shape = SubResource("floor_a")

[node name="FloorB" type="StaticBody2D" parent="."]
position = Vector2(660, 316)

[node name="Shape" type="CollisionShape2D" parent="FloorB"]
shape = SubResource("floor_b")

[node name="Start" type="Marker2D" parent="."]
position = Vector2(40, 276)

[node name="Goal" type="Area2D" parent="."]
position = Vector2(800, 260)

[node name="Shape" type="CollisionShape2D" parent="Goal"]
shape = SubResource("goal")

[node name="Player" type="CharacterBody2D" parent="." groups=["player"]]
position = Vector2(40, 276)
script = ExtResource("1_p")

[node name="Shape" type="CollisionShape2D" parent="Player"]
shape = SubResource("body")
"""

PROJECT_GODOT = """config_version=5

[application]

config/name="traversal_gym"

[input]

move_left={
"deadzone": 0.5,
"events": [Object(InputEventKey,"resource_local_to_scene":false,"resource_name":"","device":-1,"window_id":0,"alt_pressed":false,"shift_pressed":false,"ctrl_pressed":false,"meta_pressed":false,"pressed":false,"keycode":0,"physical_keycode":65,"key_label":0,"unicode":97,"location":0,"echo":false,"script":null)
]
}
move_right={
"deadzone": 0.5,
"events": [Object(InputEventKey,"resource_local_to_scene":false,"resource_name":"","device":-1,"window_id":0,"alt_pressed":false,"shift_pressed":false,"ctrl_pressed":false,"meta_pressed":false,"pressed":false,"keycode":0,"physical_keycode":68,"key_label":0,"unicode":100,"location":0,"echo":false,"script":null)
]
}
jump={
"deadzone": 0.5,
"events": [Object(InputEventKey,"resource_local_to_scene":false,"resource_name":"","device":-1,"window_id":0,"alt_pressed":false,"shift_pressed":false,"ctrl_pressed":false,"meta_pressed":false,"pressed":false,"keycode":0,"physical_keycode":32,"key_label":0,"unicode":32,"location":0,"echo":false,"script":null)
]
}
"""


@pytest.fixture
def gym(tmp_path):
    root = tmp_path / "gym"
    root.mkdir()
    (root / "project.godot").write_text(PROJECT_GODOT, encoding="utf-8")
    (root / "player.gd").write_text(PLAYER_GD, encoding="utf-8")
    (root / "gym.tscn").write_text(SCENE_TSCN, encoding="utf-8")
    (root / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>",
                                   encoding="utf-8")
    return root


def _route(inputs):
    return traversal.route(name="gap", scene="res://gym.tscn",
                           launch="Gym/Start", destination="Gym/Goal",
                           inputs=inputs)


# ---------------------------------------------------------------------------
# The driver text: what a block does at its edges
# ---------------------------------------------------------------------------

def test_the_driver_presses_once_per_block_and_releases_once():
    src = traversal.driver_source(_route([{"action": "move_right", "frames": 10}]),
                                  "out.json")
    assert "_set_held(" in src
    assert "if _step_frame == 0:" in src, "a block presses when it starts, not every frame"
    # The old shape: release everything, re-press everything, every frame.
    assert "\t_release_all()\n\tif _step >= SPEC" not in src


def test_a_press_reaches_the_event_path_too():
    src = traversal.driver_source(_route([{"action": "jump", "frames": 1}]), "o.json")
    assert "InputEventAction.new()" in src and "parse_input_event" in src


def test_samples_carry_the_step_and_the_position():
    src = traversal.driver_source(_route([{"action": "jump", "frames": 1}]), "o.json")
    assert '"step": _step' in src and '"pos":' in src


# ---------------------------------------------------------------------------
# The engine's verdict
# ---------------------------------------------------------------------------

@needs_godot
def test_a_run_jump_run_route_lands_in_the_goal_with_the_jump_inside_its_block(gym):
    spec = _route([{"action": "move_right", "frames": 40},
                   {"actions": ["move_right", "jump"], "frames": 6},
                   {"action": "move_right", "frames": 200}])
    out = gym / "out.json"
    ran = godot.run_script(traversal.driver_source(spec, out.as_posix()),
                           project_dir=str(gym), timeout=180)
    assert out.is_file(), ran.get("stdout", "")[-1500:]
    payload = json.loads(out.read_text(encoding="utf-8"))
    samples = payload["samples"]
    assert samples, payload
    # Every step of the program was reached: the trace names all three.
    assert {s["step"] for s in samples} >= {0, 1, 2}, sorted({s["step"] for s in samples})
    # The jump fired INSIDE block 1: the first airborne RUN long enough to be
    # a jump (a single not-grounded frame is the settle bounce off the launch
    # surface) starts while step 1 is held, not after it ended.
    runs, current = [], []
    for sample in samples:
        if not sample["grounded"]:
            current.append(sample)
        elif current:
            runs.append(current)
            current = []
    # Runs that begin in the first frames are the drop onto the launch
    # surface, not a jump; the program's first block is 40 frames of running.
    jumps = [r for r in runs if len(r) >= 4 and r[0]["frame"] > 20]
    assert jumps, "the body never left the ground for a jump's worth of frames"
    assert jumps[0][0]["step"] == 1, jumps[0][0]
    verdict = traversal.verdict(spec, payload)
    assert verdict["ok"] is True, verdict
    assert payload["frames"] < spec["max_frames"], "the driver stopped when the route was proven"
