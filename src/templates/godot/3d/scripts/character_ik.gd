extends Node
class_name BGateCharacterIK
## Feet that meet the ground and a head that looks where it is told.
##
## A clip is authored on a flat floor. On a ramp, a step, or a kerb one
## foot hangs in the air and the other sinks - the "rails shattered on
## slopes" class of defect, on a body. This node fixes it in the ENGINE,
## after the animation, where the ground actually is:
##
##   feet   one TwoBoneIK modifier per foot (UpperLeg -> LowerLeg -> Foot),
##          its target a marker this node drops onto whatever the ray under
##          the foot hits; the pelvis is lowered by the lowest foot's drop so
##          the higher leg does not have to over-reach. Weight fades in on
##          the floor and out in the air.
##   head   a LookAtModifier3D on the Head bone toward `look_target`, when
##          the engine has the class (4.4+); silently absent otherwise and
##          reported by report().
##
## THE SOLVER IS OURS. SkeletonIK3D (deprecated) reported running and moved
## nothing on a retargeted skeleton, tree on or off - measured, 40 frames,
## foot at rest to the millimetre. TwoBoneIK below is a SkeletonModifier3D
## with an analytic two-bone solve, so it runs after the animation pose
## every frame and its result is a number this node can read back.
##
## Works on any rig whose foot bones follow the profile names - LeftFoot /
## RightFoot for a humanoid, <Left|Right><Front|Back>Foot for a quadruped -
## and finds the Skeleton3D under the body on its own.


class TwoBoneIK extends SkeletonModifier3D:
	## Analytic two-bone IK in skeleton space: bend the middle joint until the
	## tip can reach, aim the root at the target, keep the tip's own facing.
	var root_bone := ""
	var mid_bone := ""
	var tip_bone := ""
	var target: Node3D = null
	## Where the middle joint bends toward, skeleton space.
	var pole := Vector3.FORWARD
	var weight := 0.0
	var last_error := 0.0
	var debug := {}

	func _process_modification() -> void:
		var sk := get_skeleton()
		if sk == null or target == null or weight <= 0.0:
			return
		var ri := sk.find_bone(root_bone)
		var mi := sk.find_bone(mid_bone)
		var ti := sk.find_bone(tip_bone)
		if ri == -1 or mi == -1 or ti == -1:
			return
		var to_skel := sk.global_transform.affine_inverse()
		var t: Vector3 = to_skel * target.global_position
		var root_g := sk.get_bone_global_pose(ri)
		var mid_g := sk.get_bone_global_pose(mi)
		var tip_g := sk.get_bone_global_pose(ti)
		var a := root_g.origin
		var b := mid_g.origin
		var c := tip_g.origin
		var l1 := (b - a).length()
		var l2 := (c - b).length()
		if l1 < 1e-5 or l2 < 1e-5:
			return
		var d := clampf((t - a).length(), 0.001, (l1 + l2) * 0.999)
		# The interior angle the knee needs, and the one it has.
		var want := acos(clampf((l1 * l1 + l2 * l2 - d * d) / (2.0 * l1 * l2), -1.0, 1.0))
		var cur := (a - b).angle_to(c - b)
		var axis := (b - a).cross(c - b)
		if axis.length() < 1e-6:
			axis = (b - a).cross(pole - a)
		if axis.length() < 1e-6:
			axis = Vector3.RIGHT
		axis = axis.normalized()
		var old_mid_rot := sk.get_bone_pose_rotation(mi)
		var old_root_rot := sk.get_bone_pose_rotation(ri)
		var old_tip_rot := sk.get_bone_pose_rotation(ti)
		var old_tip_basis := tip_g.basis
		# Rotate the shin about the knee; the sign that lands the reach wins.
		var best_rot := old_mid_rot
		var best_gap := INF
		for sign in [1.0, -1.0]:
			var delta: float = (want - cur) * sign
			var new_basis := Basis(axis, delta) * mid_g.basis
			var local := (root_g.basis.inverse() * new_basis).get_rotation_quaternion()
			sk.set_bone_pose_rotation(mi, local)
			var c2 := sk.get_bone_global_pose(ti).origin
			var gap := absf((c2 - a).length() - d)
			if gap < best_gap:
				best_gap = gap
				best_rot = local
		sk.set_bone_pose_rotation(mi, best_rot)
		debug = {"d": d, "reach": l1 + l2, "want": want, "cur": cur, "best_gap": best_gap, "axis": axis}
		# Aim the whole leg: the tip's current direction onto the target's.
		var c3 := sk.get_bone_global_pose(ti).origin
		var from := (c3 - a).normalized()
		var to := (t - a).normalized()
		var parent := sk.get_bone_parent(ri)
		var parent_basis := sk.get_bone_global_pose(parent).basis if parent != -1 else Basis.IDENTITY
		if from.length() > 0.0 and to.length() > 0.0 and from.angle_to(to) > 1e-5:
			var swing := Quaternion(from, to)
			var new_root := Basis(swing) * root_g.basis
			sk.set_bone_pose_rotation(ri, (parent_basis.inverse() * new_root).get_rotation_quaternion())
		# The foot keeps the facing the clip gave it.
		var mid_now := sk.get_bone_global_pose(mi).basis
		sk.set_bone_pose_rotation(ti, (mid_now.inverse() * old_tip_basis).get_rotation_quaternion())
		# Blend toward the solve by weight.
		if weight < 1.0:
			sk.set_bone_pose_rotation(ri, old_root_rot.slerp(sk.get_bone_pose_rotation(ri), weight))
			sk.set_bone_pose_rotation(mi, old_mid_rot.slerp(sk.get_bone_pose_rotation(mi), weight))
			sk.set_bone_pose_rotation(ti, old_tip_rot.slerp(sk.get_bone_pose_rotation(ti), weight))
		last_error = (sk.get_bone_global_pose(ti).origin - t).length()


## The body (CharacterBody3D) the skeleton hangs under. Default: parent.
@export var body_path: NodePath = NodePath("..")
## Foot bones. Empty = every profile foot name the skeleton has.
@export var feet: PackedStringArray = PackedStringArray()
@export var head_bone := "Head"
## What the head tracks. Empty = no head tracking.
@export var look_target: NodePath
## Ray from this far above the foot's rest height, this far down.
@export var ray_up := 0.5
@export var ray_down := 1.2
## How fast IK weight and pelvis offset follow the ground.
@export var blend_speed := 10.0
## Largest pelvis drop, metres.
@export var max_pelvis_drop := 0.35
@export var enabled := true
## Physics layers the ground rays hit.
@export_flags_3d_physics var ground_mask := 1

var _body: Node3D = null
var _skeleton: Skeleton3D = null
var _iks := {}          # foot -> TwoBoneIK
var _targets := {}      # foot -> Node3D marker
var _rest_height := {}  # foot -> metres the foot bone sits above the floor at rest
var _hits := {}         # foot -> {hit: bool, y: float, error: float}
var _weight := 0.0
var _pelvis := 0.0
var _look: Node = null
var _errors: PackedStringArray = PackedStringArray()
var _model_rest_y := 0.0


func _find_skeleton(node: Node) -> Skeleton3D:
	if node == null:
		return null
	if node is Skeleton3D:
		return node
	for c in node.get_children():
		var f := _find_skeleton(c)
		if f != null:
			return f
	return null


func _ready() -> void:
	# Deferred: the body is still setting up its children when this runs,
	# and add_child() on it fails with "parent busy" (measured).
	call_deferred("_setup")


func _model() -> Node3D:
	var model := _skeleton.get_parent()
	while model != null and model.get_parent() != _body and model.get_parent() != null:
		model = model.get_parent()
	return model as Node3D


func _setup() -> void:
	_body = get_node_or_null(body_path) as Node3D
	_skeleton = _find_skeleton(_body if _body != null else get_parent())
	if _skeleton == null:
		_errors.append("no Skeleton3D under %s" % (body_path))
		return
	var wanted := feet
	if wanted.is_empty():
		for n in ["LeftFoot", "RightFoot", "LeftFrontFoot", "RightFrontFoot",
				"LeftBackFoot", "RightBackFoot"]:
			if _skeleton.find_bone(n) != -1:
				wanted.append(n)
	var floor_y: float = (_body.global_position.y if _body != null else 0.0)
	for foot in wanted:
		var tip := _skeleton.find_bone(foot)
		if tip == -1:
			_errors.append("no bone %s" % foot)
			continue
		var upper := foot.trim_suffix("Foot") + "UpperLeg"
		var lower := foot.trim_suffix("Foot") + "LowerLeg"
		if _skeleton.find_bone(upper) == -1 or _skeleton.find_bone(lower) == -1:
			_errors.append("no %s/%s chain above %s" % [upper, lower, foot])
			continue
		var marker := Marker3D.new()
		marker.name = foot + "IKTarget"
		(_body if _body != null else self).add_child(marker)
		var ik := TwoBoneIK.new()
		ik.name = foot + "IK"
		ik.root_bone = upper
		ik.mid_bone = lower
		ik.tip_bone = foot
		ik.target = marker
		ik.weight = 0.0
		# The knee bends the way the rig was built: toward where the middle
		# joint already sits, projected out from the leg line.
		var a: Vector3 = _skeleton.get_bone_global_rest(_skeleton.find_bone(upper)).origin
		var m: Vector3 = _skeleton.get_bone_global_rest(_skeleton.find_bone(lower)).origin
		var b: Vector3 = _skeleton.get_bone_global_rest(tip).origin
		var axis := (b - a).normalized()
		var off := (m - a) - axis * (m - a).dot(axis)
		if off.length() < 0.02:
			off = Vector3.FORWARD
		ik.pole = m + off.normalized() * 0.5
		_skeleton.add_child(ik)
		_iks[foot] = ik
		_targets[foot] = marker
		var rest := _skeleton.global_transform * _skeleton.get_bone_global_rest(tip)
		_rest_height[foot] = rest.origin.y - floor_y
		marker.global_position = rest.origin
	var model := _model()
	if model != null:
		_model_rest_y = model.position.y
	if look_target != NodePath("") and _skeleton.find_bone(head_bone) != -1:
		if ClassDB.class_exists("LookAtModifier3D"):
			_look = ClassDB.instantiate("LookAtModifier3D")
			_look.name = "HeadLook"
			_skeleton.add_child(_look)
			_look.set("bone_name", head_bone)
			_look.set("target_node", _look.get_path_to(get_node(look_target)))
			_look.set("use_angle_limitation", true)
			_look.set("primary_limit_angle", deg_to_rad(70.0))
			_look.set("influence", 1.0)
		else:
			_errors.append("LookAtModifier3D is not in this engine (4.4+); head tracking off")


func _physics_process(delta: float) -> void:
	if _skeleton == null or _iks.is_empty():
		return
	var on_floor := true
	if _body is CharacterBody3D:
		on_floor = (_body as CharacterBody3D).is_on_floor()
	var want := 1.0 if (enabled and on_floor) else 0.0
	_weight = lerpf(_weight, want, minf(1.0, blend_speed * delta))
	var space := get_viewport().get_world_3d().direct_space_state
	var body_y := _body.global_position.y if _body != null else 0.0
	var lowest := 0.0
	for foot in _iks:
		var ik: TwoBoneIK = _iks[foot]
		var tip := _skeleton.find_bone(foot)
		var pose := _skeleton.global_transform * _skeleton.get_bone_global_pose(tip)
		var from := pose.origin + Vector3.UP * ray_up
		var query := PhysicsRayQueryParameters3D.create(from, from + Vector3.DOWN * (ray_up + ray_down), ground_mask)
		if _body is CollisionObject3D:
			query.exclude = [(_body as CollisionObject3D).get_rid()]
		var hit := space.intersect_ray(query)
		var rec := {"hit": not hit.is_empty(), "y": 0.0, "error": 0.0}
		if not hit.is_empty():
			var ground_y: float = hit["position"].y
			# The clip's floor is the body's origin; the foot keeps whatever
			# height the clip gave it above THAT floor, shifted by how far
			# the real ground under it differs. A swinging foot stays in
			# the air; a planted one meets the slope. Pulling every foot to
			# ground + rest dragged the swing leg down (measured 3.6 cm).
			# The pose read here already carries the pelvis drop, so the
			# ground delta is taken against the clip's ORIGINAL floor
			# (body minus the drop) - or the downhill foot is asked to reach
			# the drop twice and a straight leg comes up 7 cm short.
			var target_y: float = pose.origin.y + (ground_y - (body_y - _pelvis))
			var marker: Node3D = _targets[foot]
			marker.global_position = Vector3(pose.origin.x, target_y, pose.origin.z)
			rec["y"] = ground_y
			lowest = minf(lowest, ground_y - body_y)
			# MEASURED AFTER THE SOLVE, NOT HERE: this runs in physics, before
			# the animation re-poses and the modifier re-solves for the frame,
			# so the pose read above is the clip's, not the result. The solver
			# records its own distance to the target when it finishes.
			rec["error"] = ik.last_error if ik.weight > 0.5 else absf(pose.origin.y - target_y)
		_hits[foot] = rec
		ik.weight = _weight if rec["hit"] else 0.0
	# Pelvis follows the lowest foot so the other leg can stay planted.
	var want_drop := clampf(-lowest, 0.0, max_pelvis_drop) * _weight
	_pelvis = lerpf(_pelvis, want_drop, minf(1.0, blend_speed * delta))
	var model := _model()
	if model != null:
		model.position.y = _model_rest_y - _pelvis


func _solver_state() -> Dictionary:
	var out := {}
	for foot in _iks:
		var ik: TwoBoneIK = _iks[foot]
		var marker: Node3D = _targets[foot]
		var tip := _skeleton.find_bone(foot)
		var pose := _skeleton.global_transform * _skeleton.get_bone_global_pose(tip)
		out[foot] = {"weight": ik.weight, "solve_error": ik.last_error,
			"target": marker.global_position, "foot": pose.origin, "debug": ik.debug}
	return out


func report() -> Dictionary:
	return {
		"skeleton": String(_skeleton.get_path()) if _skeleton != null else "",
		"feet": _iks.keys(),
		"solvers": _solver_state(),
		"rest_height": _rest_height,
		"hits": _hits,
		"weight": _weight,
		"pelvis_drop": _pelvis,
		"head_look": _look != null,
		"errors": _errors,
	}
