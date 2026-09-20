extends "res://scripts/interactable_2d.gd"
## A one-shot 2D pickup: bobs until used, then disappears. When the actor
## (or one of its children) is a BGInventory it goes in there; overflow
## leaves the pickup on the ground with the remainder.

@export var item_id := "coin"
@export var count := 1
@export var bob_height := 2.0
@export var bob_speed := 3.0
## The node to bob. Empty: this Area2D's first Sprite2D/AnimatedSprite2D child.
@export var visual_path: NodePath

var _t := 0.0
var _base_y := 0.0
var _visual: Node2D = null


func _ready() -> void:
	super._ready()
	one_shot = true
	prompt = "Take " + item_id
	if not visual_path.is_empty():
		_visual = get_node_or_null(visual_path) as Node2D
	if _visual == null:
		for child in get_children():
			if child is Sprite2D or child is AnimatedSprite2D:
				_visual = child
				break
	if _visual != null:
		_base_y = _visual.position.y


func _process(delta: float) -> void:
	super._process(delta)
	_t += delta
	if _visual != null:
		_visual.position.y = _base_y + sin(_t * bob_speed) * bob_height


func _on_interact(actor: Node) -> void:
	var inv := _inventory_of(actor)
	var left := 0
	if inv != null:
		left = inv.add(item_id, count)
	BGateTelemetry.emit_event("pickup", {
		"item": item_id, "count": count - left,
		"by": String(actor.name) if actor else "",
	})
	if left > 0:
		# Could not all go in: stay on the ground with what remains.
		count = left
		_used = false
		set_deferred("monitorable", true)
		return
	queue_free()


static func _inventory_of(actor: Node) -> Node:
	if actor == null:
		return null
	if actor.has_method("add") and actor.has_method("count_of"):
		return actor
	for child in actor.get_children():
		if child.has_method("add") and child.has_method("count_of"):
			return child
	return null
