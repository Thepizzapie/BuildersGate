extends Area3D
## A trigger: fires when a body in `group` enters or leaves. Checkpoints, room
## boundaries, kill planes and goal volumes are all this node with a different
## signal wired. traversal_prove drives to volumes exactly like this one, so a
## level's goals should BE these, not markers with a radius.

signal body_entered_volume(body: Node3D)
signal body_exited_volume(body: Node3D)

## Only bodies in this group count. Empty counts every body.
@export var group := &"player"
@export var one_shot := false
## Named in telemetry so a playtest can see which volume fired when.
@export var volume_id := ""

var _fired := false


func _ready() -> void:
	body_entered.connect(_on_enter)
	body_exited.connect(_on_exit)


func _counts(body: Node) -> bool:
	return group == StringName("") or body.is_in_group(group)


func _on_enter(body: Node3D) -> void:
	if not _counts(body) or (one_shot and _fired):
		return
	_fired = true
	body_entered_volume.emit(body)
	BGateTelemetry.emit_event("trigger_enter", {"volume": volume_id if volume_id != "" else String(name),
		"body": String(body.name)})


func _on_exit(body: Node3D) -> void:
	if not _counts(body):
		return
	body_exited_volume.emit(body)
