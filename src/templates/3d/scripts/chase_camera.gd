extends Camera3D
## Chase camera for vehicle_controller.gd. A SIBLING of the car, never a child:
## parented, the lag and the snap-back have nothing to lag behind. Runs in
## _physics_process because the car does; stepping on the render frame while
## the target moves on the physics frame is what makes a chase camera shimmer.
##
## It follows a HEADING, not the car's nose: the velocity direction while
## moving, the car's forward when stopped, so a parked car cannot make the
## camera orbit and a sideways car is framed along the way it travels. It
## smooths the OFFSET from the car rather than a world position - smoothing a
## world position against a moving target leaves a steady-state error of
## speed * lag (measured 5.9 m at 176 km/h on Corniche).

## Empty takes the first node in the "player_car" group.
@export var target_path: NodePath
@export var pivot_height := 1.0
@export var distance_min := 5.5
@export var distance_max := 7.5
@export var camera_height := 2.0
@export var position_lag := 0.12
@export var heading_lag := 0.45
@export var fov_min := 70.0
@export var fov_max := 85.0
@export var look_ahead_per_mps := 0.25
@export var still_speed := 2.0
@export var top_speed := 45.0

var _car: Node3D
var _heading := Vector3.FORWARD
var _offset := Vector3.ZERO


func _ready() -> void:
	_resolve()
	if _car != null:
		_heading = -_car.global_transform.basis.z
		_offset = -_heading * distance_min + Vector3.UP * camera_height
		global_position = _car.global_position + _offset


func _resolve() -> void:
	if target_path != NodePath(""):
		_car = get_node_or_null(target_path) as Node3D
	if _car == null:
		_car = get_tree().get_first_node_in_group(&"player_car") as Node3D


func _physics_process(delta: float) -> void:
	if _car == null:
		_resolve()
		if _car == null:
			return
	var vel := Vector3.ZERO
	if _car is RigidBody3D:
		vel = (_car as RigidBody3D).linear_velocity
	var flat := Vector3(vel.x, 0.0, vel.z)
	var speed := flat.length()
	var want := -_car.global_transform.basis.z
	want.y = 0.0
	if speed > still_speed:
		want = flat.normalized()
	if want.length_squared() > 1e-6:
		var k := 1.0 - exp(-delta / maxf(heading_lag, 0.001))
		_heading = _heading.slerp(want.normalized(), k).normalized()
	var t := clampf(speed / maxf(top_speed, 1.0), 0.0, 1.0)
	var want_offset := -_heading * lerpf(distance_min, distance_max, t) + Vector3.UP * camera_height
	var kp := 1.0 - exp(-delta / maxf(position_lag, 0.001))
	_offset = _offset.lerp(want_offset, kp)
	var pivot := _car.global_position + Vector3.UP * pivot_height
	global_position = pivot + _offset
	look_at(pivot + _heading * speed * look_ahead_per_mps, Vector3.UP)
	fov = lerpf(fov_min, fov_max, t)
