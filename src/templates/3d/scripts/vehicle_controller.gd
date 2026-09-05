extends RigidBody3D
## Arcade vehicle. THE MODEL THAT FINALLY FELT RIGHT on Corniche (2026-09-04)
## after a bicycle model + grip cap gave "stiff, uncontrollable, bouncy":
##
##  * the heading turns at a FIXED rate the stick commands (turn_rate_max),
##    scaled to nothing when parked and eased off toward top speed;
##  * the velocity vector CHASES the heading exponentially (grip_align_hz) -
##    no understeer, no snap; lower the rate and you have a drift;
##  * no springs: with two or more wheels on ground the body is HELD at
##    ride_height over the averaged contact and pitched onto the ground normal,
##    so nothing can oscillate; airborne, gravity and air_stick take over;
##  * wheel rays hit the ROAD layer (6) before terrain, so a 1.5 m road bake
##    reads as road even where the terrain pokes through.
##
## Input: move_forward accelerates, move_back brakes/reverses, move_left /
## move_right steer, jump is the handbrake. Add chase_camera.gd as a SIBLING.

@export_group("Drive")
@export var engine_accel := 12.0
@export var top_speed := 45.0
@export var brake_decel := 18.0
@export var reverse_speed := 8.0
@export var rolling_drag := 0.3
@export var quad_drag := 0.0006

@export_group("Steering")
## Yaw rate (rad/s) full lock buys once the car is moving.
@export var turn_rate_max := 1.6
## Speed at which the full turn rate is available; below it steering fades so a
## parked car does not pirouette.
@export var turn_full_speed := 8.0
## Fraction of turn_rate_max left at top speed.
@export var turn_high_speed_scale := 0.5
@export var steer_input_rate := 3.0
## How fast the yaw rate approaches the commanded one (s).
@export var yaw_response := 0.12
## Velocity realigns to the heading at this rate in grip; lower is slidier.
@export var grip_align_hz := 10.0
@export var handbrake_align_hz := 2.0
@export var handbrake_drag := 0.6

@export_group("Ride")
## Wheel contact points in body space (x right, z back). Four by default.
@export var wheel_offsets: Array[Vector3] = [
	Vector3(-0.8, 0.0, -1.3), Vector3(0.8, 0.0, -1.3),
	Vector3(-0.8, 0.0, 1.3), Vector3(0.8, 0.0, 1.3)]
## Height of the body origin above the averaged contact when riding.
@export var ride_height := 0.6
## How far below the wheel offset a ray still counts as ground.
@export var ground_reach := 1.0
@export var ride_follow_rate := 12.0
@export var ride_max_vertical := 8.0
@export var attitude_rate := 10.0
## Physics layer the road bodies live on; 0 = no preference.
@export var road_layer := 6
@export var air_stick_accel := 8.0
@export var upright_torque := 14.0
@export var upright_damping := 8.0

var control_throttle := 0.0
var control_brake := 0.0
var control_steer := 0.0
var handbrake := false

var speed_kmh := 0.0
var wheels_on_ground := 0
var wheels_on_road := 0

var _steer_now := 0.0
var _ground_hit_avg := Vector3.ZERO
var _ground_normal := Vector3.UP


func _ready() -> void:
	add_to_group(&"player_car")
	custom_integrator = false
	can_sleep = false


func _read_input() -> void:
	control_throttle = Input.get_action_strength("move_forward")
	control_brake = Input.get_action_strength("move_back")
	control_steer = Input.get_action_strength("move_right") - Input.get_action_strength("move_left")
	handbrake = Input.is_action_pressed("jump")


func _integrate_forces(state: PhysicsDirectBodyState3D) -> void:
	var dt := state.step
	if dt <= 0.0:
		return
	_read_input()
	var gb := state.transform.basis.orthonormalized()
	var fwd := -gb.z
	var up := gb.y
	_probe_ground(state)
	var grounded := wheels_on_ground > 0
	_apply_ride(state, dt)
	var v := state.linear_velocity
	var v_fwd := v.dot(fwd)
	speed_kmh = v_fwd * 3.6
	_apply_steering(state, gb, dt, grounded)
	_apply_drive(state, fwd, v_fwd, dt, grounded)
	_apply_grip(state, gb, dt, grounded)
	if not grounded:
		_apply_air(state, up)
	if Engine.get_physics_frames() % 15 == 0:
		BGateTelemetry.emit_event("vehicle", {"speed_kmh": speed_kmh, "wheels_on_ground": wheels_on_ground,
			"wheels_on_road": wheels_on_road, "handbrake": handbrake})


## One ray per wheel, straight down in BODY space so a pitched car still reads
## the road under its wheels. Averages the contact and the normal.
func _probe_ground(state: PhysicsDirectBodyState3D) -> void:
	var space := state.get_space_state()
	var xf := state.transform
	var down := -xf.basis.y.normalized()
	var hits := 0
	var road := 0
	var sum_pos := Vector3.ZERO
	var sum_n := Vector3.ZERO
	for off in wheel_offsets:
		var from: Vector3 = xf * off
		var q := PhysicsRayQueryParameters3D.create(from, from + down * ground_reach)
		q.exclude = [get_rid()]
		var hit := space.intersect_ray(q)
		if hit.is_empty():
			continue
		hits += 1
		sum_pos += hit.position
		sum_n += hit.normal
		var col: Object = hit.collider
		if road_layer > 0 and col is CollisionObject3D and (col as CollisionObject3D).get_collision_layer_value(road_layer):
			road += 1
	wheels_on_ground = hits
	wheels_on_road = road
	if hits > 0:
		_ground_hit_avg = sum_pos / hits
		_ground_normal = sum_n.normalized()
	else:
		_ground_normal = Vector3.UP


func _apply_ride(state: PhysicsDirectBodyState3D, dt: float) -> void:
	if wheels_on_ground < 2:
		return
	var n := _ground_normal
	var xf := state.transform
	var d := (xf.origin - _ground_hit_avg).dot(n)
	var err := ride_height - d
	var v := state.linear_velocity
	var want_vn := clampf(err * ride_follow_rate, -ride_max_vertical, ride_max_vertical)
	state.linear_velocity = v + n * (want_vn - v.dot(n))
	state.apply_central_force(-state.total_gravity * mass)
	var b := xf.basis.orthonormalized()
	var fwd := -b.z
	fwd = fwd - n * fwd.dot(n)
	if fwd.length_squared() < 1e-6:
		return
	fwd = fwd.normalized()
	var tb := Basis(n.cross(-fwd).normalized(), n, -fwd).orthonormalized()
	var k := 1.0 - exp(-dt * attitude_rate)
	var q := b.get_rotation_quaternion().slerp(tb.get_rotation_quaternion(), k)
	state.transform = Transform3D(Basis(q), xf.origin)
	var av := state.angular_velocity
	state.angular_velocity = n * av.dot(n)


func _turn_rate(speed: float) -> float:
	var ramp := clampf(speed / maxf(turn_full_speed, 0.1), 0.0, 1.0)
	var high := clampf(speed / maxf(top_speed, 1.0), 0.0, 1.0)
	return turn_rate_max * ramp * lerpf(1.0, turn_high_speed_scale, high)


func _apply_steering(state: PhysicsDirectBodyState3D, gb: Basis, dt: float, grounded: bool) -> void:
	_steer_now = move_toward(_steer_now, clampf(control_steer, -1.0, 1.0), steer_input_rate * dt)
	if not grounded:
		return
	var v := state.linear_velocity
	var planar := maxf(Vector3(v.x, 0.0, v.z).length(), 1.0)
	# +steer is RIGHT; a positive yaw about +Y is a LEFT turn.
	var target := -_turn_rate(planar) * _steer_now
	var up := gb.y
	var av := state.angular_velocity
	var yaw_now := av.dot(up)
	var blend := 1.0 - exp(-dt / maxf(yaw_response, 0.001))
	state.angular_velocity = av + up * (lerpf(yaw_now, target, blend) - yaw_now)


func _apply_drive(state: PhysicsDirectBodyState3D, fwd: Vector3, v_fwd: float, dt: float, grounded: bool) -> void:
	if not grounded:
		return
	var accel := 0.0
	if control_brake > 0.0 and v_fwd > 0.5:
		accel = -brake_decel * control_brake
	elif control_brake > 0.0:
		accel = -engine_accel * control_brake * (1.0 if v_fwd > -reverse_speed else 0.0)
	elif control_throttle > 0.0:
		var headroom := clampf(1.0 - v_fwd / maxf(top_speed, 0.1), 0.0, 1.0)
		accel = engine_accel * control_throttle * headroom
	accel -= rolling_drag * signf(v_fwd) * (1.0 if absf(v_fwd) > 0.2 else 0.0)
	accel -= quad_drag * v_fwd * absf(v_fwd)
	if handbrake:
		accel -= handbrake_drag * v_fwd
	state.linear_velocity += fwd * accel * dt
	# Parked: kill creep so the car does not drift off on its own.
	if control_throttle == 0.0 and control_brake == 0.0 and absf(v_fwd) < 0.3:
		var v := state.linear_velocity
		state.linear_velocity = v - fwd * v.dot(fwd)


func _apply_grip(state: PhysicsDirectBodyState3D, gb: Basis, dt: float, grounded: bool) -> void:
	if not grounded:
		return
	var v := state.linear_velocity
	var flat := Vector3(v.x, 0.0, v.z)
	if flat.length() < 0.3:
		return
	var fwd := -gb.z
	fwd.y = 0.0
	if fwd.length_squared() < 1e-6:
		return
	fwd = fwd.normalized()
	# Reverse travels along -fwd; align to whichever way we are going.
	if flat.dot(fwd) < 0.0:
		fwd = -fwd
	var hz := handbrake_align_hz if handbrake else grip_align_hz
	var turn := flat.signed_angle_to(fwd, Vector3.UP) * (1.0 - exp(-dt * hz))
	flat = flat.rotated(Vector3.UP, turn)
	state.linear_velocity = Vector3(flat.x, v.y, flat.z)


func _apply_air(state: PhysicsDirectBodyState3D, up: Vector3) -> void:
	if air_stick_accel > 0.0:
		state.apply_central_force(Vector3.DOWN * air_stick_accel * mass)
	var axis := up.cross(Vector3.UP)
	var av := state.angular_velocity
	var perp := av - up * av.dot(up)
	state.apply_torque((axis * upright_torque - perp * upright_damping) * mass)


## Put the car somewhere with a velocity, cleanly. Used by respawns.
func place_at(xform: Transform3D, vel: Vector3 = Vector3.ZERO) -> void:
	PhysicsServer3D.body_set_state(get_rid(), PhysicsServer3D.BODY_STATE_TRANSFORM, xform)
	PhysicsServer3D.body_set_state(get_rid(), PhysicsServer3D.BODY_STATE_LINEAR_VELOCITY, vel)
	PhysicsServer3D.body_set_state(get_rid(), PhysicsServer3D.BODY_STATE_ANGULAR_VELOCITY, Vector3.ZERO)
