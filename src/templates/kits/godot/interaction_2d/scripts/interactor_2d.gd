extends Area2D
## The player's side of interaction_2d: an Area2D under the player that
## tracks which interactable it overlaps, keeps the nearest one focused, and
## fires `interact(actor)` on it when the `interact` action is pressed.
##
## Add as a child of the body, give it a CollisionShape2D sized to the reach
## you want, and leave `actor_path` empty to use the parent as the actor.

## The focused interactable changed (null when none is in reach).
signal target_changed(target: Node)

## Who "interacts". Empty: this node's parent.
@export var actor_path: NodePath
## Nodes in this group with `interact(actor)` are candidates.
@export var interactable_group := &"interactable"

var target: Node = null


func _ready() -> void:
	monitoring = true
	monitorable = false


func _process(_delta: float) -> void:
	_refocus()
	if target != null and Input.is_action_just_pressed("interact"):
		var actor: Node = get_parent()
		if not actor_path.is_empty():
			actor = get_node_or_null(actor_path)
		if target.has_method("interact"):
			target.interact(actor)
			BGateTelemetry.emit_event("interact", {"target": String(target.name)})


func _refocus() -> void:
	var best: Node = null
	var best_d := INF
	for area in get_overlapping_areas():
		if not area.is_in_group(interactable_group):
			continue
		if area.has_method("can_interact") and not area.can_interact():
			continue
		var d := global_position.distance_squared_to(area.global_position)
		if d < best_d:
			best_d = d
			best = area
	if best == target:
		return
	if target != null and target.has_method("set_focused"):
		target.set_focused(false)
	target = best
	if target != null and target.has_method("set_focused"):
		target.set_focused(true)
	target_changed.emit(target)
