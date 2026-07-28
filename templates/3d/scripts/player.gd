extends CharacterBody3D
## Template player. Same philosophy as the 2D one: the tunables a playtester
## complains about are exported AND emitted, so a vibe lands next to its numbers.

@export var speed := 5.0
@export var jump_velocity := 4.8
@export var gravity := 14.0
## Falling faster than rising is the main lever on "floaty" — the first thing to
## try when someone says the jump feels wrong. Exported, not baked into gravity.
@export var fall_multiplier := 1.5
@export var mouse_sensitivity := 0.0025
@export var coyote_time := 0.10

var _coyote := 0.0
## Starts false: the player spawns in mid-air, and claiming otherwise makes the
## first landing look like a transition that never happened.
var _was_on_floor := false
var _air_start_ms := 0
var _air_start_y := 0.0
var _peak_y := 0.0
## Why we're airborne. Without this the spawn-drop reports as a jump and every
## session opens with a fabricated "land" whose numbers look real enough to mislead.
var _air_cause := "spawn"

@onready var _camera: Camera3D = $Camera3D


func _ready() -> void:
	_begin_air("spawn")
	# Headless has no window to capture a mouse in, and doing so would fail noisily
	# during automated runs.
	if not DisplayServer.get_name() == "headless":
		Input.mouse_mode = Input.MOUSE_MODE_CAPTURED


func _begin_air(cause: String) -> void:
	_air_cause = cause
	_air_start_ms = Time.get_ticks_msec()
	_air_start_y = global_position.y
	_peak_y = global_position.y


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion and Input.mouse_mode == Input.MOUSE_MODE_CAPTURED:
		rotate_y(-event.relative.x * mouse_sensitivity)
		_camera.rotate_x(-event.relative.y * mouse_sensitivity)
		_camera.rotation.x = clampf(_camera.rotation.x, -1.4, 1.4)
	if event.is_action_pressed("ui_cancel"):
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE


func _physics_process(delta: float) -> void:
	var on_floor := is_on_floor()

	if on_floor:
		_coyote = coyote_time
	else:
		_coyote = maxf(_coyote - delta, 0.0)
		# In 3D, up is +Y: the peak is the MAXIMUM y reached.
		_peak_y = maxf(_peak_y, global_position.y)
		velocity.y -= gravity * delta * (fall_multiplier if velocity.y < 0.0 else 1.0)

	if Input.is_action_just_pressed("jump") and _coyote > 0.0:
		_coyote = 0.0
		_begin_air("jump")
		velocity.y = jump_velocity
		BGateTelemetry.emit_event("jump", {
			"jump_velocity": jump_velocity,
			"gravity": gravity,
			"fall_multiplier": fall_multiplier,
			"coyote_time": coyote_time,
			"from_coyote": not on_floor,
		})

	var input := Input.get_vector("move_left", "move_right", "move_forward", "move_back")
	var direction := (transform.basis * Vector3(input.x, 0.0, input.y)).normalized()
	velocity.x = direction.x * speed
	velocity.z = direction.z * speed

	move_and_slide()

	var now_on_floor := is_on_floor()

	# Walked off a ledge — airborne without jumping. Start the clock here or the
	# next landing reports air_time since the last JUMP, which may be minutes.
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
