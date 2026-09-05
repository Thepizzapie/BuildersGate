extends Node3D
## Third-person camera rig: this node is the PIVOT, the SpringArm3D child sets
## distance and pulls in against walls, the Camera3D rides on the arm.
##
## A SIBLING OF THE TARGET, NEVER A CHILD. A camera parented to the body
## inherits every jitter, turn and physics correction the body makes, so the
## lag and the smoothing have nothing to lag behind (Corniche, bible 9). This
## rig owns its own world transform and follows.
##
## Three modes:
##   ORBIT  - mouse / right stick turns the rig; the body moves relative to it.
##   FOLLOW - the rig swings behind the target's facing; stick nudges it.
##   FIXED  - the rig holds its authored yaw/pitch (top-down, isometric, side).
## `camera_toggle` cycles them at runtime so a playtester can compare.

enum Mode { ORBIT, FOLLOW, FIXED }

## The node to follow. Empty takes the first node in the "player" group.
@export var target_path: NodePath
@export var mode: Mode = Mode.ORBIT
## Height above the target's origin the rig pivots at.
@export var pivot_height := 1.4
## Seconds to close ~63% of a position error. Smooths the OFFSET, so the
## camera never falls behind at speed (see chase_camera.gd for why).
@export var position_lag := 0.08
@export var mouse_sensitivity := 0.003
@export var stick_speed := 2.5
@export var pitch_min_deg := -60.0
@export var pitch_max_deg := 45.0
## FOLLOW: seconds for the yaw to swing behind the target's facing.
@export var follow_lag := 0.6
@export var capture_mouse := true

var _target: Node3D
var _yaw := 0.0
var _pitch := -0.3
var _pos := Vector3.ZERO

@onready var _arm: SpringArm3D = get_node_or_null("SpringArm3D") as SpringArm3D


func _ready() -> void:
	_yaw = rotation.y
	_pitch = rotation.x
	_resolve_target()
	if _target != null:
		_pos = _target.global_position + Vector3(0, pivot_height, 0)
		global_position = _pos
	# The arm must not collide with the body it is looking at.
	if _arm != null and _target is CollisionObject3D:
		_arm.add_excluded_object((_target as CollisionObject3D).get_rid())
	if capture_mouse and mode == Mode.ORBIT and DisplayServer.get_name() != "headless":
		Input.mouse_mode = Input.MOUSE_MODE_CAPTURED


func _resolve_target() -> void:
	if target_path != NodePath(""):
		_target = get_node_or_null(target_path) as Node3D
	if _target == null:
		var found := get_tree().get_first_node_in_group(&"player")
		_target = found as Node3D


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion and mode != Mode.FIXED and Input.mouse_mode == Input.MOUSE_MODE_CAPTURED:
		_yaw -= event.relative.x * mouse_sensitivity
		_pitch = clampf(_pitch - event.relative.y * mouse_sensitivity,
			deg_to_rad(pitch_min_deg), deg_to_rad(pitch_max_deg))
	if event.is_action_pressed("camera_toggle"):
		mode = ((mode + 1) % 3) as Mode
		BGateTelemetry.emit_event("camera_mode", {"mode": Mode.keys()[mode]})
	if event.is_action_pressed("ui_cancel"):
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE


func _physics_process(delta: float) -> void:
	if _target == null:
		_resolve_target()
		if _target == null:
			return
	var look := Input.get_vector("look_left", "look_right", "look_up", "look_down") 		if InputMap.has_action("look_left") else Vector2.ZERO
	match mode:
		Mode.ORBIT:
			_yaw -= look.x * stick_speed * delta
			_pitch = clampf(_pitch - look.y * stick_speed * delta,
				deg_to_rad(pitch_min_deg), deg_to_rad(pitch_max_deg))
		Mode.FOLLOW:
			var want := _target.rotation.y
			var k := 1.0 - exp(-delta / maxf(follow_lag, 0.001))
			_yaw = lerp_angle(_yaw, want, k) - look.x * stick_speed * delta
		Mode.FIXED:
			pass
	var want_pos := _target.global_position + Vector3(0, pivot_height, 0)
	var k := 1.0 - exp(-delta / maxf(position_lag, 0.001))
	_pos = _pos.lerp(want_pos, k)
	global_position = _pos
	rotation = Vector3(_pitch, _yaw, 0.0)
