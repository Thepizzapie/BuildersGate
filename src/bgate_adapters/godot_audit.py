"""Scene audit and export verification for 3D Godot projects.

Two tools that exist because every one of these defects was diagnosed and
hand-fixed inside a game, and none of them had a gate in the harness:

* the game still BOOTED into the scaffold demo after a full production run
  (every gate named its own scene; nobody loaded the one the player gets);
* one `.tscn` sub_resource shared by eight instances, resized in `_ready()`,
  so every instance got the LAST instance's size - 130 assertions green;
* props floating off the surface they were meant to rest on;
* a landing surface with a book on it, a counter under a cabinet - the gates
  measured the surface and never the space above it;
* colliders missing, or a mesh whose collider no longer matched it;
* per-instance overrides and materials silently dropped in the EXPORTED pck
  while every editor-run screenshot was fine.

:func:`audit` runs the static checks in Python and the geometric ones in the
engine (world-space AABBs, rays down for support, a ray grid up for headroom).
:func:`export_verify` fingerprints a scene twice - loaded from the project and
loaded from the pck - and diffs the two.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import godot as _godot

# ------------------------------------------------------------------ static

# What the scaffold's 3D demo scene is made of. Any two of these in the scene
# the game boots into means production never replaced the template.
_SCAFFOLD_MARKS = (
    ('name="BGateDemo"', "the scaffold's root node name"),
    ('size = Vector3(40, 1, 40)', "the scaffold's 40 m ground slab"),
    ('path="res://scripts/player.gd"', "the scaffold's player script"),
)

_NODE_RE = re.compile(r'^\[node name="([^"]+)"(?: type="([^"]+)")?(?: parent="([^"]*)")?'
                      r'(?: instance=ExtResource\("([^"]+)"\))?[^\]]*\]', re.M)
_SUB_RE = re.compile(r'^\[sub_resource type="([^"]+)" id="([^"]+)"\]', re.M)
_EXT_RE = re.compile(r'^\[ext_resource type="([^"]+)"(?: uid="[^"]*")? path="([^"]+)" id="([^"]+)"\]', re.M)
_LOCAL_RE = re.compile(r'^resource_local_to_scene = true', re.M)

# A script that assigns into a mesh/shape/material at runtime. Matched against
# the whole script text; the property names are the ones a graybox sets.
_MUTATION_RE = re.compile(
    r'\b(mesh|shape|material|material_override|surface_material_override)\b'
    r'[^\n=]*?\.(size|radius|height|albedo_color|albedo_texture|emission|'
    r'points|faces|extents|top_radius|bottom_radius)\s*=[^=]', re.M)


def _blocks(text: str) -> list[tuple[str, dict, str]]:
    """Every `[header ...]` block as (kind, attrs, body)."""
    out = []
    parts = re.split(r'^(?=\[)', text, flags=re.M)
    for part in parts:
        m = re.match(r'\[(\w+)([^\]]*)\]\n?(.*)', part, re.S)
        if not m:
            continue
        kind, attr_text, body = m.group(1), m.group(2), m.group(3)
        attrs = dict(re.findall(r'(\w+)=(?:"([^"]*)"|(\S+))', attr_text) and
                     [(k, a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|(\S+))', attr_text)])
        out.append((kind, attrs, body))
    return out


def _res_path(project: Path, res: str) -> Path:
    return project / res[len("res://"):] if res.startswith("res://") else project / res


def static_checks(project_dir: str, scene_res: str) -> dict:
    """Everything that can be known from the files alone.

    Returns {boot_scene, scaffold_marks, shared_subresources, instanced_scenes,
    findings} where findings is a list of {code, level, node, detail}.
    """
    project = Path(project_dir)
    findings: list[dict] = []
    boot = ""
    try:
        cfg = (project / "project.godot").read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^run/main_scene="([^"]+)"', cfg, re.M)
        boot = m.group(1) if m else ""
    except OSError:
        pass
    if not boot:
        findings.append({"code": "no_boot_scene", "level": "error", "node": "project.godot",
                         "detail": "application/run/main_scene is not set - the game boots into nothing"})
    scene_path = _res_path(project, scene_res)
    if not scene_path.is_file():
        findings.append({"code": "scene_missing", "level": "error", "node": scene_res,
                         "detail": f"{scene_res} does not exist"})
        return {"boot_scene": boot, "findings": findings, "shared_subresources": [],
                "instanced_scenes": [], "scaffold_marks": []}
    text = scene_path.read_text(encoding="utf-8", errors="replace")
    marks = [why for needle, why in _SCAFFOLD_MARKS if needle in text]
    if scene_res == boot and len(marks) >= 2:
        findings.append({"code": "boot_is_scaffold", "level": "error", "node": scene_res,
                         "detail": "the scene the game BOOTS into is still the scaffold demo ("
                                   + "; ".join(marks) + ") - set run/main_scene to the real one"})

    # --- shared sub_resources and who mutates them
    ext: dict[str, tuple[str, str]] = {i: (t, p) for t, p, i in _EXT_RE.findall(text)}
    subs: dict[str, dict] = {}
    for kind, attrs, body in _blocks(text):
        if kind == "sub_resource":
            subs[attrs["id"]] = {"type": attrs.get("type", ""), "local": bool(_LOCAL_RE.search(body)),
                                 "users": [], "scripts": set()}
    node_script: dict[str, str] = {}
    node_type: dict[str, str] = {}
    node_parent: dict[str, str] = {}
    instances: dict[str, list[str]] = {}
    for kind, attrs, body in _blocks(text):
        if kind != "node":
            continue
        name = attrs.get("name", "")
        parent = attrs.get("parent", "")
        full = name if parent in ("", None) else (f"{parent}/{name}" if parent != "." else name)
        node_type[full] = attrs.get("type", "")
        node_parent[full] = parent
        inst = attrs.get("instance", "")
        m = re.search(r'ExtResource\("([^"]+)"\)', inst) if inst else None
        if m and m.group(1) in ext:
            instances.setdefault(ext[m.group(1)][1], []).append(full)
        sm = re.search(r'^script = ExtResource\("([^"]+)"\)', body, re.M)
        if sm and sm.group(1) in ext:
            node_script[full] = ext[sm.group(1)][1]
        for sid in re.findall(r'SubResource\("([^"]+)"\)', body):
            if sid in subs:
                subs[sid]["users"].append(full)

    def script_mutates(res: str) -> Optional[str]:
        p = _res_path(project, res)
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        m = _MUTATION_RE.search(src)
        return m.group(0).strip() if m else None

    # A node's mutating script may be on the node itself or on an ancestor
    # (the prop's root script sizing its child mesh).
    def scripts_over(node: str) -> list[str]:
        out = []
        cur = node
        while cur:
            if cur in node_script:
                out.append(node_script[cur])
            parent = node_parent.get(cur, "")
            if parent in ("", ".", None):
                break
            cur = parent
        return out

    shared = []
    for sid, sub in subs.items():
        if len(sub["users"]) < 2 or sub["local"]:
            continue
        if not any(k in sub["type"] for k in ("Mesh", "Shape", "Material")):
            continue
        mutators = []
        for user in sub["users"]:
            for res in scripts_over(user):
                hit = script_mutates(res)
                if hit:
                    mutators.append(f"{res}: {hit}")
        row = {"id": sid, "type": sub["type"], "users": sub["users"], "mutated_by": sorted(set(mutators))}
        shared.append(row)
        if mutators:
            findings.append({"code": "shared_subresource_mutated", "level": "error",
                             "node": ", ".join(sub["users"][:4]),
                             "detail": f"{sub['type']} {sid} is ONE object shared by {len(sub['users'])} nodes "
                                       f"and a script assigns into it ({mutators[0]}); every node gets the "
                                       "last writer's value. Set resource_local_to_scene = true or "
                                       "duplicate() before mutating"})

    # --- instanced scenes whose own script resizes their own sub_resources
    inst_rows = []
    for res, users in instances.items():
        if len(users) < 2:
            continue
        p = _res_path(project, res)
        try:
            inner = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        inner_ext = {i: (t, pp) for t, pp, i in _EXT_RE.findall(inner)}
        inner_scripts = [inner_ext[s][1] for s in re.findall(r'^script = ExtResource\("([^"]+)"\)', inner, re.M)
                         if s in inner_ext]
        unlocal = [(t, i) for t, i in _SUB_RE.findall(inner)
                   if any(k in t for k in ("Mesh", "Shape", "Material"))]
        local_all = len(_LOCAL_RE.findall(inner)) >= len(unlocal) and unlocal
        hits = [f"{s}: {script_mutates(s)}" for s in inner_scripts if script_mutates(s)]
        row = {"scene": res, "instances": len(users), "sub_resources": len(unlocal),
               "mutated_by": hits, "local_to_scene": bool(local_all)}
        inst_rows.append(row)
        if hits and unlocal and not local_all:
            findings.append({"code": "instanced_subresource_mutated", "level": "error",
                             "node": f"{len(users)} instances of {res}",
                             "detail": f"{res} embeds {len(unlocal)} mesh/shape/material sub_resources without "
                                       f"resource_local_to_scene, and its script assigns into them ({hits[0]}). "
                                       "All instances share those objects: each gets whatever the LAST _ready() set. "
                                       "Mark them resource_local_to_scene = true"})

    # --- concave (trimesh) shapes under moving bodies: unsupported in Godot
    for kind, attrs, body in _blocks(text):
        if kind != "node" or attrs.get("type") != "CollisionShape3D":
            continue
        sm = re.search(r'^shape = SubResource\("([^"]+)"\)', body, re.M)
        if not sm or subs.get(sm.group(1), {}).get("type") != "ConcavePolygonShape3D":
            continue
        parent = attrs.get("parent", "")
        if node_type.get(parent, "") in ("RigidBody3D", "CharacterBody3D", "VehicleBody3D"):
            findings.append({"code": "trimesh_on_moving_body", "level": "error",
                             "node": f"{parent}/{attrs.get('name')}",
                             "detail": f"ConcavePolygonShape3D on a {node_type[parent]}: Godot does not "
                                       "collide a moving concave shape - use a convex or primitive shape"})
    return {"boot_scene": boot, "scaffold_marks": marks, "shared_subresources": shared,
            "instanced_scenes": inst_rows, "findings": findings}


# ------------------------------------------------------------------ engine

_AUDIT_GD = r'''
extends SceneTree
## Builders Gate scene audit - written by godot_scene_audit, never shipped.
## Frame 1 instantiates the scene in-tree; the checks run after two physics
## steps so the space knows every collider.

var _frame := 0
var _scene: Node
var _report := {"ok": true, "findings": [], "meshes": [], "surfaces": [], "bodies": 0}
var _player_h := 1.8
var _player_r := 0.4
var _headroom := 1.8
var _landing_min := 0.15
var _landing_max := 2.5


func _process(_delta: float) -> bool:
	_frame += 1
	if _frame == 1:
		_player_h = float(OS.get_environment("BGATE_AUDIT_PLAYER_H"))
		_player_r = float(OS.get_environment("BGATE_AUDIT_PLAYER_R"))
		_headroom = float(OS.get_environment("BGATE_AUDIT_HEADROOM"))
		var path := OS.get_environment("BGATE_AUDIT_SCENE")
		var packed = load(path)
		if packed == null or not (packed is PackedScene):
			_fail("scene_unloadable", path, "could not load " + path)
			_finish()
			return true
		_scene = (packed as PackedScene).instantiate()
		get_root().add_child(_scene)
		return false
	if _frame < 4:
		return false
	_run()
	_finish()
	return true


func _fail(code: String, node: String, detail: String, level: String = "error") -> void:
	_report["findings"].append({"code": code, "level": level, "node": node, "detail": detail})


func _finish() -> void:
	var out := OS.get_environment("BGATE_AUDIT_OUT")
	var f := FileAccess.open(out, FileAccess.WRITE)
	if f != null:
		f.store_string(JSON.stringify(_report))
		f.close()
	print("BGATE_AUDIT_DONE")
	quit()


func _owner_of(node: Node) -> CollisionObject3D:
	var cur := node.get_parent() if node is CollisionShape3D else node
	while cur != null:
		if cur is CollisionObject3D:
			return cur as CollisionObject3D
		cur = cur.get_parent()
	return null


func _body_of(node: Node) -> PhysicsBody3D:
	var owner := _owner_of(node)
	return owner as PhysicsBody3D if owner is PhysicsBody3D else null


func _moving(body: PhysicsBody3D) -> bool:
	if body is RigidBody3D:
		var rb := body as RigidBody3D
		return not rb.sleeping and (rb.linear_velocity.length() > 0.05 or rb.angular_velocity.length() > 0.05)
	if body is CharacterBody3D:
		return (body as CharacterBody3D).velocity.length() > 0.05
	return false


func _shape_aabb(cs: CollisionShape3D) -> AABB:
	var s := cs.shape
	var xf := cs.global_transform
	if s is BoxShape3D:
		var sz: Vector3 = (s as BoxShape3D).size
		return xf * AABB(-sz * 0.5, sz)
	if s is SphereShape3D:
		var r: float = (s as SphereShape3D).radius
		return xf * AABB(Vector3(-r, -r, -r), Vector3(r, r, r) * 2.0)
	if s is CapsuleShape3D:
		var c := s as CapsuleShape3D
		return xf * AABB(Vector3(-c.radius, -c.height * 0.5, -c.radius), Vector3(c.radius * 2.0, c.height, c.radius * 2.0))
	if s is CylinderShape3D:
		var c := s as CylinderShape3D
		return xf * AABB(Vector3(-c.radius, -c.height * 0.5, -c.radius), Vector3(c.radius * 2.0, c.height, c.radius * 2.0))
	var pts := PackedVector3Array()
	if s is ConvexPolygonShape3D:
		pts = (s as ConvexPolygonShape3D).points
	elif s is ConcavePolygonShape3D:
		pts = (s as ConcavePolygonShape3D).get_faces()
	if pts.size() == 0:
		return AABB()
	var box := AABB(xf * pts[0], Vector3.ZERO)
	for p in pts:
		box = box.expand(xf * p)
	return box


func _walk(node: Node, meshes: Array, shapes: Array) -> void:
	if node is MeshInstance3D and (node as MeshInstance3D).mesh != null and (node as MeshInstance3D).is_visible_in_tree():
		meshes.append(node)
	if node is CollisionShape3D and (node as CollisionShape3D).shape != null and not (node as CollisionShape3D).disabled:
		shapes.append(node)
	for c in node.get_children():
		_walk(c, meshes, shapes)


func _touched_laterally(space: PhysicsDirectSpaceState3D, body: PhysicsBody3D, world: AABB) -> bool:
	var c := world.get_center()
	var h := world.size * 0.5
	var probes := [
		[Vector3(c.x - h.x, c.y, c.z), Vector3.LEFT], [Vector3(c.x + h.x, c.y, c.z), Vector3.RIGHT],
		[Vector3(c.x, c.y, c.z - h.z), Vector3.FORWARD], [Vector3(c.x, c.y, c.z + h.z), Vector3.BACK],
		[Vector3(c.x, c.y + h.y, c.z), Vector3.UP],
	]
	for probe in probes:
		var from: Vector3 = probe[0] - probe[1] * 0.02
		var q := PhysicsRayQueryParameters3D.create(from, probe[0] + probe[1] * 0.05)
		q.exclude = [body.get_rid()]
		q.hit_from_inside = true
		if not space.intersect_ray(q).is_empty():
			return true
	return false


func _run() -> void:
	var meshes: Array = []
	var shapes: Array = []
	_walk(_scene, meshes, shapes)
	var space := _scene.get_viewport().world_3d.direct_space_state
	# Scene floor: the lowest mesh bottom. Anything resting within 5 cm of it
	# is ground, not a prop.
	var floor_y := INF
	var by_body: Dictionary = {}
	for mi in meshes:
		var world: AABB = mi.global_transform * mi.mesh.get_aabb()
		floor_y = minf(floor_y, world.position.y)
	var shape_by_body: Dictionary = {}
	for cs in shapes:
		var body := _body_of(cs)
		if body != null:
			if not shape_by_body.has(body):
				shape_by_body[body] = []
			shape_by_body[body].append(cs)
	_report["bodies"] = shape_by_body.size()
	# Meshes per body: the collider-matches-mesh test only means something
	# when one mesh is what the collider stands for.
	for mi in meshes:
		var b := _body_of(mi)
		if b != null:
			by_body[b] = int(by_body.get(b, 0)) + 1
	# The ground's TOP is the datum for landing heights: the lowest top among
	# the meshes whose bottom sits on the scene floor.
	var ground_top := INF
	for mi in meshes:
		var w: AABB = mi.global_transform * mi.mesh.get_aabb()
		if w.position.y <= floor_y + 0.05:
			ground_top = minf(ground_top, w.position.y + w.size.y)
	if ground_top == INF:
		ground_top = floor_y
	_report["ground_top"] = ground_top
	for mi in meshes:
		var world: AABB = mi.global_transform * mi.mesh.get_aabb()
		var size := world.size
		var path := String(_scene.get_path_to(mi))
		var body := _body_of(mi)
		var owner := _owner_of(mi)
		var row := {"node": path, "min": [world.position.x, world.position.y, world.position.z],
			"size": [size.x, size.y, size.z], "body": String(body.name) if body != null else "",
			"body_type": body.get_class() if body != null else ""}
		var big: bool = maxf(size.x, maxf(size.y, size.z)) >= 0.1
		# 1. collider present? A mesh under an Area3D is a pickup or a trigger's
		# visual: deliberately walk-through, not a missing collider.
		if owner is Area3D and body == null:
			row["collider"] = "area"
		elif body == null or not shape_by_body.has(body):
			if big:
				_fail("no_collider", path, "visible mesh %.2fx%.2fx%.2f m with no CollisionShape3D on any PhysicsBody3D ancestor - the player walks through it" % [size.x, size.y, size.z], "warning")
			row["collider"] = "none"
		else:
			# 2. collider matches the mesh?
			var cs_list: Array = shape_by_body[body]
			var merged := AABB()
			var first := true
			for cs in cs_list:
				var sb := _shape_aabb(cs)
				if sb.size == Vector3.ZERO:
					continue
				merged = sb if first else merged.merge(sb)
				first = false
			if not first and big:
				var dsize := (merged.size - size).abs()
				var dcenter := (merged.get_center() - world.get_center()).abs()
				var ref := maxf(size.x, maxf(size.y, size.z))
				var worst := maxf(dsize.x, maxf(dsize.y, dsize.z))
				var off := maxf(dcenter.x, maxf(dcenter.y, dcenter.z))
				row["collider_size_delta_m"] = worst
				row["collider_center_delta_m"] = off
				if by_body.get(body, 1) == 1 and (worst > 0.25 * ref + 0.05 or off > 0.25 * ref + 0.05):
					_fail("collider_mismatch", path, "collider box %.2fx%.2fx%.2f vs mesh %.2fx%.2fx%.2f (centres %.2f m apart) - the shape was not resized with the mesh, or a shared resource was" % [merged.size.x, merged.size.y, merged.size.z, size.x, size.y, size.z, off], "warning")
			row["collider"] = cs_list[0].shape.get_class()
		# 3. resting on something? (static/rigid bodies above the floor)
		var is_ground: bool = world.position.y <= floor_y + 0.05
		row["ground"] = is_ground
		if body != null and _moving(body):
			row["in_motion"] = true
		if body != null and not is_ground and big and (body is StaticBody3D or body is RigidBody3D) and not _moving(body):
			var from := Vector3(world.get_center().x, world.position.y + 0.01, world.get_center().z)
			var q := PhysicsRayQueryParameters3D.create(from, from + Vector3.DOWN * 10.0)
			q.exclude = [body.get_rid()]
			q.hit_from_inside = true
			var hit := space.intersect_ray(q)
			var gap: float = -1.0 if hit.is_empty() else world.position.y - hit.position.y
			row["gap_m"] = gap
			if not hit.is_empty() and gap < -0.05:
				_fail("sunk", path, "%.3f m INTO %s" % [-gap, String(hit.collider.name) if hit.collider else "?"], "info")
			if hit.is_empty() or gap > 0.03:
				# Touched on a side or from above is ATTACHED (a lintel between
				# two walls, a cabinet under a ceiling), not floating.
				if _touched_laterally(space, body, world):
					row["attached"] = true
				elif hit.is_empty():
					_fail("unsupported", path, "nothing under it within 10 m and nothing touching it - it floats", "warning")
				else:
					_fail("floating", path, "%.3f m above %s with nothing touching it - a prop that does not rest on something is a bug, not a camera angle" % [gap, String(hit.collider.name) if hit.collider else "?"], "warning")
		_report["meshes"].append(row)
	# 4. headroom over landing surfaces: the top face of every static body
	# between _landing_min and _landing_max above the floor, sampled on a 5x5
	# grid inset by the player's radius, one ray up per sample.
	for body in shape_by_body.keys():
		if not (body is StaticBody3D):
			continue
		var top := AABB()
		var first := true
		for cs in shape_by_body[body]:
			var sb := _shape_aabb(cs)
			if sb.size == Vector3.ZERO:
				continue
			top = sb if first else top.merge(sb)
			first = false
		if first:
			continue
		var top_y := top.position.y + top.size.y
		var height_above := top_y - ground_top
		if height_above < _landing_min or height_above > _landing_max:
			continue
		if top.size.x < 0.15 or top.size.z < 0.15:
			continue
		# Inset by the player's radius, or 40% of a narrow side so a shelf or
		# counter narrower than the player is still sampled along its middle.
		var inset_x: float = minf(_player_r, top.size.x * 0.4)
		var inset_z: float = minf(_player_r, top.size.z * 0.4)
		var clear := 0
		var total := 0
		for i in range(5):
			for j in range(5):
				var x: float = top.position.x + inset_x + (top.size.x - 2.0 * inset_x) * (i / 4.0)
				var z: float = top.position.z + inset_z + (top.size.z - 2.0 * inset_z) * (j / 4.0)
				var from := Vector3(x, top_y + 0.01, z)
				var q := PhysicsRayQueryParameters3D.create(from, from + Vector3.UP * _headroom)
				q.exclude = [body.get_rid()]
				q.hit_from_inside = true
				total += 1
				if space.intersect_ray(q).is_empty():
					clear += 1
		var frac := float(clear) / float(total)
		var name := String(_scene.get_path_to(body))
		_report["surfaces"].append({"node": name, "top_y": top_y, "height_above_floor": height_above,
			"size": [top.size.x, top.size.z], "clear_fraction": frac, "headroom_m": _headroom})
		if frac < 0.5:
			_fail("no_headroom", name, "only %d/%d of its top has %.2f m clear above it - something is standing on the landing" % [clear, total, _headroom], "warning")
		elif frac < 1.0:
			_fail("partial_headroom", name, "%d/%d of its top has %.2f m clear above it" % [clear, total, _headroom], "info")
'''


def audit(project_dir: str, scene: Optional[str] = None, *, player_height: float = 1.8,
          player_radius: float = 0.4, headroom: Optional[float] = None,
          timeout: int = 180) -> dict:
    """Static + in-engine audit of one scene (the boot scene by default)."""
    project = Path(project_dir)
    if not (project / "project.godot").is_file():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}
    static = static_checks(str(project), scene or "")
    scene_res = scene or static["boot_scene"]
    if scene and not scene.startswith("res://"):
        scene_res = "res://" + scene.replace("\\", "/").lstrip("/")
    if not scene:
        static = static_checks(str(project), scene_res) if scene_res else static
    findings = list(static["findings"])
    engine: dict = {}
    if scene_res and not any(f["code"] in ("scene_missing", "no_boot_scene") for f in findings):
        exe = _godot.find_godot()
        with tempfile.TemporaryDirectory(prefix="bgate_audit_") as tmp:
            script = Path(tmp) / "audit.gd"
            script.write_text(_AUDIT_GD, encoding="utf-8")
            out = Path(tmp) / "audit.json"
            env = {**os.environ, "BGATE_AUDIT_SCENE": scene_res, "BGATE_AUDIT_OUT": str(out),
                   "BGATE_AUDIT_PLAYER_H": str(player_height), "BGATE_AUDIT_PLAYER_R": str(player_radius),
                   "BGATE_AUDIT_HEADROOM": str(headroom if headroom is not None else player_height)}
            cmd = [exe, "--headless", "--path", str(project), "--script", str(script)]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env,
                                      stdin=subprocess.DEVNULL, creationflags=_godot._NO_WINDOW,
                                      **_godot._TEXT)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"audit timed out after {timeout}s", "scene": scene_res,
                        "findings": findings}
            output = (proc.stdout or "") + (proc.stderr or "")
            if out.is_file():
                try:
                    engine = json.loads(out.read_text(encoding="utf-8"))
                except ValueError:
                    engine = {}
            if not engine:
                return {"ok": False, "error": "the in-engine pass wrote no report",
                        "scene": scene_res, "findings": findings,
                        "engine_errors": _godot._errors(output), "output": output[-1500:]}
            findings += engine.get("findings", [])
            engine["errors"] = _godot._errors(output)
    errors = [f for f in findings if f["level"] == "error"]
    warnings = [f for f in findings if f["level"] == "warning"]
    return {"ok": not errors, "scene": scene_res, "boot_scene": static["boot_scene"],
            "is_boot_scene": scene_res == static["boot_scene"],
            "errors": errors, "warnings": warnings,
            "info": [f for f in findings if f["level"] == "info"],
            "shared_subresources": static["shared_subresources"],
            "instanced_scenes": static["instanced_scenes"],
            "meshes": engine.get("meshes", []), "surfaces": engine.get("surfaces", []),
            "bodies": engine.get("bodies", 0), "engine_errors": engine.get("errors", [])}


# ------------------------------------------------------------------ export verify

_FINGERPRINT_GD = r'''
extends SceneTree
## Builders Gate export fingerprint - the same walk over a scene loaded from the
## project and from the pck; the Python side diffs the two.

func _process(_d: float) -> bool:
	var path := OS.get_environment("BGATE_FP_SCENE")
	var report := {"scene": path, "nodes": {}, "ok": true}
	var packed = load(path)
	if packed == null or not (packed is PackedScene):
		report["ok"] = false
		report["error"] = "could not load " + path
	else:
		var root: Node = (packed as PackedScene).instantiate()
		get_root().add_child(root)
		_walk(root, root, report["nodes"])
		root.queue_free()
	var f := FileAccess.open(OS.get_environment("BGATE_FP_OUT"), FileAccess.WRITE)
	if f != null:
		f.store_string(JSON.stringify(report))
		f.close()
	print("BGATE_FP_DONE")
	quit()
	return true


func _r(v: float) -> float:
	return snappedf(v, 0.001)


func _color(c: Color) -> Array:
	return [_r(c.r), _r(c.g), _r(c.b), _r(c.a)]


func _mat(m: Material) -> Dictionary:
	if m == null:
		return {"kind": "none"}
	if m is StandardMaterial3D:
		var s := m as StandardMaterial3D
		return {"kind": "standard", "albedo": _color(s.albedo_color),
			"albedo_tex": s.albedo_texture.resource_path if s.albedo_texture else "",
			"emission": s.emission_enabled, "transparency": s.transparency,
			"path": m.resource_path}
	if m is ShaderMaterial:
		var sm := m as ShaderMaterial
		return {"kind": "shader", "shader": sm.shader.resource_path if sm.shader else "", "path": m.resource_path}
	return {"kind": m.get_class(), "path": m.resource_path}


func _walk(node: Node, root: Node, out: Dictionary) -> void:
	var key := "." if node == root else String(root.get_path_to(node))
	var row := {"type": node.get_class(), "visible": true}
	if node is Node3D:
		var n3 := node as Node3D
		row["visible"] = n3.visible
		var o := n3.transform.origin
		var s := n3.transform.basis.get_scale()
		row["origin"] = [_r(o.x), _r(o.y), _r(o.z)]
		row["scale"] = [_r(s.x), _r(s.y), _r(s.z)]
	if node is MeshInstance3D and (node as MeshInstance3D).mesh != null:
		var mi := node as MeshInstance3D
		var aabb := mi.mesh.get_aabb()
		row["mesh"] = mi.mesh.resource_path
		row["surfaces"] = mi.mesh.get_surface_count()
		row["aabb"] = [_r(aabb.size.x), _r(aabb.size.y), _r(aabb.size.z)]
		var mats := []
		for i in range(mi.mesh.get_surface_count()):
			mats.append(_mat(mi.get_active_material(i)))
		row["materials"] = mats
		row["material_override"] = mi.material_override != null
	if node is CollisionShape3D and (node as CollisionShape3D).shape != null:
		var cs := node as CollisionShape3D
		var sh := cs.shape
		row["shape"] = sh.get_class()
		if sh is BoxShape3D:
			var sz: Vector3 = (sh as BoxShape3D).size
			row["shape_size"] = [_r(sz.x), _r(sz.y), _r(sz.z)]
		elif sh is SphereShape3D:
			row["shape_size"] = [_r((sh as SphereShape3D).radius)]
		elif sh is CapsuleShape3D:
			row["shape_size"] = [_r((sh as CapsuleShape3D).radius), _r((sh as CapsuleShape3D).height)]
		elif sh is ConvexPolygonShape3D:
			row["shape_size"] = [(sh as ConvexPolygonShape3D).points.size()]
		elif sh is ConcavePolygonShape3D:
			row["shape_size"] = [(sh as ConcavePolygonShape3D).get_faces().size()]
		row["disabled"] = cs.disabled
	if node is Skeleton3D:
		row["bones"] = (node as Skeleton3D).get_bone_count()
	if node is AnimationPlayer:
		var names := []
		for a in (node as AnimationPlayer).get_animation_list():
			names.append(String(a))
		row["animations"] = names
	var script = node.get_script()
	if script != null and script is Script:
		row["script"] = (script as Script).resource_path
		var exports := {}
		for p in node.get_property_list():
			if p.usage & PROPERTY_USAGE_SCRIPT_VARIABLE == 0 or p.usage & PROPERTY_USAGE_STORAGE == 0:
				continue
			var v = node.get(p.name)
			match typeof(v):
				TYPE_FLOAT: exports[p.name] = _r(v)
				TYPE_INT, TYPE_BOOL, TYPE_STRING, TYPE_STRING_NAME: exports[p.name] = v
				TYPE_COLOR: exports[p.name] = _color(v)
				TYPE_VECTOR3: exports[p.name] = [_r(v.x), _r(v.y), _r(v.z)]
				TYPE_VECTOR2: exports[p.name] = [_r(v.x), _r(v.y)]
				TYPE_OBJECT: exports[p.name] = (v.resource_path if v != null and v is Resource else ("<obj>" if v != null else null))
				TYPE_NODE_PATH: exports[p.name] = String(v)
				_: pass
		row["exports"] = exports
	out[key] = row
	for c in node.get_children():
		_walk(c, root, out)
'''


def _fingerprint(exe: str, scene_res: str, *, project: Optional[Path], pck: Optional[Path],
                 timeout: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="bgate_fp_") as tmp:
        script = Path(tmp) / "fingerprint.gd"
        script.write_text(_FINGERPRINT_GD, encoding="utf-8")
        out = Path(tmp) / "fp.json"
        env = {**os.environ, "BGATE_FP_SCENE": scene_res, "BGATE_FP_OUT": str(out)}
        if pck is not None:
            cmd = [exe, "--headless", "--main-pack", str(pck), "--script", str(script)]
            cwd = str(pck.parent)
        else:
            cmd = [exe, "--headless", "--path", str(project), "--script", str(script)]
            cwd = None
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, cwd=cwd,
                                  stdin=subprocess.DEVNULL, creationflags=_godot._NO_WINDOW,
                                  **_godot._TEXT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"fingerprint timed out after {timeout}s"}
        output = (proc.stdout or "") + (proc.stderr or "")
        if not out.is_file():
            return {"ok": False, "error": "no fingerprint written", "errors": _godot._errors(output),
                    "output": output[-1500:]}
        try:
            report = json.loads(out.read_text(encoding="utf-8"))
        except ValueError as exc:
            return {"ok": False, "error": f"unreadable fingerprint: {exc}"}
        report["errors"] = _godot._errors(output)
        return report


def _boot_scene(project: Path) -> str:
    try:
        cfg = (project / "project.godot").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r'^run/main_scene="([^"]+)"', cfg, re.M)
    return m.group(1) if m else ""


def export_verify(project_dir: str, pck: str, scene: Optional[str] = None, *,
                  timeout: int = 180) -> dict:
    """Load `scene` from the project and from `pck`, fingerprint both, diff.

    Reports nodes missing from or added in the export, and per node: type,
    visibility, transform, mesh/aabb, materials per surface, collision shape
    class and size, bone and animation counts, and the exported script
    variables (the per-instance overrides that a pck has been seen to drop).
    """
    project = Path(project_dir)
    if not (project / "project.godot").is_file():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}
    pck_path = Path(pck)
    if not pck_path.is_absolute():
        pck_path = project / pck_path
    if not pck_path.is_file():
        return {"ok": False, "error": f"no pck at {pck_path} - export first"}
    scene_res = scene or _boot_scene(project)
    if scene_res and not scene_res.startswith("res://"):
        scene_res = "res://" + scene_res.replace("\\", "/").lstrip("/")
    if not scene_res:
        return {"ok": False, "error": "no scene given and run/main_scene is not set"}
    exe = _godot.find_godot()
    editor = _fingerprint(exe, scene_res, project=project, pck=None, timeout=timeout)
    if not editor.get("ok"):
        return {"ok": False, "error": "project load failed: " + str(editor.get("error")),
                "scene": scene_res, "editor": editor}
    shipped = _fingerprint(exe, scene_res, project=None, pck=pck_path, timeout=timeout)
    if not shipped.get("ok"):
        return {"ok": False, "error": "pck load failed: " + str(shipped.get("error")),
                "scene": scene_res, "shipped": shipped,
                "hint": "the scene is not in the export (export_filter / include_filter), "
                        "or the pck is older than the scene"}
    diffs: list[dict] = []
    a, b = editor["nodes"], shipped["nodes"]
    for key in sorted(set(a) - set(b)):
        diffs.append({"node": key, "field": "node", "editor": a[key]["type"], "shipped": None,
                      "detail": "present in the project, missing from the pck"})
    for key in sorted(set(b) - set(a)):
        diffs.append({"node": key, "field": "node", "editor": None, "shipped": b[key]["type"],
                      "detail": "in the pck only"})
    for key in sorted(set(a) & set(b)):
        ra, rb = a[key], b[key]
        for field in sorted(set(ra) | set(rb)):
            va, vb = ra.get(field), rb.get(field)
            if field == "exports" and isinstance(va, dict) and isinstance(vb, dict):
                for name in sorted(set(va) | set(vb)):
                    if va.get(name) != vb.get(name):
                        diffs.append({"node": key, "field": f"exports.{name}", "editor": va.get(name),
                                      "shipped": vb.get(name),
                                      "detail": "script variable differs in the export - a per-instance "
                                                "override the pck did not carry"})
                continue
            if va != vb:
                diffs.append({"node": key, "field": field, "editor": va, "shipped": vb,
                              "detail": {"materials": "material differs in the export",
                                         "shape": "collider class differs in the export",
                                         "shape_size": "collider size differs in the export",
                                         "mesh": "mesh resource differs in the export",
                                         "visible": "visibility differs in the export"}.get(
                                  field, f"{field} differs in the export")})
    return {"ok": not diffs, "scene": scene_res, "pck": str(pck_path),
            "nodes_editor": len(a), "nodes_shipped": len(b), "diffs": diffs,
            "editor_errors": editor.get("errors", []), "shipped_errors": shipped.get("errors", [])}
