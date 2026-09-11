extends "res://scripts/interactable.gd"
## A one-shot pickup: bobs and spins until used, then disappears. The kind of
## thing every prototype writes on day one; here it is a scene to instance.

@export var item_id := "coin"
@export var spin_speed := 1.5
@export var bob_height := 0.08
@export var bob_speed := 2.0

var _t := 0.0
var _base_y := 0.0

@onready var _visual: Node3D = get_node_or_null("Visual") as Node3D


func _ready() -> void:
	super._ready()
	one_shot = true
	prompt = "Take " + item_id
	if _visual != null:
		_base_y = _visual.position.y


func _process(delta: float) -> void:
	super._process(delta)
	_t += delta
	if _visual != null:
		_visual.rotation.y += spin_speed * delta
		_visual.position.y = _base_y + sin(_t * bob_speed) * bob_height


func _on_interact(actor: Node) -> void:
	BGateTelemetry.emit_event("pickup", {"item": item_id, "by": String(actor.name)})
	queue_free()
