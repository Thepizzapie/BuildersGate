extends CharacterBody2D
## Top-down character controller, four-directional by default.
##
## A Zelda-style body: WASD/arrows move it, it remembers which way it faces
## when it stops, and it tells you when that changes so a sprite can turn.
## Nothing here knows about your art: wire `facing_changed` to whatever
## draws the character, or set `sprite_path` and it plays
## "<walk_anim>_<facing>" / "<idle_anim>_<facing>" on an AnimatedSprite2D
## when those animations exist.
##
## Needs the input actions move_left / move_right / move_up / move_down.
## kit_install adds them to project.godot when they are missing.

## The direction the body last moved in, or faces at rest. Always a unit
## cardinal (or diagonal when `four_way` is off).
signal facing_changed(direction: Vector2, facing: String)
## Fires once when the body starts moving and once when it stops.
signal moving_changed(moving: bool)

@export_group("Movement")
@export var speed := 96.0
## Pixels per second^2 toward the wanted velocity. 0 snaps instantly.
@export var acceleration := 1200.0
## Pixels per second^2 back to rest. 0 snaps instantly.
@export var friction := 1600.0
## Snap movement to the four cardinal directions. Off allows diagonals; the
## reported `facing` is still one of the four, whichever axis is larger.
@export var four_way := true

@export_group("Sprite (optional)")
## An AnimatedSprite2D to drive. Empty: the controller only emits signals.
@export var sprite_path: NodePath
@export var walk_anim := "walk"
@export var idle_anim := "idle"

var facing := Vector2.DOWN
var facing_name := "down"
var _moving := false
var _sprite: AnimatedSprite2D = null

const _NAMES := {
	Vector2.DOWN: "down", Vector2.UP: "up",
	Vector2.LEFT: "left", Vector2.RIGHT: "right",
}


func _ready() -> void:
	add_to_group(&"player")
	if not sprite_path.is_empty():
		_sprite = get_node_or_null(sprite_path) as AnimatedSprite2D
	_play(idle_anim)


func _physics_process(delta: float) -> void:
	var input := Input.get_vector("move_left", "move_right", "move_up", "move_down")
	if four_way and input != Vector2.ZERO:
		input = _cardinal(input)

	var target := input * speed
	if input != Vector2.ZERO:
		velocity = _approach(velocity, target, acceleration, delta)
		_set_facing(input)
	else:
		velocity = _approach(velocity, Vector2.ZERO, friction, delta)

	move_and_slide()

	var now_moving := input != Vector2.ZERO
	if now_moving != _moving:
		_moving = now_moving
		moving_changed.emit(_moving)
		_play(walk_anim if _moving else idle_anim)
		BGateTelemetry.emit_event("move_start" if _moving else "move_stop", {
			"facing": facing_name, "speed": speed,
		})


## Snap any direction to the nearest of the four cardinals. Ties go to the
## horizontal axis so a perfect diagonal is not a coin flip every frame.
static func _cardinal(v: Vector2) -> Vector2:
	if absf(v.x) >= absf(v.y):
		return Vector2.RIGHT if v.x > 0.0 else Vector2.LEFT
	return Vector2.DOWN if v.y > 0.0 else Vector2.UP


static func _approach(from: Vector2, to: Vector2, rate: float, delta: float) -> Vector2:
	if rate <= 0.0:
		return to
	return from.move_toward(to, rate * delta)


func _set_facing(direction: Vector2) -> void:
	var cardinal := _cardinal(direction)
	if cardinal == facing:
		return
	facing = cardinal
	facing_name = _NAMES[cardinal]
	facing_changed.emit(facing, facing_name)
	if _moving:
		_play(walk_anim)


func _play(base: String) -> void:
	if _sprite == null or _sprite.sprite_frames == null:
		return
	var name := base + "_" + facing_name
	if _sprite.sprite_frames.has_animation(name):
		_sprite.play(name)
	elif _sprite.sprite_frames.has_animation(base):
		_sprite.play(base)


## THE TRAVERSAL CONTRACT. traversal_prove refuses any controller that cannot
## say whether a scripted move owns the body right now. This one has none.
func is_in_scripted_move() -> bool:
	return false
