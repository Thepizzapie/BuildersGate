extends CharacterBody3D
## Third-person character: camera-relative movement, turns to face where it
## goes, jump with coyote time, sprint, an interaction probe. The tunables a
## playtester complains about are exported AND emitted, so a vibe lands next to
## its numbers. Pair with camera_rig.gd (a SIBLING, never a child - see there).
##
## Camera-relative means: the camera's yaw decides what "forward" is. With no
## current camera the body's own basis is used, so a headless run still moves.

@export_group("Movement")
@export var walk_speed := 4.0
@export var sprint_speed := 7.0
@export var ground_accel := 24.0
@export var ground_decel := 30.0
@export var air_control := 0.35
## Radians per second the body turns toward its travel direction.
@export var turn_rate := 10.0

@export_group("Jump")
@export var jump_velocity := 5.2
@export var gravity := 16.0
## Falling faster than rising is the main lever on "floaty".
@export var fall_multiplier := 1.6
@export var coyote_time := 0.10
@export var jump_buffer_time := 0.10

@export_group("Interaction")
## Nodes in this group with an `interact(actor)` method are what `interact` hits.
@export var interactable_group := &"interactable"

var _coyote := 0.0
var _jump_buffer := 0.0
var _was_on_floor := false
var _air_start_ms := 0
var _air_start_y := 0.0
var _peak_y := 0.0
var _air_cause := "spawn"
var _facing := 0.0

@onready var _range: Area3D = get_node_or_null("InteractRange") as Area3D


func _ready() -> void:
	add_to_group(&"player")
	_facing = rotation.y
	_begin_air("spawn")


func _begin_air(cause: String) -> void:
	_air_cause = cause
	_air_start_ms = Time.get_ticks_msec()
	_air_start_y = global_position.y
	_peak_y = global_position.y


func _camera_basis() -> Basis:
	var cam := get_viewport().get_camera_3d()
	if cam == null:
		return global_transform.basis
	# Yaw only: a camera looking down must not make "forward" mean "into the floor".
	var f := -cam.global_transform.basis.z
	f.y = 0.0
	if f.length_squared() < 1e-6:
		return global_transform.basis
	f = f.normalized()
	return Basis(f.cross(Vector3.UP).normalized(), Vector3.UP, -f)


func _physics_process(delta: float) -> void:
	var on_floor := is_on_floor()
	if on_floor:
		_coyote = coyote_time
	else:
		_coyote = maxf(_coyote - delta, 0.0)
		_peak_y = maxf(_peak_y, global_position.y)
		velocity.y -= gravity * delta * (fall_multiplier if velocity.y < 0.0 else 1.0)

	if Input.is_action_just_pressed("jump"):
		_jump_buffer = jump_buffer_time
	else:
		_jump_buffer = maxf(_jump_buffer - delta, 0.0)
	if _jump_buffer > 0.0 and _coyote > 0.0:
		_jump_buffer = 0.0
		_coyote = 0.0
		_begin_air("jump")
		velocity.y = jump_velocity
		BGateTelemetry.emit_event("jump", {
			"jump_velocity": jump_velocity, "gravity": gravity,
			"fall_multiplier": fall_multiplier, "coyote_time": coyote_time,
			"from_coyote": not on_floor,
		})

	var input := Input.get_vector("move_left", "move_right", "move_forward", "move_back")
	var basis := _camera_basis()
	var wish := (basis.x * input.x + basis.z * input.y)
	wish.y = 0.0
	var sprinting := Input.is_action_pressed("sprint")
	var top := sprint_speed if sprinting else walk_speed
	var target := wish.normalized() * top * minf(wish.length(), 1.0) if wish.length_squared() > 1e-6 else Vector3.ZERO
	var rate := (ground_accel if target.length_squared() > 0.0 else ground_decel)
	if not on_floor:
		rate *= air_control
	var planar := Vector3(velocity.x, 0.0, velocity.z).move_toward(target, rate * delta)
	velocity.x = planar.x
	velocity.z = planar.z

	# Face the way we travel, at a bounded rate, so the body reads as walking
	# rather than sliding. Idle keeps the last facing.
	if planar.length_squared() > 0.04:
		var want := atan2(-planar.x, -planar.z)
		_facing = lerp_angle(_facing, want, minf(1.0, turn_rate * delta))
		rotation.y = _facing

	move_and_slide()

	var now_on_floor := is_on_floor()
	if _was_on_floor and not now_on_floor and _air_cause == "":
		_begin_air("fall")
	if now_on_floor and not _was_on_floor:
		BGateTelemetry.emit_event("land", {
			"air_time": float(Time.get_ticks_msec() - _air_start_ms) / 1000.0,
			"peak_height": absf(_peak_y - _air_start_y),
			"fall_distance": absf(_peak_y - global_position.y),
			"cause": _air_cause,
		})
		_air_cause = ""
	_was_on_floor = now_on_floor

	if Input.is_action_just_pressed("interact"):
		_interact()


## The nearest interactable inside InteractRange gets `interact(self)`.
func _interact() -> void:
	if _range == null:
		return
	var best: Node = null
	var best_d := INF
	for area in _range.get_overlapping_areas():
		if not area.is_in_group(interactable_group) or not area.has_method("interact"):
			continue
		var d := global_position.distance_squared_to(area.global_position)
		if d < best_d:
			best_d = d
			best = area
	if best != null:
		best.call("interact", self)
		BGateTelemetry.emit_event("interact", {"target": String(best.name)})


## THE TRAVERSAL CONTRACT. traversal_prove refuses any controller that cannot
## say whether a scripted move (mantle, vault, cutscene) owns the body right
## now. This controller has none; a project that adds one returns true for its
## duration.
func is_in_scripted_move() -> bool:
	return false
