extends CharacterBody2D
## Template player. Every tunable that a playtester is likely to complain about
## is exported and emitted as telemetry, so "the jump feels floaty" lands next to
## the numbers that made it feel that way.

@export var speed := 220.0
@export var jump_velocity := -380.0
@export var gravity := 980.0
## Falling faster than rising is the single biggest lever on "floaty". It is
## exported (not baked into gravity) precisely because it's the first thing an
## agent should try when someone says the jump feels wrong.
@export var fall_multiplier := 1.6
## Forgiveness windows. Their absence reads to players as "the jump didn't
## register" — which gets reported as a bug, not as a missing feature.
@export var coyote_time := 0.10
@export var jump_buffer := 0.10

var _coyote := 0.0
var _buffer := 0.0
## Starts false: the player spawns in mid-air, and claiming otherwise makes the
## first landing look like a transition that never happened.
var _was_on_floor := false
var _air_start_ms := 0
var _air_start_y := 0.0
var _peak_y := 0.0
## Why we're airborne. Without this the spawn-drop reports as a jump, and every
## session opens with a fabricated "land" whose numbers look real enough to
## mislead — measured: peak_height 302 on a 24px player.
var _air_cause := "spawn"


func _ready() -> void:
	_begin_air("spawn")


func _begin_air(cause: String) -> void:
	_air_cause = cause
	_air_start_ms = Time.get_ticks_msec()
	_air_start_y = global_position.y
	_peak_y = global_position.y


func _physics_process(delta: float) -> void:
	var on_floor := is_on_floor()

	if on_floor:
		_coyote = coyote_time
	else:
		_coyote = maxf(_coyote - delta, 0.0)
		# In 2D, up is -Y: the peak is the MINIMUM y reached.
		_peak_y = minf(_peak_y, global_position.y)
		velocity.y += gravity * delta * (fall_multiplier if velocity.y > 0.0 else 1.0)

	_buffer = maxf(_buffer - delta, 0.0)
	if Input.is_action_just_pressed("jump"):
		_buffer = jump_buffer

	if _buffer > 0.0 and _coyote > 0.0:
		_buffer = 0.0
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

	var direction := Input.get_axis("move_left", "move_right")
	velocity.x = direction * speed

	move_and_slide()

	var now_on_floor := is_on_floor()

	# Walked off a ledge — airborne without jumping. Start the clock here or the
	# next landing reports air_time since the last JUMP, which may be minutes.
	if _was_on_floor and not now_on_floor and _air_cause == "":
		_begin_air("fall")

	# Landing closes the loop: air_time and peak height are the numbers behind the
	# word "floaty". Emitted on the transition, not every frame.
	if now_on_floor and not _was_on_floor:
		BGateTelemetry.emit_event("land", {
			"air_time": float(Time.get_ticks_msec() - _air_start_ms) / 1000.0,
			"peak_height": absf(_peak_y - _air_start_y),
			"fall_distance": absf(global_position.y - _peak_y),
			"cause": _air_cause,
		})
		_air_cause = ""

	_was_on_floor = now_on_floor
