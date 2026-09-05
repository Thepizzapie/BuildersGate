extends Area3D
## An interaction volume. Anything the player can use - a door, a pickup, a
## lever - is an Area3D with this script (or one extending it) in the
## "interactable" group. third_person_controller.gd finds the nearest one inside
## its InteractRange and calls `interact(actor)` on the `interact` action.
##
## `interacted` is the hook: wire it in the editor to the thing that should
## happen, or override `_on_interact` in a subclass. One volume, one meaning.

signal interacted(actor: Node)

## Shown by a HUD prompt when the player is in range (the template has no HUD;
## this is the string it would show).
@export var prompt := "Interact"
## After the first use the volume switches off. A pickup is one-shot; a door
## is not.
@export var one_shot := false
## Seconds before the same volume answers again. 0 = every press.
@export var cooldown := 0.0

var _cooling := 0.0
var _used := false


func _ready() -> void:
	add_to_group(&"interactable")
	monitoring = true
	monitorable = true


func _process(delta: float) -> void:
	_cooling = maxf(_cooling - delta, 0.0)


func can_interact() -> bool:
	return not (_used and one_shot) and _cooling <= 0.0


func interact(actor: Node) -> void:
	if not can_interact():
		return
	_used = true
	_cooling = cooldown
	_on_interact(actor)
	interacted.emit(actor)
	if one_shot:
		set_deferred("monitorable", false)


## Override in a subclass; the base does nothing but signal.
func _on_interact(_actor: Node) -> void:
	pass
