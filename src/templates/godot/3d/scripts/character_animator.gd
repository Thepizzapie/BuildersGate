extends AnimationTree
class_name BGateCharacterAnimator
## Drives a rigged character's clips from what its body is DOING.
##
## Written once, here, because every project re-invented "velocity -> walk"
## by hand and most of them got a T-pose statue: blender_animate ends at a
## .glb with clips in it, and nothing in the engine plays a clip until a
## state machine says so. This node builds that state machine AT RUNTIME
## from clip names, so the scene stays small and a missing clip is a
## reported gap rather than a broken resource.
##
##   Locomotion  a BlendSpace1D over `locomotion` (idle, walk, run, ...)
##               positioned by PLANAR SPEED in m/s, with the blend points at
##               the body's own walk/sprint speeds when it exports them.
##   Jump/Fall   entered when the body leaves the floor (rising / falling).
##   Land        played once on touchdown when the clip exists.
##   actions     any other clip, played once with play_action(); the machine
##               returns to Locomotion when it ends.
##
## Locomotion clips are FORCED TO LOOP. Godot's importer only loops a clip
## named "<name>-loop", and a walk that stops after one cycle reads as a
## freeze. `report()` says which were changed.
##
## Manual mode (`manual = true`) replaces the body's readings with the
## `manual_*` values - what character_wire's in-engine probe drives, so a
## headless run can prove every state is reachable without physics.

## The body whose velocity and floor contact drive the states. Defaults to
## the parent, which is where character_wire puts this node.
@export var body_path: NodePath = NodePath("..")
## Clips in order of speed: idle first, then walk, run, sprint...
@export var locomotion: PackedStringArray = PackedStringArray(["idle", "walk", "run"])
## Speed (m/s) of each locomotion clip when the body exports no speeds.
@export var locomotion_speeds: PackedFloat32Array = PackedFloat32Array([0.0, 1.5, 4.0, 7.0])
@export var clip_jump := ""
@export var clip_fall := ""
@export var clip_land := ""
## One-shot clips reachable through play_action(name).
@export var actions: PackedStringArray = PackedStringArray()
## AnimationLibrary resources (res://...) added to the player before the
## machine is built - what godot_clip_retarget saves. Their clips are named
## "<file stem>/<clip>" and may appear in `locomotion`, `clip_*`, `actions`.
@export var libraries: PackedStringArray = PackedStringArray()
## The bone whose translation is root motion (a pack's "_RM" clips); empty
## disables. root_motion_velocity is then the clip's travel per second in
## WORLD space, for a controller to apply.
@export var root_motion_bone := "Root"
@export var blend_time := 0.15
## Seconds off the floor before Fall plays - ramps and steps flicker otherwise.
@export var fall_delay := 0.12
## How fast the blend position follows the measured speed.
@export var speed_smoothing := 12.0

@export_group("Manual (probe)")
@export var manual := false
@export var manual_speed := 0.0
@export var manual_on_floor := true
@export var manual_vertical := 0.0

signal action_finished(name: String)

var _body: Node = null
var _player: AnimationPlayer = null
var _playback: AnimationNodeStateMachinePlayback = null
var _states: PackedStringArray = PackedStringArray()
var _present: PackedStringArray = PackedStringArray()
var _missing: PackedStringArray = PackedStringArray()
var _looped: PackedStringArray = PackedStringArray()
var _speeds: PackedFloat32Array = PackedFloat32Array()
var _blend := 0.0
var _air_time := 0.0
var _was_on_floor := true
var _action := ""
var _action_entered := false
var _built := false
var _errors: PackedStringArray = PackedStringArray()
var _loaded_libraries: PackedStringArray = PackedStringArray()
var _action_clips := {}
var _root_motion_active := false
## The current clip's root travel per second, world space. Zero on in-place clips.
var root_motion_velocity := Vector3.ZERO


func _ready() -> void:
	_body = get_node_or_null(body_path)
	_player = _resolve_player()
	if _player == null:
		_errors.append("no AnimationPlayer under %s - is the model instanced?" % get_path())
		return
	# The tree's tracks resolve against ITS root_node, not the player's (4.3+),
	# and the player's root is the imported model. Point ours at the same node.
	var player_root := _player.get_node_or_null(_player.root_node)
	if player_root != null:
		root_node = get_path_to(player_root)
	anim_player = get_path_to(_player)
	_load_libraries()
	_build()
	if root_motion_bone != "":
		var skel := _find_skeleton(_player.get_node_or_null(_player.root_node))
		if skel != null and skel.find_bone(root_motion_bone) != -1:
			root_motion_track = NodePath("%s:%s" % [_player.get_node(_player.root_node).get_path_to(skel), root_motion_bone])
			_root_motion_active = true
	active = true
	if _built:
		_playback = get("parameters/playback") as AnimationNodeStateMachinePlayback
		if _playback != null:
			_playback.start(&"Locomotion")
	animation_finished.connect(_on_finished)


func _load_libraries() -> void:
	for res in libraries:
		if res == "" or not ResourceLoader.exists(res):
			_errors.append("library not found: %s" % res)
			continue
		var lib = load(res)
		if not (lib is AnimationLibrary):
			_errors.append("not an AnimationLibrary: %s" % res)
			continue
		var stem := res.get_file().get_basename()
		if _player.has_animation_library(StringName(stem)):
			_loaded_libraries.append(stem)
			continue
		var err := _player.add_animation_library(StringName(stem), lib)
		if err != OK:
			_errors.append("could not add library %s (error %d)" % [res, err])
		else:
			_loaded_libraries.append(stem)


func _find_skeleton(node: Node) -> Skeleton3D:
	if node == null:
		return null
	if node is Skeleton3D:
		return node
	for child in node.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null


func _resolve_player() -> AnimationPlayer:
	if anim_player != NodePath("") and has_node(anim_player):
		var explicit := get_node(anim_player)
		if explicit is AnimationPlayer:
			return explicit
	var host := _body if _body != null else get_parent()
	return _find_player(host)


func _find_player(node: Node) -> AnimationPlayer:
	if node == null:
		return null
	if node is AnimationPlayer:
		return node
	for child in node.get_children():
		var found := _find_player(child)
		if found != null:
			return found
	return null


func _has(clip: String) -> bool:
	return clip != "" and _player.has_animation(clip)


func _anim_node(clip: String) -> AnimationNodeAnimation:
	var a := AnimationNodeAnimation.new()
	a.animation = StringName(clip)
	return a


func _transition(from: StringName, to: StringName, auto_at_end: bool = false) -> void:
	var t := AnimationNodeStateMachineTransition.new()
	t.xfade_time = blend_time
	if auto_at_end:
		t.switch_mode = AnimationNodeStateMachineTransition.SWITCH_MODE_AT_END
		t.advance_mode = AnimationNodeStateMachineTransition.ADVANCE_MODE_AUTO
	else:
		t.switch_mode = AnimationNodeStateMachineTransition.SWITCH_MODE_IMMEDIATE
		t.advance_mode = AnimationNodeStateMachineTransition.ADVANCE_MODE_ENABLED
	(tree_root as AnimationNodeStateMachine).add_transition(from, to, t)


func _loop(clip: String) -> void:
	var anim := _player.get_animation(clip)
	if anim != null and anim.loop_mode == Animation.LOOP_NONE:
		anim.loop_mode = Animation.LOOP_LINEAR
		_looped.append(clip)


func _build() -> void:
	var sm := AnimationNodeStateMachine.new()
	tree_root = sm
	_present = PackedStringArray()
	_missing = PackedStringArray()
	_states = PackedStringArray()

	# Locomotion: whichever of the ordered clips exist, at rising speeds.
	var loco_clips: PackedStringArray = PackedStringArray()
	for clip in locomotion:
		if _has(clip):
			loco_clips.append(clip)
			_present.append(clip)
		else:
			_missing.append(clip)
	if loco_clips.is_empty():
		_errors.append("no locomotion clip present (wanted %s)" % ", ".join(locomotion))
		return
	_speeds = _speeds_for(loco_clips.size())
	var space := AnimationNodeBlendSpace1D.new()
	space.min_space = _speeds[0]
	space.max_space = maxf(_speeds[_speeds.size() - 1], _speeds[0] + 0.01)
	space.sync = true
	for i in loco_clips.size():
		_loop(loco_clips[i])
		space.add_blend_point(_anim_node(loco_clips[i]), _speeds[i])
	sm.add_node(&"Locomotion", space)
	_states.append("Locomotion")
	_transition(&"Start", &"Locomotion")

	# Air: jump (rising) -> fall (falling) -> land (once) -> locomotion.
	for pair in [["Jump", clip_jump], ["Fall", clip_fall], ["Land", clip_land]]:
		var state: String = pair[0]
		var clip: String = pair[1]
		if clip == "":
			continue
		if not _has(clip):
			_missing.append(clip)
			continue
		_present.append(clip)
		if state == "Fall":
			_loop(clip)
		sm.add_node(StringName(state), _anim_node(clip))
		_states.append(state)
	if _states.has("Jump"):
		_transition(&"Locomotion", &"Jump")
		if _states.has("Fall"):
			_transition(&"Jump", &"Fall", true)
		_transition(&"Jump", &"Locomotion")
	if _states.has("Fall"):
		_transition(&"Locomotion", &"Fall")
		_transition(&"Fall", &"Locomotion")
	if _states.has("Land"):
		_transition(&"Land", &"Locomotion", true)
		if _states.has("Fall"):
			_transition(&"Fall", &"Land")
		if _states.has("Jump"):
			_transition(&"Jump", &"Land")
		_transition(&"Locomotion", &"Land")

	# Actions: one state each, back to locomotion when the clip ends.
	for clip in actions:
		if not _has(clip):
			_missing.append(clip)
			continue
		# A state name cannot carry "/" (a library clip is "lib/clip"); the
		# state is the clip with "/" as "_", and play_action takes either.
		var state := _state_name(clip)
		if _states.has(state):
			continue
		_present.append(clip)
		sm.add_node(StringName(state), _anim_node(clip))
		_states.append(state)
		_action_clips[state] = clip
		_transition(&"Locomotion", StringName(state))
		_transition(StringName(state), &"Locomotion", true)
	_built = true


## Blend points from the body's own tunables when it exports them
## (walk_speed / sprint_speed, or speed), else the defaults.
func _speeds_for(count: int) -> PackedFloat32Array:
	var out := PackedFloat32Array()
	var walk := -1.0
	var top := -1.0
	if _body != null:
		var w = _body.get("walk_speed")
		var s = _body.get("sprint_speed")
		var only = _body.get("speed")
		if w != null:
			walk = float(w)
		if s != null:
			top = float(s)
		if walk < 0.0 and only != null:
			walk = float(only)
	if count == 1:
		out.append(0.0)
		return out
	if walk > 0.0:
		if top <= walk:
			top = walk * 2.0
		out.append(0.0)
		if count == 2:
			out.append(walk)
			return out
		# idle, walk, ..., top - intermediates spaced evenly between walk and top
		var inner := count - 2
		for i in inner:
			out.append(walk + (top - walk) * float(i) / float(inner))
		out.append(top)
		return out
	for i in count:
		out.append(locomotion_speeds[mini(i, locomotion_speeds.size() - 1)])
	return out


func _physics_process(delta: float) -> void:
	if not _built or _playback == null:
		return
	var speed := manual_speed
	var on_floor := manual_on_floor
	var vertical := manual_vertical
	if not manual and _body is CharacterBody3D:
		var cb := _body as CharacterBody3D
		speed = Vector2(cb.velocity.x, cb.velocity.z).length()
		on_floor = cb.is_on_floor()
		vertical = cb.velocity.y
	# Locomotion blend follows speed, smoothed so a stop does not snap.
	_blend = lerpf(_blend, speed, minf(1.0, speed_smoothing * delta))
	set("parameters/Locomotion/blend_position", _blend)
	if _root_motion_active and delta > 0.0:
		var travel := get_root_motion_position()
		var basis: Basis = _body.global_transform.basis if _body is Node3D else Basis.IDENTITY
		root_motion_velocity = (basis * travel) / delta

	var current := String(_playback.get_current_node())
	if _action != "":
		# A travel takes a frame to land and a cross-fade to finish; the
		# action is over only once it has been SEEN playing and is gone.
		if current == _action:
			_action_entered = true
		elif _action_entered:
			_action = ""
			_action_entered = false
		if _action != "":
			_was_on_floor = on_floor
			return
	if on_floor:
		_air_time = 0.0
		if not _was_on_floor and (current == "Fall" or current == "Jump"):
			_playback.travel(&"Land" if _states.has("Land") else &"Locomotion")
	else:
		_air_time += delta
		if vertical > 0.5 and _states.has("Jump") and current != "Jump":
			_playback.travel(&"Jump")
		elif vertical <= 0.0 and _air_time >= fall_delay and _states.has("Fall") \
				and current != "Fall" and current != "Land":
			_playback.travel(&"Fall")
	_was_on_floor = on_floor


## Play a one-shot clip; the machine returns to Locomotion when it ends.
func play_action(name: String) -> bool:
	var state := _state_name(name)
	if not _built or _playback == null or not _states.has(state):
		return false
	_action = state
	_action_entered = false
	_playback.travel(StringName(state))
	return true


func _state_name(clip: String) -> String:
	return clip.replace("/", "_")


func _on_finished(anim_name: StringName) -> void:
	if _state_name(String(anim_name)) == _action:
		var done := _action
		_action = ""
		_action_entered = false
		action_finished.emit(done)


func current_state() -> String:
	if _playback == null:
		return ""
	return String(_playback.get_current_node())


## Everything the wiring tool and a QA probe want to know, as one dictionary.
func report() -> Dictionary:
	var clips := PackedStringArray()
	if _player != null:
		clips = _player.get_animation_list()
	return {
		"built": _built,
		"player": String(_player.get_path()) if _player != null else "",
		"body": String(_body.get_path()) if _body != null else "",
		"clips_in_player": clips,
		"present": _present,
		"missing": _missing,
		"states": _states,
		"looped": _looped,
		"speeds": _speeds,
		"errors": _errors,
		"current": current_state(),
		"blend": _blend,
		"libraries": _loaded_libraries,
		"root_motion": _root_motion_active,
		"root_motion_velocity": root_motion_velocity,
	}
