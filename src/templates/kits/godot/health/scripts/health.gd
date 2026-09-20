extends Node
class_name BGHealth
## A health component. Add under anything that can be hurt: the player, an
## enemy, a door. It holds the number, applies damage and healing with an
## invulnerability window, and tells the parent what happened. It never
## frees the parent: connect `died` and decide (play a death, queue_free,
## respawn) where the game logic lives.
##
## Works in 2D and 3D alike; nothing here touches a transform.

signal health_changed(current: int, maximum: int)
## `amount` is what actually landed after clamping; `source` is whatever the
## attacker passed (a Node, a String, null).
signal damaged(amount: int, source: Variant)
signal healed(amount: int, source: Variant)
signal died(source: Variant)
## Health went from 0 back above it.
signal revived

@export var max_health := 10
## Starting health. 0 or less means start full.
@export var start_health := 0
## Seconds after a hit during which further damage is ignored. 0 disables.
@export var invulnerable_seconds := 0.0
## Hurt while dead? Off by default: a corpse taking damage re-emits `died`.
@export var damage_when_dead := false

var current := 0
var _invulnerable_until_ms := 0


func _ready() -> void:
	current = start_health if start_health > 0 else max_health
	current = clampi(current, 0, max_health)
	health_changed.emit(current, max_health)


func is_dead() -> bool:
	return current <= 0


func is_invulnerable() -> bool:
	return Time.get_ticks_msec() < _invulnerable_until_ms


## Fraction in 0..1, for bars.
func ratio() -> float:
	return 0.0 if max_health <= 0 else float(current) / float(max_health)


## Apply `amount` of damage. Returns what landed (0 while invulnerable or,
## by default, while already dead).
func damage(amount: int, source: Variant = null) -> int:
	if amount <= 0 or is_invulnerable():
		return 0
	if is_dead() and not damage_when_dead:
		return 0
	var before := current
	current = maxi(current - amount, 0)
	var landed := before - current
	if landed == 0:
		return 0
	if invulnerable_seconds > 0.0:
		_invulnerable_until_ms = Time.get_ticks_msec() + int(invulnerable_seconds * 1000.0)
	damaged.emit(landed, source)
	health_changed.emit(current, max_health)
	BGateTelemetry.emit_event("damage", {
		"target": String(get_parent().name) if get_parent() else "",
		"amount": landed, "left": current,
	})
	if current == 0 and before > 0:
		died.emit(source)
		BGateTelemetry.emit_event("death", {
			"target": String(get_parent().name) if get_parent() else "",
		})
	return landed


## Restore `amount`. Returns what was actually restored.
func heal(amount: int, source: Variant = null) -> int:
	if amount <= 0:
		return 0
	var before := current
	current = mini(current + amount, max_health)
	var restored := current - before
	if restored == 0:
		return 0
	healed.emit(restored, source)
	health_changed.emit(current, max_health)
	if before == 0:
		revived.emit()
	return restored


## Set to full and clear invulnerability. A respawn.
func reset() -> void:
	var was_dead := is_dead()
	current = max_health
	_invulnerable_until_ms = 0
	health_changed.emit(current, max_health)
	if was_dead:
		revived.emit()


## Grow (or shrink) the maximum; current is clamped, and grows with it when
## `keep_ratio` is set (a level-up that leaves you at the same fraction).
func set_max(value: int, keep_ratio: bool = false) -> void:
	value = maxi(value, 1)
	if keep_ratio and max_health > 0:
		current = int(round(ratio() * value))
	max_health = value
	current = clampi(current, 0, max_health)
	health_changed.emit(current, max_health)
