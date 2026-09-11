"""Wire an animated character into a playable Godot scene.

THE GAP THIS CLOSES, measured on three games: blender_animate ends at a .glb
with clips in it, godot_deliver_asset wraps it in a CharacterBody3D, and then
NOTHING PLAYS A CLIP. The template controller has no AnimationPlayer or
AnimationTree; every project re-wrote "velocity -> walk" by hand, and the
ones that did not shipped a statue that slid across the floor with every
gate green (a T-pose still plants its feet).

wire_character() takes the animated .glb the rest of the way:

  1. imports it (import_asset) and reads the clips the ENGINE sees,
  2. resolves clip names to roles - idle / walk / run / jump / fall / land,
     everything else an action - by name, overridable,
  3. installs templates/3d/scripts/character_animator.gd (and the
     third-person controller) into the project when absent, never over
     the project's own copy,
  4. writes scenes/characters/<name>.tscn: CharacterBody3D + the model +
     a capsule fitted to the model + an AnimationTree running the animator,
  5. PROVES IT IN THE ENGINE: instantiates the scene headless, drives the
     animator through stop / walk / run / jump / fall / land / one action,
     and reports the state the machine reached at each step.

The proof is what makes this different from writing a .tscn: a clip name
that resolves to nothing plays happily and moves zero.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Optional

from bgate_adapters import godot as _godot

TEMPLATE_SCRIPTS = Path(__file__).resolve().parent.parent / "templates" / "godot" / "3d" / "scripts"
ANIMATOR_SCRIPT = "character_animator.gd"
IK_SCRIPT = "character_ik.gd"
CONTROLLERS = {
    "third_person": "third_person_controller.gd",
    "player": "player.gd",
    "none": "",
}

# Clip-name vocabulary. Locomotion roles carry a RANK so a pack's walk /
# trot / gallop or walk / jog / run / sprint lands in speed order.
LOCOMOTION_RANK = {
    "idle": 0, "stand": 0, "breathing": 0,
    "sneak": 1, "crouch_walk": 1, "crouch_fwd": 1,
    "walk": 2,
    "trot": 3, "jog": 3,
    "run": 4,
    "gallop": 5, "sprint": 5,
}
AIR_ROLES = {
    "jump": ("jump", "leap", "hop"),
    "fall": ("fall", "falling", "airborne", "air"),
    "land": ("land", "landing"),
}
PROBE_MARK = "BGATE_CHARWIRE:"


def _tokens(name: str) -> list[str]:
    base = name[:-5] if name.endswith("-loop") else name
    return [t for t in re.split(r"[^a-z0-9]+", base.lower()) if t]


def resolve_clips(animations, overrides: Optional[dict] = None) -> dict:
    """Which clip plays which role, from the names alone.

    Returns {"locomotion": [clip...] in speed order, "jump", "fall",
    "land", "actions": [...], "missing": [...], "ambiguous": {...}}.
    `overrides` = {"idle": "Idle_Loop", "walk": ..., "run": ..., "jump":
    ..., "fall": ..., "land": ..., "actions": [...]} pins any of them.
    """
    names = []
    for a in animations or []:
        names.append(a["name"] if isinstance(a, dict) else str(a))
    over = dict(overrides or {})
    used: set = set()
    by_rank: dict = {}
    ambiguous: dict = {}
    for n in names:
        toks = _tokens(n)
        ranks = {LOCOMOTION_RANK[t] for t in toks if t in LOCOMOTION_RANK}
        # crouch_walk-style compounds: the joined form wins over its parts
        joined = "_".join(toks)
        for key, rank in LOCOMOTION_RANK.items():
            if "_" in key and key in joined:
                ranks = {rank}
        if len(ranks) == 1:
            rank = ranks.pop()
            by_rank.setdefault(rank, []).append(n)
    locomotion = []
    for rank in sorted(by_rank):
        cands = by_rank[rank]
        pin = None
        for role, r in LOCOMOTION_RANK.items():
            if r == rank and over.get(role) in names:
                pin = over[role]
        pick = pin or sorted(cands, key=lambda s: ("/" in s, len(_tokens(s)), len(s)))[0]
        if len(cands) > 1 and not pin:
            ambiguous[pick] = [c for c in cands if c != pick]
        # A pin can name a clip another rank already placed (run pinned to a
        # jog): one entry, not two blend points on the same clip.
        if pick in used:
            continue
        locomotion.append(pick)
        used.add(pick)
    for role in ("idle", "walk", "run"):
        if over.get(role) in names and over[role] not in used:
            locomotion.append(over[role])
            used.add(over[role])
    out = {"locomotion": locomotion}
    for role, words in AIR_ROLES.items():
        pick = over.get(role) if over.get(role) in names else None
        if pick is None:
            cands = [n for n in names if n not in used
                     and any(w in _tokens(n) for w in words)]
            if role == "fall":
                # A pack's Jump_Loop is the airborne hold, not the take-off.
                cands += [n for n in names if n not in used and n not in cands
                          and {"jump", "loop"} <= set(_tokens(n))]
            if cands:
                def _rank(s):
                    toks = _tokens(s)
                    if role == "jump":
                        lead = 0 if "start" in toks or "up" in toks else (2 if "loop" in toks else 1)
                    elif role == "land":
                        lead = 0 if "land" in toks else 1
                    else:
                        lead = 0 if "fall" in toks else 1
                    # The character's own clip beats a library's ("lib/clip").
                    return ("/" in s, lead, len(toks), len(s))
                pick = sorted(cands, key=_rank)[0]
                if len(cands) > 1:
                    ambiguous[pick] = [c for c in cands if c != pick]
        out[role] = pick or ""
        if pick:
            used.add(pick)
    ranked = {n for n in names if any(t in LOCOMOTION_RANK for t in _tokens(n))}
    actions = [n for n in (over.get("actions") or []) if n in names]
    # A walk that lost its role to a pinned library walk is superseded, not
    # a one-shot; only unranked leftovers become actions.
    actions += [n for n in names if n not in used and n not in actions and n not in ranked]
    out["actions"] = actions
    missing = []
    if not locomotion:
        missing.append("idle/walk")
    for role in AIR_ROLES:
        if not out[role]:
            missing.append(role)
    out["missing"] = missing
    out["ambiguous"] = ambiguous
    return out


def _read_sidecar(project: Path, lib_res: str) -> dict:
    """The clip list godot_clip_retarget wrote beside its library."""
    if not lib_res.startswith("res://"):
        return {}
    side = project / (lib_res[len("res://"):] + ".json")
    if not side.is_file():
        return {}
    try:
        return json.loads(side.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _gd_array(values) -> str:
    return "PackedStringArray(" + ", ".join(json.dumps(str(v)) for v in values) + ")"


_FACING_GD = '''extends SceneTree
## Which way the character faces WHILE ITS IDLE PLAYS. The rest pose lies
## twice over: an engine retarget turns the skeleton, a pack clip carries its
## own yaw in the Hips track, and a Blender-side repair can flip the toe
## bones. The posed foot direction under the clip the game will actually
## play is the only reading that matched what the player saw.
const MARK := "__MARK__"
var out := {"ok": false}
var sk: Skeleton3D = null
var n := 0
func _find(node: Node, cls: String) -> Node:
	if node.is_class(cls):
		return node
	for c in node.get_children():
		var f := _find(c, cls)
		if f != null:
			return f
	return null
func _say() -> void:
	print(MARK + JSON.stringify(out))
	quit()
func _init() -> void:
	var packed = load("__RES__")
	if packed == null or not (packed is PackedScene):
		_say()
		return
	var s: Node = packed.instantiate()
	get_root().add_child(s)
	sk = _find(s, "Skeleton3D") as Skeleton3D
	var ap := _find(s, "AnimationPlayer") as AnimationPlayer
	if sk == null:
		_say()
		return
	out["ok"] = true
	out["skeleton"] = String(sk.name)
	if ap != null:
		for res in __LIBS__:
			var lib = load(res)
			if lib is AnimationLibrary:
				ap.add_animation_library(res.get_file().get_basename(), lib)
		var clip := "__CLIP__"
		if clip != "" and ap.has_animation(clip):
			ap.play(clip)
			ap.seek(0.2, true)
			out["clip"] = clip
func _process(_d: float) -> bool:
	n += 1
	if n < 3:
		return false
	var a := sk.find_bone("LeftFoot")
	var b := sk.find_bone("LeftToes")
	if a != -1 and b != -1:
		var v: Vector3 = sk.get_bone_global_pose(b).origin - sk.get_bone_global_pose(a).origin
		out["toes_z"] = snappedf(v.normalized().z, 0.001)
	# THE SKIN, POSED, THE WAY THE GPU POSES IT: bone_global_pose * skin bind
	# pose * bind vertex, on the dominant bone. The lowest slab of the posed
	# skin reaches further toward the toes than the heel; that side is front.
	var mi := _find(sk.get_parent(), "MeshInstance3D") as MeshInstance3D
	if mi != null and mi.mesh != null and mi.skin != null and mi.mesh.get_surface_count() > 0:
		var arrays := mi.mesh.surface_get_arrays(0)
		var verts: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
		var bones = arrays[Mesh.ARRAY_BONES]
		var weights = arrays[Mesh.ARRAY_WEIGHTS]
		if bones != null and weights != null and bones.size() >= verts.size() * 4:
			var per := int(bones.size() / verts.size())
			var xforms := {}
			for i in mi.skin.get_bind_count():
				var bi := mi.skin.get_bind_bone(i)
				if bi == -1:
					bi = sk.find_bone(mi.skin.get_bind_name(i))
				if bi != -1:
					xforms[i] = sk.get_bone_global_pose(bi) * mi.skin.get_bind_pose(i)
			var posed := PackedVector3Array()
			posed.resize(verts.size())
			var lo := INF
			var hi := -INF
			for vi in verts.size():
				var best := 0
				var bw := -1.0
				for k in per:
					if weights[vi * per + k] > bw:
						bw = weights[vi * per + k]
						best = bones[vi * per + k]
				var pv: Vector3 = verts[vi]
				if xforms.has(best):
					pv = xforms[best] * verts[vi]
				posed[vi] = pv
				lo = minf(lo, pv.y)
				hi = maxf(hi, pv.y)
			var cut: float = lo + (hi - lo) * 0.06
			var zsum := 0.0
			var zmin := INF
			var zmax := -INF
			for pv in posed:
				zsum += pv.z
				if pv.y <= cut:
					zmin = minf(zmin, pv.z)
					zmax = maxf(zmax, pv.z)
			var body_z: float = zsum / maxf(posed.size(), 1)
			out["skin_ahead"] = snappedf(zmax - body_z, 0.001)
			out["skin_behind"] = snappedf(body_z - zmin, 0.001)
	_say()
	return true
'''


def model_facing(project_dir, res_path: str, timeout: int = 120, *, clip: str = "",
                 libraries: Optional[list] = None) -> dict:
    """Which way the imported model's feet point in its own space. Godot
    moves a body along -Z; an engine retarget (SkeletonProfileHumanoid)
    leaves the skeleton facing +Z, and every clip then walks backwards
    while the body travels forwards. Returns {ok, forward_z, yaw_deg}
    where yaw_deg is the turn the Model node needs (0 or 180)."""
    script = (_FACING_GD.replace("__MARK__", PROBE_MARK + "FACING:")
              .replace("__RES__", str(res_path)).replace("__CLIP__", str(clip or ""))
              .replace("__LIBS__", json.dumps([str(l) for l in (libraries or [])])))
    ran = _godot.run_script(script, project_dir=str(project_dir), timeout=timeout)
    text = (ran.get("stdout") or "") + "\n" + (ran.get("stderr") or "")
    for line in text.splitlines():
        if PROBE_MARK + "FACING:" in line:
            try:
                rec = json.loads(line.split(PROBE_MARK + "FACING:", 1)[1])
            except json.JSONDecodeError:
                break
            if rec.get("ok"):
                ahead = float(rec.get("skin_ahead") or 0.0)
                behind = float(rec.get("skin_behind") or 0.0)
                if ahead > behind * 1.15:
                    fwd, src = 1.0, "posed skin: feet reach +Z"
                elif behind > ahead * 1.15:
                    fwd, src = -1.0, "posed skin: feet reach -Z"
                else:
                    fwd, src = float(rec.get("toes_z") or 0.0), "posed toe bones (skin unreadable)"
                rec["read_from"] = "%s under %s" % (src, rec.get("clip") or "rest")
                rec["forward_z"] = fwd
                rec["yaw_deg"] = 180 if fwd > 0.2 else 0
                return rec
    return {"ok": False, "yaw_deg": 0}


def character_scene_text(model_res: str, *, node_name: str, model_uid: str = "",
                         controller_res: str = "", animator_res: str = "",
                         bounds_size, bounds_position, resolved: dict,
                         interact_range: float = 1.2, libraries: Optional[list] = None,
                         ik_res: str = "", model_yaw_deg: int = 0) -> str:
    """The .tscn: body, model, fitted capsule, the animator, an interact probe.

    THE MODEL KEEPS ITS AUTHORED ORIGIN. blender_animate grounds a character
    at its feet, so the capsule is lifted to half the height instead of the
    model being sunk to the capsule's centre - the delivered-into-the-floor
    failure deliver_asset documents. A model whose bounds start well below
    zero (centred in Blender) is lifted so its feet land on the origin.
    """
    sx, sy, sz = (float(v) for v in bounds_size)
    px, py, pz = (float(v) for v in bounds_position)
    radius, height = _godot._capsule_for_bounds(sx, sy, sz)
    lift = -py if py < -0.15 * max(sy, 1e-6) else 0.0
    ext = ['[ext_resource type="PackedScene" '
           + (f'uid="{model_uid}" ' if model_uid else "")
           + f'path="{model_res}" id="1_model"]']
    if controller_res:
        ext.append(f'[ext_resource type="Script" path="{controller_res}" id="2_ctrl"]')
    if animator_res:
        ext.append(f'[ext_resource type="Script" path="{animator_res}" id="3_anim"]')
    if ik_res:
        ext.append(f'[ext_resource type="Script" path="{ik_res}" id="4_ik"]')
    steps = len(ext) + 2 + 1
    lines = [f"[gd_scene load_steps={steps} format=3]", "", *ext, "",
             '[sub_resource type="CapsuleShape3D" id="CapsuleShape3D_body"]',
             f"radius = {radius:.4f}", f"height = {height:.4f}", "",
             '[sub_resource type="SphereShape3D" id="SphereShape3D_reach"]',
             f"radius = {interact_range:.3f}", "",
             f'[node name="{node_name}" type="CharacterBody3D"]',
             "collision_layer = 2", "collision_mask = 1"]
    if controller_res:
        lines.append('script = ExtResource("2_ctrl")')
    lines += ["",
              '[node name="Model" parent="." instance=ExtResource("1_model")]',
              # A model whose feet point +Z is turned to face the body's -Z.
              (f"transform = Transform3D(-1, 0, 0, 0, 1, 0, 0, 0, -1, 0, {lift:.4f}, 0)"
               if int(model_yaw_deg) == 180 else
               f"transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, {lift:.4f}, 0)"),
              "",
              '[node name="CollisionShape3D" type="CollisionShape3D" parent="."]',
              f"transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, {height * 0.5:.4f}, 0)",
              'shape = SubResource("CapsuleShape3D_body")', ""]
    loco = resolved.get("locomotion") or []
    lines += ['[node name="Animator" type="AnimationTree" parent="."]']
    if animator_res:
        lines.append('script = ExtResource("3_anim")')
    lines += ['anim_player = NodePath("../Model/AnimationPlayer")',
              'body_path = NodePath("..")',
              f"locomotion = {_gd_array(loco)}",
              f'clip_jump = {json.dumps(resolved.get("jump") or "")}',
              f'clip_fall = {json.dumps(resolved.get("fall") or "")}',
              f'clip_land = {json.dumps(resolved.get("land") or "")}',
              f"actions = {_gd_array(resolved.get('actions') or [])}"]
    if libraries:
        lines.append(f"libraries = {_gd_array(libraries)}")
    lines.append("")
    if ik_res:
        # IK runs AFTER the tree (a later sibling processes later), so the
        # feet are placed on top of the clip rather than under it.
        lines += ['[node name="IK" type="Node" parent="."]',
                  'script = ExtResource("4_ik")',
                  'body_path = NodePath("..")', ""]
    lines += ['[node name="InteractRange" type="Area3D" parent="."]',
              f"transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, {height * 0.5:.4f}, 0)",
              "collision_layer = 0", "collision_mask = 4", "monitorable = false", "",
              '[node name="ReachShape" type="CollisionShape3D" parent="InteractRange"]',
              'shape = SubResource("SphereShape3D_reach")', ""]
    return "\n".join(lines)


def install_script(project_dir, name: str, dest_rel: str = "scripts") -> dict:
    """Copy a template script into the project unless the project has one."""
    src = TEMPLATE_SCRIPTS / name
    dest = Path(project_dir) / dest_rel / name
    if dest.is_file():
        return {"res": f"res://{dest_rel}/{name}", "installed": False, "reason": "already present"}
    if not src.is_file():
        return {"res": "", "installed": False, "reason": f"no template {src}"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return {"res": f"res://{dest_rel}/{name}", "installed": True}


_PROBE_GD = '''extends Node
## character_wire's proof: instance the scene, drive the animator through
## every state with the manual inputs, read the state it reached a frame
## later. A clip that resolves to nothing plays happily and moves zero -
## only the machine can say whether it went where it was sent.
const MARK := "__MARK__"
var out := {"ok": false, "steps": []}
var inst: Node = null
var anim: Node = null
var stage := 0
var wait := 0
var actions: Array = []
var ik: Node = null
var slope_stage: Node3D = null
var probe_ik := __IK__

func _slope() -> void:
	## A 15-degree ramp under the character, so the two feet meet ground at
	## different heights - the case flat-floor clips get wrong.
	slope_stage = Node3D.new()
	add_child(slope_stage)
	var ramp := StaticBody3D.new()
	var shape := CollisionShape3D.new()
	var box := BoxShape3D.new()
	box.size = Vector3(6, 0.4, 6)
	shape.shape = box
	ramp.add_child(shape)
	ramp.rotation_degrees = Vector3(15, 0, 0)
	ramp.position = Vector3(0, -0.2, 0)
	slope_stage.add_child(ramp)
	inst.set("position", Vector3(0, 0.05, 0))

func _ready() -> void:
	var path := "__SCENE__"
	if not ResourceLoader.exists(path):
		out["error"] = "no scene at %s" % path
		_say()
		return
	var packed = load(path)
	if packed == null or not (packed is PackedScene):
		out["error"] = "%s is not a PackedScene" % path
		_say()
		return
	inst = packed.instantiate()
	add_child(inst)
	anim = inst.get_node_or_null("Animator")
	if anim == null:
		out["error"] = "no Animator node under %s" % inst.name
		_say()
		return
	if not anim.has_method("report"):
		out["error"] = "Animator has no report(): the script did not attach"
		_say()
		return
	anim.set("manual", true)
	anim.set("fall_delay", 0.0)
	anim.set("speed_smoothing", 1000.0)
	ik = inst.get_node_or_null("IK")
	if probe_ik and ik != null and ik.has_method("report"):
		_slope()
		# The controller must not fall through the probe: gravity stays, floor
		# contact is what the IK weight reads, so let it settle on the ramp.
	wait = 6

func _drive(speed: float, on_floor: bool, vertical: float) -> void:
	anim.set("manual_speed", speed)
	anim.set("manual_on_floor", on_floor)
	anim.set("manual_vertical", vertical)

func _physics_process(_delta: float) -> void:
	if anim == null:
		return
	if wait > 0:
		wait -= 1
		return
	var rep: Dictionary = anim.call("report")
	if stage == 0:
		out["report"] = rep
		var speeds: Array = Array(rep.get("speeds", []))
		out["walk_speed"] = float(speeds[1]) if speeds.size() > 1 else 1.5
		out["top_speed"] = float(speeds[speeds.size() - 1]) if speeds.size() > 1 else 4.0
		actions = Array(rep.get("states", [])).filter(func(s): return not (s in ["Locomotion", "Jump", "Fall", "Land"]))
		_drive(0.0, true, 0.0)
	else:
		out["steps"].append({"step": stage, "state": rep.get("current", ""), "blend": rep.get("blend", 0.0)})
	stage += 1
	match stage:
		1: pass
		2: _drive(out["walk_speed"], true, 0.0)
		3: _drive(out["top_speed"], true, 0.0)
		4: _drive(out["top_speed"], false, 4.0)
		5: _drive(out["top_speed"], false, -4.0)
		6: _drive(0.0, true, 0.0)
		7:
			_drive(0.0, true, 0.0)
			if actions.size() > 0:
				out["action_tried"] = actions[0]
				out["action_accepted"] = anim.call("play_action", actions[0])
		8: pass
		9:
			if ik != null and ik.has_method("report"):
				_drive(0.0, true, 0.0)
				ik.set("enabled", true)
		10:
			if ik != null and ik.has_method("report"):
				out["ik"] = ik.call("report")
		_:
			out["ok"] = true
			_say()
			return
	wait = 14 if stage < 9 else 40

func _say() -> void:
	print(MARK + JSON.stringify(out))
	get_tree().quit()
'''


def probe_character(project_dir, scene_res: str, timeout: int = 180,
                    ik: bool = False) -> dict:
    """Run the animator through its states in the engine and read them back;
    with `ik`, stand the character on a ramp afterwards and read the IK."""
    script = (_PROBE_GD.replace("__MARK__", PROBE_MARK).replace("__SCENE__", scene_res)
              .replace("__IK__", "true" if ik else "false"))
    ran = _godot.run_script(script, project_dir=project_dir, timeout=timeout)
    text = (ran.get("stdout") or "") + "\n" + (ran.get("stderr") or "")
    found = None
    for line in text.splitlines():
        if PROBE_MARK in line:
            try:
                found = json.loads(line.split(PROBE_MARK, 1)[1])
            except json.JSONDecodeError:
                found = None
    if found is None:
        return {"ok": False, "measured": False,
                "error": ran.get("error") or "the probe printed no report",
                "stderr": (ran.get("stderr") or "")[-1200:],
                "stdout": (ran.get("stdout") or "")[-800:]}
    found["measured"] = True
    found["engine_errors"] = [l for l in text.splitlines()
                              if "ERROR" in l or "SCRIPT ERROR" in l][:12]
    return found


def judge_probe(probe: dict, resolved: dict) -> dict:
    """What the drive proved: one row per state the scene claimed to have."""
    if not probe.get("measured") or not probe.get("ok"):
        return {"passed": False, "issues": [probe.get("error") or "probe did not finish"]}
    steps = {s["step"]: s for s in probe.get("steps") or []}
    rep = probe.get("report") or {}
    states = list(rep.get("states") or [])
    issues = []
    if not rep.get("built"):
        issues.append("the animator did not build: %s" % "; ".join(rep.get("errors") or []))
    for miss in rep.get("missing") or []:
        issues.append("clip %r named in the scene is not in the model" % miss)

    def state_at(step):
        return (steps.get(step) or {}).get("state", "")
    if state_at(1) != "Locomotion":
        issues.append("standing still did not sit in Locomotion (got %r)" % state_at(1))
    if len(resolved.get("locomotion") or []) > 1:
        b_stop = float((steps.get(1) or {}).get("blend", 0.0))
        b_walk = float((steps.get(2) or {}).get("blend", 0.0))
        if not b_walk > b_stop:
            issues.append("walking did not move the locomotion blend (%.2f -> %.2f)"
                          % (b_stop, b_walk))
    if "Jump" in states and state_at(4) != "Jump":
        issues.append("leaving the floor upward did not enter Jump (got %r)" % state_at(4))
    if "Fall" in states and state_at(5) not in ("Fall", "Jump"):
        issues.append("falling did not enter Fall (got %r)" % state_at(5))
    if ("Jump" in states or "Fall" in states):
        want = "Land" if "Land" in states else "Locomotion"
        if state_at(6) not in (want, "Locomotion"):
            issues.append("touchdown did not enter %s (got %r)" % (want, state_at(6)))
    if probe.get("action_tried"):
        if not probe.get("action_accepted"):
            issues.append("play_action(%r) was refused" % probe["action_tried"])
        elif state_at(8) != probe["action_tried"]:
            issues.append("play_action(%r) did not enter its state (got %r)"
                          % (probe["action_tried"], state_at(8)))
    ik_rec = probe.get("ik")
    ik_verdict = None
    if isinstance(ik_rec, dict):
        hits = ik_rec.get("hits") or {}
        ik_verdict = {"feet": ik_rec.get("feet") or [], "head_look": ik_rec.get("head_look"),
                      "weight": ik_rec.get("weight"), "pelvis_drop": ik_rec.get("pelvis_drop"),
                      "errors": ik_rec.get("errors") or [],
                      "foot_error_m": {f: round(float(h.get("error", 0.0)), 4)
                                       for f, h in hits.items()}}
        for err in ik_rec.get("errors") or []:
            issues.append("IK: " + str(err))
        if not ik_rec.get("feet"):
            issues.append("IK found no foot chain to solve")
        for foot, h in hits.items():
            if not h.get("hit"):
                issues.append("IK: no ground under %s on the probe ramp" % foot)
            elif float(h.get("error", 0.0)) > 0.06:
                issues.append("IK: %s sits %.1f cm off the ramp under it"
                              % (foot, 100.0 * float(h.get("error", 0.0))))
        if float(ik_rec.get("weight") or 0.0) < 0.5:
            issues.append("IK weight stayed at %.2f on the ramp - the body never "
                          "reported floor contact" % float(ik_rec.get("weight") or 0.0))
    return {"passed": not issues, "issues": issues, "states": states,
            "looped": rep.get("looped") or [], "speeds": rep.get("speeds") or [],
            "libraries": rep.get("libraries") or [], "root_motion": rep.get("root_motion"),
            "ik": ik_verdict, "engine_errors": probe.get("engine_errors") or []}


def wire_character(project_dir, glb, *, name: str = "", clips: Optional[dict] = None,
                   controller: str = "third_person", script_res: str = "",
                   dest_rel: str = "assets", scene_rel: str = "scenes/characters",
                   overwrite: bool = False, probe: bool = True,
                   libraries: Optional[list] = None, ik: bool = True,
                   timeout: int = 300) -> dict:
    """The whole path: import, resolve, install, write, prove. See module doc.

    controller  third_person (template controller, needs move_*/jump/sprint
                actions and the BGateTelemetry autoload - both in every
                scaffolded project), player (the first-person template), or
                none (an NPC: the animator alone, drive velocity yourself).
    script_res  a res:// script to attach instead of a template controller.
    libraries   res:// AnimationLibrary files (godot_clip_retarget's) whose
                clips join the resolution as "<stem>/<clip>".
    ik          add character_ik.gd: feet placed on the ground by ray and
                SkeletonIK3D, pelvis lowered to the lower foot, head
                tracking when a look_target is set. The probe stands the
                character on a 15-degree slope and measures each foot's
                distance from the ground under it.
    """
    project = Path(project_dir)
    if not (project / "project.godot").is_file():
        return {"ok": False, "error": f"no project.godot in {project}"}
    if controller not in CONTROLLERS:
        return {"ok": False, "error": "controller must be one of %s" % (tuple(CONTROLLERS),)}
    steps = []
    first = _godot.import_asset(str(project), str(glb), dest_rel=dest_rel, timeout=timeout)
    steps.append({"step": "import", "ok": bool(first.get("ok")),
                  "errors": (first.get("import") or {}).get("errors", [])})
    if not first.get("ok"):
        return {"ok": False, "error": "the engine could not load the model",
                "detail": first, "steps": steps}
    view = first.get("engine_view") or {}
    res_path = first["res_path"]
    asset_rel = str(Path(first["copied_to"]).relative_to(project)).replace("\\", "/")
    animations = []
    for player in view.get("animation_players") or []:
        animations += [a.get("name") for a in player.get("animations") or []]
    if not animations:
        animations = list(view.get("animations") or [])
    lib_res = []
    for lib in libraries or []:
        side = _read_sidecar(project, str(lib))
        stem = Path(str(lib)).stem
        if not side:
            return {"ok": False, "steps": steps,
                    "error": "%s has no .res.json sidecar - godot_clip_retarget "
                             "writes one; pass its `library`" % lib}
        lib_res.append(str(lib))
        animations += ["%s/%s" % (stem, c) for c in side.get("clips") or []]
    if not animations:
        return {"ok": False, "steps": steps, "res_path": res_path,
                "error": "the engine sees NO animations in %s - this is not an "
                         "animated character. Run blender_animate first; a rig "
                         "alone is a statue." % res_path}
    if not view.get("skeletons"):
        return {"ok": False, "steps": steps, "res_path": res_path,
                "error": "no Skeleton3D in %s - the clips have nothing to drive" % res_path}
    resolved = resolve_clips(animations, clips)
    steps.append({"step": "resolve", "ok": bool(resolved["locomotion"]),
                  "resolved": resolved})
    if not resolved["locomotion"]:
        return {"ok": False, "steps": steps, "res_path": res_path, "clips": animations,
                "error": "no clip reads as idle or walk among %s; pass clips="
                         "{'idle': ..., 'walk': ...}" % animations}

    stem = name or Path(glb).stem
    node_name = re.sub(r"[^A-Za-z0-9_]", "", stem) or "Character"
    installed = {"animator": install_script(project, ANIMATOR_SCRIPT)}
    animator_res = installed["animator"]["res"]
    ik_res = ""
    if ik:
        installed["ik"] = install_script(project, IK_SCRIPT)
        ik_res = installed["ik"]["res"]
    controller_res = script_res
    if not controller_res and CONTROLLERS[controller]:
        installed["controller"] = install_script(project, CONTROLLERS[controller])
        controller_res = installed["controller"]["res"]
        if controller == "third_person":
            installed["camera_rig"] = install_script(project, "camera_rig.gd")
    steps.append({"step": "scripts", "ok": bool(animator_res), "installed": installed})
    if not animator_res:
        return {"ok": False, "steps": steps, "error": "could not install the animator script"}

    size = (view.get("size_check") or {}).get("metres") or [1.0, 1.8, 1.0]
    origin = [0.0, 0.0, 0.0]
    for mesh in view.get("meshes") or []:
        pos = mesh.get("aabb_global_position")
        if pos:
            origin = [min(origin[i], pos[i]) for i in range(3)] if origin != [0.0, 0.0, 0.0] else list(pos)
    scene_dir = project / scene_rel
    scene_dir.mkdir(parents=True, exist_ok=True)
    scene_path = scene_dir / f"{stem}.tscn"
    scene_res = f"res://{scene_rel}/{stem}.tscn"
    if scene_path.is_file() and not overwrite:
        return {"ok": False, "steps": steps, "scene": scene_res,
                "error": "%s exists; pass overwrite=True to regenerate it (hand "
                         "edits are lost) or wire under another name" % scene_res}
    idle_clip = (resolved.get("locomotion") or [""])[0]
    facing = model_facing(project, res_path, timeout=timeout, clip=idle_clip, libraries=lib_res)
    steps.append({"step": "facing", "ok": bool(facing.get("ok")),
                  "forward_z": facing.get("forward_z"), "yaw_deg": facing.get("yaw_deg", 0)})
    text = character_scene_text(res_path, node_name=node_name,
                                model_uid=_godot._import_uid(str(project), asset_rel),
                                controller_res=controller_res, animator_res=animator_res,
                                bounds_size=size, bounds_position=origin, resolved=resolved,
                                libraries=lib_res, ik_res=ik_res,
                                model_yaw_deg=int(facing.get("yaw_deg", 0)))
    scene_path.write_text(text, encoding="utf-8")
    steps.append({"step": "scene", "ok": True, "path": str(scene_path)})
    result = {"ok": True, "scene": scene_res, "scene_path": str(scene_path),
              "res_path": res_path, "node": node_name, "clips": animations,
              "facing": facing,
              "resolved": resolved, "installed": installed, "steps": steps,
              "warnings": []}
    if resolved.get("missing"):
        result["warnings"].append("no clip for: %s (those states are left out; "
                                  "the machine stays in Locomotion there)"
                                  % ", ".join(resolved["missing"]))
    if resolved.get("ambiguous"):
        result["warnings"].append("picked by name over alternatives: %s"
                                  % json.dumps(resolved["ambiguous"]))
    result["libraries"] = lib_res
    result["ik"] = bool(ik_res)
    if probe:
        measured = probe_character(str(project), scene_res, timeout=timeout,
                                   ik=bool(ik_res))
        verdict = judge_probe(measured, resolved)
        result["probe"] = {"measured": measured.get("measured", False),
                           "steps": measured.get("steps"), **verdict}
        steps.append({"step": "probe", "ok": bool(verdict.get("passed"))})
        if not verdict.get("passed"):
            result["ok"] = False
            result["error"] = ("the scene is written but the engine did not prove "
                               "it: " + "; ".join(verdict.get("issues") or []))
        if verdict.get("looped"):
            result["warnings"].append("forced to loop at runtime (export with "
                                      "loop_suffix=True to bake it): %s"
                                      % ", ".join(verdict["looped"]))
    return result
