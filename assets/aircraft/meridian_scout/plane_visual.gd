extends Node3D
## Visual controls only. Attach this scene beneath your aircraft physics body.
## Model forward is +Z, up is +Y; wheel/propeller origins are already aligned.
@export_range(0, 3000, 10) var propeller_rpm := 0.0
@export_range(-1, 1, .01) var roll_input := 0.0
@export_range(-1, 1, .01) var pitch_input := 0.0
@export_range(-1, 1, .01) var rudder_input := 0.0
@export_range(0, 1, .01) var flap_extension := 0.0
@export var wheel_speed_mps := 0.0

var parts: Dictionary[String, Node3D] = {}
var rest: Dictionary[String, Vector3] = {}

func _ready() -> void:
	for part in ["Propeller", "AileronLeft", "AileronRight", "ElevatorLeft", "ElevatorRight",
			"FlapLeft", "FlapRight", "Rudder", "WheelLeft", "WheelRight", "TailWheel"]:
		var node := $Model.find_child(part, true, false) as Node3D
		if node != null:
			parts[part] = node
			rest[part] = node.rotation

func _process(delta: float) -> void:
	if parts.has("Propeller"):
		parts.Propeller.rotate_z(propeller_rpm * TAU / 60.0 * delta)
	for part in ["WheelLeft", "WheelRight", "TailWheel"]:
		if parts.has(part):
			parts[part].rotate_x(wheel_speed_mps / (.19 if part == "TailWheel" else .38) * delta)
	_pose("AileronLeft", Vector3.RIGHT, deg_to_rad(20) * clampf(roll_input, -1, 1))
	_pose("AileronRight", Vector3.RIGHT, -deg_to_rad(20) * clampf(roll_input, -1, 1))
	for part in ["ElevatorLeft", "ElevatorRight"]:
		_pose(part, Vector3.RIGHT, deg_to_rad(22) * clampf(pitch_input, -1, 1))
	for part in ["FlapLeft", "FlapRight"]:
		_pose(part, Vector3.RIGHT, -deg_to_rad(32) * clampf(flap_extension, 0, 1))
	_pose("Rudder", Vector3.UP, deg_to_rad(25) * clampf(rudder_input, -1, 1))

func _pose(part: String, axis: Vector3, angle: float) -> void:
	if parts.has(part):
		parts[part].rotation = rest[part] + axis * angle
