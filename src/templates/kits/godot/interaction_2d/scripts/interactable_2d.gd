extends Area2D
## A 2D interaction volume: a door, a sign, a chest, a pickup. Anything the
## player can use is an Area2D with this script (or one extending it) in the
## "interactable" group. interactor_2d.gd finds the nearest one overlapping
## its own Area2D and calls `interact(actor)` on the `interact` action.
##
## `interacted` is the hook: wire it in the editor to the thing that should
## happen, or override `_on_interact` in a subclass. One volume, one meaning.

signal interacted(actor: Node)
## The actor's probe entered or left this volume; drive a "press E" prompt.
signal focus_changed(focused: bool)

## Shown by a HUD prompt when the player is in range.
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


## Called by interactor_2d when this becomes / stops being its target.
func set_focused(focused: bool) -> void:
	focus_changed.emit(focused)


## Override in a subclass; the base does nothing but signal.
func _on_interact(_actor: Node) -> void:
	pass
