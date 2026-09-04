extends SceneTree
## BUILDERS GATE TRACK GENERATOR - a spec-driven closed circuit for a driving game.
##
## Run by the `track_generate` MCP tool, which writes the spec to
## res://.bgate_track_spec.json and invokes this script headless. Everything it
## emits is NODE-SHAPED and NAMED so a designer can edit the result:
##   Track/Road (+RoadBody on layers 1 and 6 - 6 is ROAD ONLY, for wheel rays)
##   Track/RacingLine (Path3D, meta target_speeds every bake_interval metres)
##   Track/Checkpoints/Checkpoint_NN (Area3D, layer 3, meta index)
##   Track/Barriers/Barrier_<sector>_<L|R>_<nn> (pitched 8 m runs that FOLLOW the
##       road edge - a straight 20 m box on a grade sticks out of the ground)
##   Track/Tunnel/Tunnel_Roof|WallL|WallR (+ a rock mass in the terrain)
##   Track/Ground (+GroundBody, layer 1) - a heightfield under and around the road
##   Track/Grid/GridSlot_N, Track/Sun, Track/WorldEnvironment
##   Track/Props/<name>_<sector> (MultiMeshInstance3D per prop per sector, optional)
##
## LESSONS THIS FILE CARRIES (Corniche, 2026-09-04): bake the road at 1.5 m or
## the facets read as speed bumps at speed; the closure between the last designed
## sector and the start line is SOLVED (arc-line-arc at a chosen radius), never
## left to a spline; terrain under the road follows the LOWEST road within a
## clamp so switchbacks do not bury each other; every barrier run is pitched.
##
## Re-running is idempotent: the same spec gives the same file.

const Closure := preload("res://scripts/tools/bgate_track_closure.gd")
const SPEC_PATH := "res://.bgate_track_spec.json"
const REPORT_PATH := "res://.bgate_out/track_report.json"

var spec: Dictionary = {}
var road_width := 12.0
var waypoints: Array = []          # [{pos, sector, speed, tunnel, bl, br}]
var checkpoint_marks: Array = []   # [{pos, dir, name}]
var _pos := Vector3.ZERO
var _heading := 0.0
var _closure_total := 0.0
var report: Dictionary = {"ok": true, "errors": [], "warnings": []}


func _init() -> void:
	if not _load_spec():
		quit(1)
		return
	road_width = float(spec.get("road_width", 12.0))
	_build_spec()
	if waypoints.size() < 4:
		_fail("spec produced fewer than 4 waypoints")
		_finish(1)
		return
	var curve := _make_curve()
	_report_curvature(curve)
	var root := _build_scene(curve)
	var out: String = str(spec.get("out_scene", "res://scenes/track/track.tscn"))
	_save_scene(root, out)
	root.free()
	report["scene"] = out
	report["waypoints"] = waypoints.size()
	report["checkpoints"] = checkpoint_marks.size()
	report["lap_m"] = curve.get_baked_length()
	print("TRACK GENERATED: %s, %d waypoints, %d checkpoints, lap %.1f m" % [
		out, waypoints.size(), checkpoint_marks.size(), curve.get_baked_length()])
	_finish(0)


func _finish(code: int) -> void:
	report["ok"] = report["errors"].is_empty()
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(REPORT_PATH).get_base_dir())
	var f := FileAccess.open(REPORT_PATH, FileAccess.WRITE)
	if f != null:
		f.store_string(JSON.stringify(report, "  "))
		f.close()
	print("TRACK REPORT: " + JSON.stringify(report))
	quit(code)


func _fail(msg: String) -> void:
	push_error(msg)
	report["errors"].append(msg)


func _warn(msg: String) -> void:
	push_warning(msg)
	report["warnings"].append(msg)


func _load_spec() -> bool:
	if not FileAccess.file_exists(SPEC_PATH):
		push_error("no spec at %s - track_generate writes it" % SPEC_PATH)
		return false
	var txt := FileAccess.get_file_as_string(SPEC_PATH)
	var parsed = JSON.parse_string(txt)
	if not (parsed is Dictionary):
		push_error("spec is not a JSON object")
		return false
	spec = parsed
	if not (spec.get("sectors") is Array) or (spec["sectors"] as Array).is_empty():
		push_error("spec.sectors must be a non-empty array")
		return false
	return true


# ------------------------------------------------------------------ spec walk --

func _dir() -> Vector3:
	return Vector3(cos(_heading), 0.0, sin(_heading))


func _add_wp(sector: String, speed: float, tunnel: bool, bl: bool, br: bool) -> void:
	waypoints.append({"pos": _pos, "sector": sector, "speed": speed, "tunnel": tunnel, "bl": bl, "br": br})


func _mark_checkpoint(cp_name: String) -> void:
	checkpoint_marks.append({"pos": _pos, "dir": _dir(), "name": cp_name})


func _walk(sector: String, turn_deg: float, length: float, spacing: float, speed: float, tunnel: bool, bl: bool, br: bool, elev_gain: float) -> void:
	var steps: int = max(int(ceil(length / maxf(spacing, 1.0))), 1)
	var dtheta := deg_to_rad(turn_deg) / float(steps)
	var step_len := length / float(steps)
	var elev_step := elev_gain / float(steps)
	for i in range(steps):
		_heading += dtheta * 0.5
		_pos += _dir() * step_len
		_heading += dtheta * 0.5
		_pos.y += elev_step
		_add_wp(sector, speed, tunnel, bl, br)


func _build_spec() -> void:
	_pos = Vector3.ZERO
	_heading = 0.0
	var sectors: Array = spec["sectors"]
	var first: Dictionary = sectors[0]
	_add_wp(str(first.get("name", "sector_0")), float(first.get("speed", 40.0)), false,
		bool(first.get("barrier_left", true)), bool(first.get("barrier_right", true)))
	_mark_checkpoint("Checkpoint_00_StartFinish")
	var cp := 1
	for s in sectors:
		var sd: Dictionary = s
		var name := str(sd.get("name", "sector"))
		var kind := str(sd.get("kind", "straight"))
		var length := float(sd.get("length", 100.0))
		var turn := float(sd.get("turn_deg", 0.0)) if kind == "arc" else 0.0
		if kind == "arc" and sd.has("radius") and not sd.has("length"):
			length = absf(deg_to_rad(turn)) * float(sd["radius"])
		var spacing := float(sd.get("spacing", 14.0 if kind == "arc" else 20.0))
		_walk(name, turn, length, spacing, float(sd.get("speed", 40.0)),
			bool(sd.get("tunnel", false)), bool(sd.get("barrier_left", true)),
			bool(sd.get("barrier_right", true)), float(sd.get("elevation", 0.0)))
		if bool(sd.get("checkpoint", false)):
			_mark_checkpoint("Checkpoint_%02d" % cp)
			cp += 1
	_build_closure(cp)


func _build_closure(cp_index: int) -> void:
	var c: Dictionary = spec.get("closure", {})
	var radius := float(c.get("radius", 80.0))
	var sector_points: Array = []
	for wp in waypoints:
		sector_points.append(wp["pos"])
	var res: Dictionary = Closure.build(_pos, _heading, sector_points, {
		"radius": radius,
		"spacing": float(c.get("spacing", 14.0)),
		"line_speed": float(waypoints[0]["speed"]),
		"after_line": waypoints[1]["pos"],
		"top_speed": float(spec.get("top_speed", 60.0)),
		"lat_accel": float(spec.get("closure_lat_accel", 9.0)),
	})
	if not res["ok"]:
		_fail("closure: no %.0f m-radius arc-line-arc reaches the start line without crossing the designed sectors - shorten/rotate the last sectors or lower closure.radius" % radius)
		return
	var sol: Dictionary = res["sol"]
	_closure_total = float(sol["total"])
	report["closure"] = {"length_m": sol["total"], "radius": radius,
		"turn1_deg": sol["turn1_deg"], "straight_m": sol["straight"], "turn2_deg": sol["turn2_deg"]}
	var pts: Array = res["points"]
	var speeds: PackedFloat32Array = res["speeds"]
	var cp_at: int = res["checkpoint_index"]
	var prev: Vector3 = _pos
	var cname := str(c.get("name", "closure"))
	for i in range(pts.size()):
		_pos = pts[i]
		var tang: Vector3 = _pos - prev
		if tang.length() > 0.0001:
			_heading = atan2(tang.z, tang.x)
		_add_wp(cname, speeds[i], false, bool(c.get("barrier_left", true)), bool(c.get("barrier_right", true)))
		if i == cp_at and bool(c.get("checkpoint", true)):
			_mark_checkpoint("Checkpoint_%02d" % cp_index)
		prev = _pos
	_pos = Vector3.ZERO
	_heading = 0.0


# ------------------------------------------------------------------ the curve --

func _catmull_dir(points: Array, i: int) -> Vector3:
	var n := points.size()
	var prev: Vector3
	var next: Vector3
	if i == 0 or i == n - 1:
		prev = points[n - 2]
		next = points[1]
	else:
		prev = points[i - 1]
		next = points[i + 1]
	var d: Vector3 = next - prev
	if d.length_squared() < 0.000001:
		return Vector3.RIGHT
	return d.normalized()


func _chord(points: Array, i: int, step: int) -> float:
	var n := points.size()
	var j := i + step
	if j < 0:
		j = n - 2
	elif j > n - 1:
		j = 1
	return (points[j] - points[i]).length()


func _make_curve() -> Curve3D:
	var curve := Curve3D.new()
	curve.bake_interval = float(spec.get("bake_interval", 1.5))
	curve.up_vector_enabled = true
	var pts: Array = []
	for wp in waypoints:
		pts.append(wp["pos"])
	for i in range(pts.size()):
		var dir: Vector3 = _catmull_dir(pts, i)
		curve.add_point(pts[i], -dir * (_chord(pts, i, -1) / 3.0), dir * (_chord(pts, i, 1) / 3.0))
	return curve


func _report_curvature(curve: Curve3D) -> void:
	var sectors := PackedStringArray()
	for wp in waypoints:
		sectors.append(wp["sector"])
	var rep: Dictionary = Closure.curvature_report(curve, sectors)
	var per: Dictionary = {}
	for k in rep["order"]:
		var w: Array = rep["worst"][k]
		per[k] = {"min_radius_m": w[0], "at_m": w[1]}
		print("  curvature %-18s min radius %8.2f m at offset %7.1f" % [k, w[0], w[1]])
	report["sectors"] = per
	var c: Dictionary = spec.get("closure", {})
	var cname := str(c.get("name", "closure"))
	var min_r := float(c.get("min_radius", 30.0))
	var max_len := float(c.get("max_length", 400.0))
	var worst_c: Array = rep["worst"].get(cname, [INF, 0.0])
	if float(worst_c[0]) < min_r:
		_fail("closure minimum radius %.1f m is under the %.0f m bar - the road folds; widen closure.radius or rotate the last sectors" % [worst_c[0], min_r])
	if _closure_total > max_len:
		_fail("closure is %.0f m, over the %.0f m bar - the designed sectors do not come back around; the connector is doing the lap's work" % [_closure_total, max_len])
	var lap: float = curve.get_baked_length()
	var lap_min := float(spec.get("lap_min", 0.0))
	var lap_max := float(spec.get("lap_max", 1e9))
	if lap < lap_min or lap > lap_max:
		_fail("lap is %.0f m, outside the %.0f-%.0f m bar" % [lap, lap_min, lap_max])
	var tightest := INF
	for k in per:
		tightest = minf(tightest, float(per[k]["min_radius_m"]))
	var min_any := float(spec.get("min_radius", 10.0))
	if tightest < min_any:
		_fail("tightest corner radius %.1f m is under the %.1f m minimum - a %.0f m road cannot turn that tight" % [tightest, min_any, road_width])


func _sector_indices(sector_name: String) -> Array:
	var idxs: Array = []
	for i in range(waypoints.size()):
		if waypoints[i]["sector"] == sector_name:
			idxs.append(i)
	return idxs


func _tangent_at(i: int) -> Vector3:
	var n := waypoints.size()
	var a: Vector3 = waypoints[max(i - 1, 0)]["pos"]
	var b: Vector3 = waypoints[min(i + 1, n - 1)]["pos"]
	return (b - a).normalized()


func _right_of(t: Vector3) -> Vector3:
	return Vector3(-t.z, 0.0, t.x).normalized()


func _yaw_basis(forward: Vector3) -> Basis:
	var f := Vector3(forward.x, 0.0, forward.z).normalized()
	return Basis(Vector3.UP, atan2(-f.x, -f.z))


# ---------------------------------------------------------- target speeds -----

func _knot_offsets(curve: Curve3D) -> PackedFloat32Array:
	var offs := PackedFloat32Array()
	offs.append(0.0)
	var cum := 0.0
	var sub := 32
	for i in range(curve.point_count - 1):
		var prev: Vector3 = curve.sample(i, 0.0)
		for k in range(1, sub + 1):
			var p: Vector3 = curve.sample(i, float(k) / float(sub))
			cum += p.distance_to(prev)
			prev = p
		offs.append(cum)
	var target: float = curve.get_baked_length()
	if cum > 0.001:
		var scale: float = target / cum
		for i in range(offs.size()):
			offs[i] = offs[i] * scale
	return offs


func _build_target_speeds(curve: Curve3D) -> PackedFloat32Array:
	var offsets: PackedFloat32Array = _knot_offsets(curve)
	var speeds := PackedFloat32Array()
	for wp in waypoints:
		speeds.append(wp["speed"])
	var brake := float(spec.get("profile_brake", 9.0))
	var n := speeds.size()
	for _pass in 2:
		for i in range(n - 2, -1, -1):
			var d2: float = offsets[i + 1] - offsets[i]
			if d2 <= 0.001:
				continue
			speeds[i] = minf(speeds[i], sqrt(speeds[i + 1] * speeds[i + 1] + 2.0 * brake * d2))
		speeds[n - 1] = speeds[0]
	var result := PackedFloat32Array()
	if offsets.size() < 2:
		return result
	var step: float = maxf(curve.bake_interval, 0.5)
	var last_off: float = offsets[offsets.size() - 1]
	var count: int = int(ceil(last_off / step)) + 1
	var j := 0
	for i in range(count):
		var off: float = minf(float(i) * step, last_off)
		while j < offsets.size() - 2 and offsets[j + 1] < off:
			j += 1
		var o0: float = offsets[j]
		var o1: float = offsets[j + 1]
		var t: float = 0.0
		if o1 - o0 > 0.001:
			t = clampf((off - o0) / (o1 - o0), 0.0, 1.0)
		result.append(lerpf(speeds[j], speeds[j + 1], t))
	return result


# ----------------------------------------------------------------- meshes -----

func _add_tri(st: SurfaceTool, faces: Array, a: Vector3, b: Vector3, c: Vector3, uv_a: Vector2, uv_b: Vector2, uv_c: Vector2, ref_dir: Vector3) -> void:
	var normal: Vector3 = (b - a).cross(c - a)
	if normal.length_squared() < 0.00001:
		return
	var pb := b
	var pc := c
	var tb := uv_b
	var tc := uv_c
	if normal.dot(ref_dir) > 0.0:
		pb = c
		pc = b
		tb = uv_c
		tc = uv_b
	st.set_uv(uv_a)
	st.add_vertex(a)
	st.set_uv(tb)
	st.add_vertex(pb)
	st.set_uv(tc)
	st.add_vertex(pc)
	faces.append(a)
	faces.append(pb)
	faces.append(pc)


func _build_strip_mesh(strip_a: Array, strip_b: Array, ref_dir: Vector3, u_scale: float = 1.0, v_scale: float = 1.0) -> Dictionary:
	var st := SurfaceTool.new()
	st.begin(Mesh.PRIMITIVE_TRIANGLES)
	var faces: Array = []
	var n: int = min(strip_a.size(), strip_b.size())
	var lengths := PackedFloat32Array()
	lengths.append(0.0)
	var total := 0.0
	for i in range(1, n):
		total += (strip_a[i] - strip_a[i - 1]).length()
		lengths.append(total)
	for i in range(n - 1):
		var v_a: float = lengths[i] * v_scale
		var v_b: float = lengths[i + 1] * v_scale
		_add_tri(st, faces, strip_a[i], strip_b[i], strip_b[i + 1], Vector2(0.0, v_a), Vector2(u_scale, v_a), Vector2(u_scale, v_b), ref_dir)
		_add_tri(st, faces, strip_a[i], strip_b[i + 1], strip_a[i + 1], Vector2(0.0, v_a), Vector2(u_scale, v_b), Vector2(0.0, v_b), ref_dir)
	st.generate_normals()
	return {"mesh": st.commit(), "faces": PackedVector3Array(faces)}


func _material(key: String, fallback: Color, rough: float = 0.9) -> Material:
	var mats: Dictionary = spec.get("materials", {})
	var path := str(mats.get(key, ""))
	if path != "" and ResourceLoader.exists(path):
		var m = load(path)
		if m is Material:
			return m
		_warn("materials.%s = %s is not a Material" % [key, path])
	var sm := StandardMaterial3D.new()
	sm.albedo_color = fallback
	sm.roughness = rough
	return sm


func _static_body(node_name: String, faces: PackedVector3Array, layers: Array) -> StaticBody3D:
	var body := StaticBody3D.new()
	body.name = node_name
	body.collision_layer = 0
	for l in layers:
		body.set_collision_layer_value(int(l), true)
	body.collision_mask = 0
	var shape := ConcavePolygonShape3D.new()
	shape.set_faces(faces)
	var coll := CollisionShape3D.new()
	coll.name = "Collider"
	coll.shape = shape
	body.add_child(coll)
	return body


func _build_road(track: Node3D, curve: Curve3D) -> PackedVector3Array:
	var baked: PackedVector3Array = curve.get_baked_points()
	var n := baked.size()
	var left_pts: Array = []
	var right_pts: Array = []
	for i in range(n):
		var a: Vector3 = baked[n - 2] if i == 0 else baked[i - 1]
		var b: Vector3 = baked[1] if i == n - 1 else baked[i + 1]
		var right_v := _right_of((b - a).normalized())
		left_pts.append(baked[i] - right_v * (road_width * 0.5))
		right_pts.append(baked[i] + right_v * (road_width * 0.5))
	var built := _build_strip_mesh(left_pts, right_pts, Vector3.UP, 1.0, 1.0 / float(spec.get("road_tile_m", 6.0)))
	var road := MeshInstance3D.new()
	road.name = "Road"
	road.mesh = built["mesh"]
	road.mesh.surface_set_material(0, _material("road", Color(0.16, 0.15, 0.15)))
	track.add_child(road)
	# Layer 1 = world. Layer 6 = ROAD ONLY: a car's wheel rays ask this layer
	# first so terrain poking through the tarmac can never become a bump.
	var body := _static_body("RoadBody", built["faces"], [1, 6])
	road.add_child(body)
	# Shoulders: a gravel verge either side so running wide rests on something.
	var sw := float(spec.get("shoulder_width", 2.5))
	if sw > 0.0:
		for side in [["Shoulder_L", -1.0], ["Shoulder_R", 1.0]]:
			var inner: Array = []
			var outer: Array = []
			for i in range(n):
				var a: Vector3 = baked[n - 2] if i == 0 else baked[i - 1]
				var b: Vector3 = baked[1] if i == n - 1 else baked[i + 1]
				var r := _right_of((b - a).normalized()) * float(side[1])
				inner.append(baked[i] + r * (road_width * 0.5))
				outer.append(baked[i] + r * (road_width * 0.5 + sw) + Vector3(0, -0.08, 0))
			var sb := _build_strip_mesh(inner, outer, Vector3.UP)
			var mi := MeshInstance3D.new()
			mi.name = side[0]
			mi.mesh = sb["mesh"]
			mi.mesh.surface_set_material(0, _material("shoulder", Color(0.45, 0.40, 0.32)))
			mi.add_child(_static_body(str(side[0]) + "Body", sb["faces"], [1]))
			track.add_child(mi)
	return baked


# --------------------------------------------------------------- barriers -----

func _make_box_body(parent: Node3D, node_name: String, center: Vector3, basis: Basis, size: Vector3, color: Color, mesh_size: Vector3, mesh_offset: Vector3, mat_key: String) -> void:
	var body := StaticBody3D.new()
	body.name = node_name
	body.transform = Transform3D(basis, center)
	body.collision_layer = 0
	body.set_collision_layer_value(1, true)
	body.collision_mask = 0
	var mi := MeshInstance3D.new()
	mi.name = "Mesh"
	var bm := BoxMesh.new()
	bm.size = mesh_size
	mi.position = mesh_offset
	mi.mesh = bm
	mi.mesh.surface_set_material(0, _material(mat_key, color, 0.6))
	body.add_child(mi)
	var coll := CollisionShape3D.new()
	coll.name = "Collider"
	var bs := BoxShape3D.new()
	bs.size = size
	coll.shape = bs
	body.add_child(coll)
	parent.add_child(body)


func _build_barriers(parent: Node3D, baked: PackedVector3Array) -> void:
	var n := baked.size()
	if n < 3:
		return
	var run_len_max := float(spec.get("barrier_run_m", 8.0))
	var rail_h := float(spec.get("barrier_height", 1.0))
	var wp_of := PackedInt32Array()
	wp_of.resize(n)
	var wi := 0
	for i in range(n):
		while wi + 1 < waypoints.size() \
			and baked[i].distance_squared_to(waypoints[wi + 1]["pos"]) < baked[i].distance_squared_to(waypoints[wi]["pos"]):
			wi += 1
		wp_of[i] = wi
	var counters: Dictionary = {}
	var i := 0
	var made := 0
	while i < n - 1:
		var wp0: Dictionary = waypoints[wp_of[i]]
		var sector: String = wp0["sector"]
		var bl: bool = wp0["bl"]
		var br: bool = wp0["br"]
		var j := i + 1
		var run_len := 0.0
		while j < n:
			var wpj: Dictionary = waypoints[wp_of[j]]
			if wpj["sector"] != sector or wpj["bl"] != bl or wpj["br"] != br:
				break
			var d: float = baked[j].distance_to(baked[j - 1])
			if run_len + d > run_len_max and j > i + 1:
				break
			run_len += d
			j += 1
		var p0: Vector3 = baked[i]
		var p1: Vector3 = baked[j - 1]
		i = j - 1 if j - 1 > i else i + 1
		if not (bl or br):
			continue
		var chord: Vector3 = p1 - p0
		var seg_len: float = chord.length()
		if seg_len < 0.5:
			continue
		var tang: Vector3 = chord / seg_len
		var tang_h := Vector3(tang.x, 0.0, tang.z)
		if tang_h.length() < 0.01:
			continue
		tang_h = tang_h.normalized()
		var right_v := _right_of(tang_h)
		var bz: Vector3 = -tang
		var by: Vector3 = bz.cross(right_v).normalized()
		var bx: Vector3 = by.cross(bz).normalized()
		var basis := Basis(bx, by, bz)
		var mid: Vector3 = (p0 + p1) * 0.5
		var stripe := Color(0.85, 0.15, 0.15) if (made % 2 == 0) else Color(0.95, 0.95, 0.9)
		made += 1
		for side in [[bl, -1.0, "L"], [br, 1.0, "R"]]:
			if not side[0]:
				continue
			var pos: Vector3 = mid + right_v * float(side[1]) * (road_width * 0.5 + 0.6)
			var key: String = sector + str(side[2])
			var k: int = counters.get(key, 0)
			counters[key] = k + 1
			# The collider is 2 m tall so a lifted chassis cannot pass over it;
			# the visible rail is a thin band at the top of a 1 m post line.
			_make_box_body(parent, "Barrier_%s_%s_%02d" % [sector, side[2], k], pos, basis,
				Vector3(0.3, 2.0, seg_len), stripe, Vector3(0.14, 0.32, seg_len),
				Vector3(0.0, rail_h - 0.16, 0.0), "barrier")
	report["barrier_runs"] = made


# ----------------------------------------------------------- checkpoints ------

func _make_checkpoint(parent: Node3D, node_name: String, center: Vector3, forward: Vector3, index: int) -> void:
	var area := Area3D.new()
	var script_path := str(spec.get("checkpoint_script", ""))
	if script_path != "" and ResourceLoader.exists(script_path):
		area.set_script(load(script_path))
	area.name = node_name
	area.transform = Transform3D(_yaw_basis(forward), center)
	area.collision_layer = 0
	area.set_collision_layer_value(3, true)
	area.collision_mask = 0
	area.set_collision_mask_value(2, true)
	area.set_meta("index", index)
	var coll := CollisionShape3D.new()
	coll.name = "Trigger"
	var shape := BoxShape3D.new()
	shape.size = Vector3(road_width + 2.0, 6.0, 4.0)
	coll.shape = shape
	area.add_child(coll)
	parent.add_child(area)


func _build_grid_slots(parent: Node3D, curve: Curve3D) -> void:
	var length: float = curve.get_baked_length()
	var slots := int(spec.get("grid_slots", 6))
	for i in range(1, slots + 1):
		var idx: int = i - 1
		var row: int = idx / 2
		var col: int = idx % 2
		var lateral: float = -road_width * 0.27 if col == 0 else road_width * 0.27
		var stagger: float = 4.0 if col == 1 else 0.0
		var dist_back: float = 6.0 + float(row) * 16.0 + stagger
		var off: float = fposmod(length - dist_back, length)
		var center: Vector3 = curve.sample_baked(off, false)
		var ahead: Vector3 = curve.sample_baked(fposmod(off + 1.0, length), false)
		var behind: Vector3 = curve.sample_baked(fposmod(off - 1.0, length), false)
		var tang := Vector3(ahead.x - behind.x, 0.0, ahead.z - behind.z).normalized()
		var marker := Marker3D.new()
		marker.name = "GridSlot_%d" % i
		marker.transform = Transform3D(_yaw_basis(tang), center + _right_of(tang) * lateral)
		parent.add_child(marker)


# ----------------------------------------------------------------- tunnel -----

func _tunnel_spans() -> Array:
	var spans: Array = []
	var i := 0
	var n := waypoints.size()
	while i < n:
		if bool(waypoints[i]["tunnel"]):
			var j := i
			while j + 1 < n and bool(waypoints[j + 1]["tunnel"]):
				j += 1
			spans.append([maxi(i - 1, 0), mini(j + 1, n - 1)])
			i = j + 1
		else:
			i += 1
	return spans


func _build_tunnels(tunnel_node: Node3D) -> void:
	var h := float(spec.get("tunnel_height", 5.5))
	var w := road_width + float(spec.get("tunnel_extra_width", 3.0))
	var k := 0
	for span in _tunnel_spans():
		var roof_l: Array = []
		var roof_r: Array = []
		var wl_lo: Array = []
		var wl_hi: Array = []
		var wr_lo: Array = []
		var wr_hi: Array = []
		for i in range(span[0], span[1] + 1):
			var right_v := _right_of(_tangent_at(i))
			var base: Vector3 = waypoints[i]["pos"]
			var l: Vector3 = base - right_v * (w * 0.5)
			var r: Vector3 = base + right_v * (w * 0.5)
			roof_l.append(l + Vector3(0, h, 0))
			roof_r.append(r + Vector3(0, h, 0))
			wl_lo.append(l)
			wl_hi.append(l + Vector3(0, h, 0))
			wr_lo.append(r)
			wr_hi.append(r + Vector3(0, h, 0))
		var mid_i: int = int((span[0] + span[1]) / 2)
		var inward_l := _right_of(_tangent_at(mid_i))
		var suffix := "" if k == 0 else "_%d" % k
		for part in [["Tunnel_Roof" + suffix, roof_l, roof_r, Vector3.DOWN],
					 ["Tunnel_WallL" + suffix, wl_lo, wl_hi, inward_l],
					 ["Tunnel_WallR" + suffix, wr_lo, wr_hi, -inward_l]]:
			var built := _build_strip_mesh(part[1], part[2], part[3], 1.0, 0.25)
			var mi := MeshInstance3D.new()
			mi.name = part[0]
			mi.mesh = built["mesh"]
			mi.mesh.surface_set_material(0, _material("tunnel", Color(0.32, 0.29, 0.27)))
			mi.add_child(_static_body(str(part[0]) + "Body", built["faces"], [1]))
			tunnel_node.add_child(mi)
		# Lamps every ~15 m inside the bore.
		var cum := 0.0
		var lamp := 0
		for i in range(span[0] + 1, span[1] + 1):
			cum += (waypoints[i]["pos"] - waypoints[i - 1]["pos"]).length()
			if cum >= 15.0:
				cum = 0.0
				var light := OmniLight3D.new()
				light.name = "TunnelLight%s_%02d" % [suffix, lamp]
				lamp += 1
				light.position = waypoints[i]["pos"] + Vector3(0, h - 0.8, 0)
				light.light_color = Color(1.0, 0.78, 0.5)
				light.light_energy = 2.0
				light.omni_range = 14.0
				light.shadow_enabled = false
				tunnel_node.add_child(light)
		k += 1
	report["tunnels"] = k


# ---------------------------------------------------------------- terrain -----

func _hash_noise(x: float, z: float) -> float:
	# Cheap value noise, deterministic.
	var n := 0.0
	var amp := 1.0
	var f := 1.0
	for o in 4:
		n += amp * (sin(x * 0.0113 * f + z * 0.0071 * f) * cos(z * 0.0091 * f - x * 0.0047 * f))
		amp *= 0.5
		f *= 2.1
	return n


func _build_ground(track: Node3D, baked: PackedVector3Array, tunnel_spans: Array) -> Dictionary:
	var t: Dictionary = spec.get("terrain", {})
	if not bool(t.get("enabled", true)):
		return {}
	var cols := int(t.get("cols", 192))
	var margin := float(t.get("margin", 400.0))
	var sea := float(t.get("sea_level", -4.5))
	var sea_side := str(t.get("sea_side", "none"))     # left | right | none
	var beach := float(t.get("beach_distance", 60.0))
	var hill_h := float(t.get("hill_height", 90.0))
	var hill_d := float(t.get("hill_distance", 250.0))
	var cut_start := road_width * 0.5 + float(spec.get("shoulder_width", 2.5)) + 0.5
	var cut_slope := float(t.get("cut_slope", 0.6))
	var cut_max := float(t.get("cut_max", 8.0))
	var sink := float(t.get("road_sink", 0.35))
	var low_clamp := float(t.get("low_clamp", 12.0))
	var tunnel_h := float(spec.get("tunnel_height", 5.5))

	var min_x := INF
	var max_x := -INF
	var min_z := INF
	var max_z := -INF
	for p in baked:
		min_x = minf(min_x, p.x)
		max_x = maxf(max_x, p.x)
		min_z = minf(min_z, p.z)
		max_z = maxf(max_z, p.z)
	min_x -= margin
	max_x += margin
	min_z -= margin
	max_z += margin
	var rows := cols
	var n := baked.size()
	# Road samples with their right vectors and tunnel flag, bucketed on a grid.
	var sx := PackedFloat32Array()
	var sy := PackedFloat32Array()
	var sz := PackedFloat32Array()
	var srx := PackedFloat32Array()
	var srz := PackedFloat32Array()
	var stun := PackedByteArray()
	var wi := 0
	for i in range(0, n, 2):
		var a: Vector3 = baked[maxi(i - 1, 0)]
		var b: Vector3 = baked[mini(i + 1, n - 1)]
		var r := _right_of((b - a).normalized())
		sx.append(baked[i].x)
		sy.append(baked[i].y)
		sz.append(baked[i].z)
		srx.append(r.x)
		srz.append(r.z)
		while wi + 1 < waypoints.size() \
			and baked[i].distance_squared_to(waypoints[wi + 1]["pos"]) < baked[i].distance_squared_to(waypoints[wi]["pos"]):
			wi += 1
		stun.append(1 if bool(waypoints[wi]["tunnel"]) else 0)
	var sn := sx.size()
	var bucket := 48.0
	var bcols := int(ceil((max_x - min_x) / bucket)) + 1
	var brows := int(ceil((max_z - min_z) / bucket)) + 1
	var buckets: Array = []
	buckets.resize(bcols * brows)
	for k in range(sn):
		var bc := int((sx[k] - min_x) / bucket)
		var br := int((sz[k] - min_z) / bucket)
		var key := br * bcols + bc
		# Arrays are references, PackedInt32Array is a VALUE - appending to a
		# packed array fetched out of another array appends to a copy.
		if buckets[key] == null:
			buckets[key] = []
		(buckets[key] as Array).append(k)
	var reach := maxf(hill_d, beach) + bucket
	var reach_cells := int(ceil(reach / bucket))
	var grid: Array = []
	grid.resize(rows * cols)
	var y_hi := -INF
	for r in range(rows):
		var wz: float = lerpf(min_z, max_z, float(r) / float(rows - 1))
		var br0 := int((wz - min_z) / bucket)
		for c in range(cols):
			var wx: float = lerpf(min_x, max_x, float(c) / float(cols - 1))
			var bc0 := int((wx - min_x) / bucket)
			var best_d2 := INF
			var best_k := -1
			var low_y := INF
			var tun_d2 := INF
			var tun_y := 0.0
			var low2 := low_clamp * low_clamp
			for dr in range(-reach_cells, reach_cells + 1):
				var rr := br0 + dr
				if rr < 0 or rr >= brows:
					continue
				for dc in range(-reach_cells, reach_cells + 1):
					var cc := bc0 + dc
					if cc < 0 or cc >= bcols:
						continue
					var lst = buckets[rr * bcols + cc]
					if lst == null:
						continue
					for k in lst:
						var dx: float = wx - sx[k]
						var dz: float = wz - sz[k]
						var d2: float = dx * dx + dz * dz
						if d2 < best_d2:
							best_d2 = d2
							best_k = k
						if d2 < low2 and sy[k] < low_y:
							low_y = sy[k]
						if stun[k] == 1 and d2 < tun_d2:
							tun_d2 = d2
							tun_y = sy[k]
			var wy: float
			if best_k < 0:
				wy = sea - 3.0
			else:
				var d_road := sqrt(best_d2)
				var base: float = sy[best_k] if low_y == INF else low_y
				var shelf: float = base - sink
				var side: float = signf((wx - sx[best_k]) * srx[best_k] + (wz - sz[best_k]) * srz[best_k])
				var is_sea: bool = (sea_side == "right" and side > 0.0) or (sea_side == "left" and side < 0.0)
				if is_sea:
					var drop := smoothstep(cut_start, cut_start + beach, d_road)
					wy = lerpf(shelf, sea - 3.0, drop)
				else:
					var bank: float = minf(maxf(d_road - cut_start, 0.0) * cut_slope, cut_max)
					var near_h: float = shelf + bank
					var ramp := smoothstep(cut_start + 20.0, hill_d, d_road)
					var crest: float = base + hill_h * (0.6 + 0.4 * _hash_noise(wx, wz))
					wy = lerpf(near_h, crest, ramp)
				# Rock mass over a tunnel bore, blended in over its reach.
				if tun_d2 < 150.0 * 150.0:
					var ft: float = (1.0 - smoothstep(0.0, 150.0, sqrt(tun_d2))) * smoothstep(road_width * 0.5 + 4.0, road_width * 0.5 + 24.0, d_road)
					wy = lerpf(wy, maxf(wy, tun_y + tunnel_h + 18.0), ft)
			# Fade the patch edge down to the sea so the world ends in water.
			var edge_d: float = minf(minf(wx - min_x, max_x - wx), minf(wz - min_z, max_z - wz))
			wy = lerpf(sea - 3.0, wy, smoothstep(0.0, 150.0, edge_d))
			y_hi = maxf(y_hi, wy)
			grid[r * cols + c] = Vector3(wx, wy, wz)
	# POST-PASS: the corridor under the road is CLAMPED below the tarmac. The
	# shelf above follows the lowest road within low_clamp, but bilinear cells of
	# 8-12 m still poke through in concave dips (measured: 222 buried samples,
	# worst 0.51 m, on the first run). Every grid vertex whose cell the road
	# crosses is forced under the road at that point.
	var gi_tmp := {"grid": grid, "rows": rows, "cols": cols, "min_x": min_x, "max_x": max_x, "min_z": min_z, "max_z": max_z}
	var hw := road_width * 0.5 + float(spec.get("shoulder_width", 2.5)) + 1.0
	for i in range(0, n):
		var a2: Vector3 = baked[maxi(i - 1, 0)]
		var b2: Vector3 = baked[mini(i + 1, n - 1)]
		var rv := _right_of((b2 - a2).normalized())
		var lat := -hw
		while lat <= hw + 0.01:
			var p: Vector3 = baked[i] + rv * lat
			var fx: float = (p.x - min_x) / (max_x - min_x) * float(cols - 1)
			var fz: float = (p.z - min_z) / (max_z - min_z) * float(rows - 1)
			var c0 := clampi(int(floor(fx)), 0, cols - 2)
			var r0 := clampi(int(floor(fz)), 0, rows - 2)
			var cap_y: float = p.y - sink
			for idx in [r0 * cols + c0, r0 * cols + c0 + 1, (r0 + 1) * cols + c0, (r0 + 1) * cols + c0 + 1]:
				var g: Vector3 = grid[idx]
				if g.y > cap_y:
					grid[idx] = Vector3(g.x, cap_y, g.z)
			lat += 3.0
	var st := SurfaceTool.new()
	st.begin(Mesh.PRIMITIVE_TRIANGLES)
	var faces: Array = []
	for r in range(rows - 1):
		for c in range(cols - 1):
			var p00: Vector3 = grid[r * cols + c]
			var p10: Vector3 = grid[r * cols + c + 1]
			var p01: Vector3 = grid[(r + 1) * cols + c]
			var p11: Vector3 = grid[(r + 1) * cols + c + 1]
			var u0 := float(c) / 8.0
			var u1 := float(c + 1) / 8.0
			var v0 := float(r) / 8.0
			var v1 := float(r + 1) / 8.0
			_add_tri(st, faces, p00, p10, p11, Vector2(u0, v0), Vector2(u1, v0), Vector2(u1, v1), Vector3.UP)
			_add_tri(st, faces, p00, p11, p01, Vector2(u0, v0), Vector2(u1, v1), Vector2(u0, v1), Vector3.UP)
	st.generate_normals()
	var ground := MeshInstance3D.new()
	ground.name = "Ground"
	ground.mesh = st.commit()
	ground.mesh.surface_set_material(0, _material("ground", Color(0.42, 0.40, 0.30)))
	ground.add_child(_static_body("GroundBody", PackedVector3Array(faces), [1]))
	track.add_child(ground)
	# Sea plane.
	if sea_side != "none":
		var seam := MeshInstance3D.new()
		seam.name = "Sea"
		var pm := PlaneMesh.new()
		pm.size = Vector2(max_x - min_x + 6000.0, max_z - min_z + 6000.0)
		seam.mesh = pm
		seam.position = Vector3((min_x + max_x) * 0.5, sea, (min_z + max_z) * 0.5)
		seam.mesh.surface_set_material(0, _material("sea", Color(0.12, 0.30, 0.42), 0.15))
		track.add_child(seam)
	report["terrain"] = {"cols": cols, "cell_m": (max_x - min_x) / float(cols - 1), "max_y": y_hi}
	return {"grid": grid, "rows": rows, "cols": cols, "min_x": min_x, "max_x": max_x, "min_z": min_z, "max_z": max_z}


func _ground_height(gi: Dictionary, x: float, z: float) -> float:
	if gi.is_empty():
		return -INF
	var cols: int = gi["cols"]
	var rows: int = gi["rows"]
	var fx: float = (x - gi["min_x"]) / (gi["max_x"] - gi["min_x"]) * float(cols - 1)
	var fz: float = (z - gi["min_z"]) / (gi["max_z"] - gi["min_z"]) * float(rows - 1)
	var c0 := clampi(int(floor(fx)), 0, cols - 2)
	var r0 := clampi(int(floor(fz)), 0, rows - 2)
	var tx: float = clampf(fx - float(c0), 0.0, 1.0)
	var tz: float = clampf(fz - float(r0), 0.0, 1.0)
	var g: Array = gi["grid"]
	var y00: float = (g[r0 * cols + c0] as Vector3).y
	var y10: float = (g[r0 * cols + c0 + 1] as Vector3).y
	var y01: float = (g[(r0 + 1) * cols + c0] as Vector3).y
	var y11: float = (g[(r0 + 1) * cols + c0 + 1] as Vector3).y
	return lerpf(lerpf(y00, y10, tx), lerpf(y01, y11, tx), tz)


func _check_support(baked: PackedVector3Array, gi: Dictionary) -> void:
	# The road must sit ON the ground: never buried, never floating far.
	if gi.is_empty():
		return
	var buried := 0
	var worst_float := 0.0
	var worst_bury := 0.0
	var n := baked.size()
	for i in range(0, n, 3):
		var a: Vector3 = baked[maxi(i - 1, 0)]
		var b: Vector3 = baked[mini(i + 1, n - 1)]
		var r := _right_of((b - a).normalized())
		for lat in [-road_width * 0.5, 0.0, road_width * 0.5]:
			var p: Vector3 = baked[i] + r * lat
			var gy := _ground_height(gi, p.x, p.z)
			var gap := p.y - gy
			if gap < 0.05:
				buried += 1
				worst_bury = maxf(worst_bury, -gap)
			worst_float = maxf(worst_float, gap)
	report["support"] = {"buried_samples": buried, "worst_bury_m": worst_bury, "worst_float_m": worst_float}
	if buried > 0:
		_fail("terrain rises above the road at %d sample(s), worst %.2f m - raise terrain.road_sink or low_clamp" % [buried, worst_bury])


# ------------------------------------------------------------------ props -----

func _build_props(track: Node3D, baked: PackedVector3Array, gi: Dictionary) -> void:
	var props: Array = spec.get("props", [])
	if props.is_empty():
		return
	var holder := Node3D.new()
	holder.name = "Props"
	track.add_child(holder)
	var n := baked.size()
	var wp_of := PackedInt32Array()
	wp_of.resize(n)
	var wi := 0
	for i in range(n):
		while wi + 1 < waypoints.size() \
			and baked[i].distance_squared_to(waypoints[wi + 1]["pos"]) < baked[i].distance_squared_to(waypoints[wi]["pos"]):
			wi += 1
		wp_of[i] = wi
	var placed := 0
	var rng := RandomNumberGenerator.new()
	for pd in props:
		var p: Dictionary = pd
		var scene_path := str(p.get("scene", ""))
		if not ResourceLoader.exists(scene_path):
			_warn("prop scene missing: %s" % scene_path)
			continue
		var packed = load(scene_path)
		var mesh: Mesh = null
		var inst = (packed as PackedScene).instantiate() if packed is PackedScene else null
		if inst != null:
			mesh = _first_mesh(inst)
			inst.free()
		if mesh == null:
			_warn("prop %s has no MeshInstance3D to scatter" % scene_path)
			continue
		var name := str(p.get("name", scene_path.get_file().get_basename()))
		var sectors: Array = p.get("sectors", [])
		var spacing := float(p.get("spacing", 20.0))
		var offset := float(p.get("offset", 10.0))
		var jitter := float(p.get("jitter", 3.0))
		var side := str(p.get("side", "both"))
		var smin := float((p.get("scale", [1.0, 1.0]) as Array)[0])
		var smax := float((p.get("scale", [1.0, 1.0]) as Array)[1])
		rng.seed = hash(name)
		var xforms: Array = []
		var cum := 0.0
		for i in range(1, n):
			cum += baked[i].distance_to(baked[i - 1])
			if cum < spacing:
				continue
			cum = 0.0
			var wp: Dictionary = waypoints[wp_of[i]]
			if not sectors.is_empty() and not sectors.has(wp["sector"]):
				continue
			if bool(wp["tunnel"]):
				continue
			var a: Vector3 = baked[maxi(i - 1, 0)]
			var b: Vector3 = baked[mini(i + 1, n - 1)]
			var r := _right_of((b - a).normalized())
			for sgn in ([-1.0, 1.0] if side == "both" else ([-1.0] if side == "left" else [1.0])):
				var d: float = road_width * 0.5 + offset + rng.randf_range(-jitter, jitter)
				var along: float = rng.randf_range(-jitter, jitter)
				var pos: Vector3 = baked[i] + r * (sgn * d) + (b - a).normalized() * along
				var gy := _ground_height(gi, pos.x, pos.z)
				if gy > -INF:
					pos.y = gy
				var s := rng.randf_range(smin, smax)
				var xf := Transform3D(Basis(Vector3.UP, rng.randf_range(0.0, TAU)).scaled(Vector3.ONE * s), pos)
				xforms.append(xf)
		if xforms.is_empty():
			continue
		var mm := MultiMesh.new()
		mm.transform_format = MultiMesh.TRANSFORM_3D
		mm.mesh = mesh
		mm.instance_count = xforms.size()
		for k in range(xforms.size()):
			mm.set_instance_transform(k, xforms[k])
		var mmi := MultiMeshInstance3D.new()
		mmi.name = "Prop_" + name
		mmi.multimesh = mm
		var vr := float(p.get("visibility_range", 0.0))
		if vr > 0.0:
			mmi.visibility_range_end = vr
		holder.add_child(mmi)
		placed += xforms.size()
	report["props_placed"] = placed


func _first_mesh(node: Node) -> Mesh:
	if node is MeshInstance3D and (node as MeshInstance3D).mesh != null:
		return (node as MeshInstance3D).mesh
	for c in node.get_children():
		var m := _first_mesh(c)
		if m != null:
			return m
	return null


# ------------------------------------------------------------- environment ----

func _build_environment(track: Node3D) -> void:
	var e: Dictionary = spec.get("environment", {})
	var sun := DirectionalLight3D.new()
	sun.name = "Sun"
	var elev := deg_to_rad(float(e.get("sun_elevation_deg", 25.0)))
	var azim := deg_to_rad(float(e.get("sun_azimuth_deg", 30.0)))
	var dir := Vector3(cos(elev) * sin(azim), -sin(elev), cos(elev) * cos(azim)).normalized()
	sun.transform = Transform3D(Basis.looking_at(dir, Vector3.UP), Vector3(0, 60, 0))
	sun.light_color = Color(str(e.get("sun_color", "#fff0d8")))
	sun.light_energy = float(e.get("sun_energy", 1.4))
	sun.shadow_enabled = true
	sun.directional_shadow_max_distance = float(e.get("shadow_distance", 300.0))
	track.add_child(sun)
	var we := WorldEnvironment.new()
	we.name = "WorldEnvironment"
	var env_path := str(e.get("environment", ""))
	if env_path != "" and ResourceLoader.exists(env_path):
		we.environment = load(env_path)
	else:
		var env := Environment.new()
		var sky := Sky.new()
		var mat := ProceduralSkyMaterial.new()
		mat.sky_top_color = Color(str(e.get("sky_top", "#4a5aa8")))
		mat.sky_horizon_color = Color(str(e.get("sky_horizon", "#e8a070")))
		mat.ground_horizon_color = Color(str(e.get("sky_horizon", "#e8a070")))
		mat.ground_bottom_color = Color(0.2, 0.18, 0.16)
		sky.sky_material = mat
		env.background_mode = Environment.BG_SKY
		env.sky = sky
		env.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
		env.fog_enabled = true
		env.fog_density = float(e.get("fog_density", 0.0012))
		env.fog_light_color = Color(str(e.get("fog_color", "#d8b8a8")))
		env.tonemap_mode = Environment.TONE_MAPPER_FILMIC
		env.glow_enabled = true
		env.glow_intensity = 0.4
		we.environment = env
	track.add_child(we)


# ------------------------------------------------------------------ scene -----

func _build_scene(curve: Curve3D) -> Node3D:
	var track := Node3D.new()
	track.name = "Track"
	var baked: PackedVector3Array = _build_road(track, curve)
	var racing_line := Path3D.new()
	racing_line.name = "RacingLine"
	racing_line.curve = curve
	racing_line.set_meta("target_speeds", _build_target_speeds(curve))
	track.add_child(racing_line)
	var checkpoints := Node3D.new()
	checkpoints.name = "Checkpoints"
	track.add_child(checkpoints)
	for i in range(checkpoint_marks.size()):
		var cm: Dictionary = checkpoint_marks[i]
		_make_checkpoint(checkpoints, cm["name"], cm["pos"], cm["dir"], i)
	var barriers := Node3D.new()
	barriers.name = "Barriers"
	track.add_child(barriers)
	_build_barriers(barriers, baked)
	var tunnel := Node3D.new()
	tunnel.name = "Tunnel"
	track.add_child(tunnel)
	_build_tunnels(tunnel)
	var gi := _build_ground(track, baked, _tunnel_spans())
	_check_support(baked, gi)
	var grid_node := Node3D.new()
	grid_node.name = "Grid"
	track.add_child(grid_node)
	_build_grid_slots(grid_node, curve)
	_build_environment(track)
	_build_props(track, baked, gi)
	return track


func _set_owners(node: Node, root: Node) -> void:
	for child in node.get_children():
		child.owner = root
		if child.scene_file_path != "":
			continue
		_set_owners(child, root)


func _save_scene(root: Node, path: String) -> void:
	_set_owners(root, root)
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(path).get_base_dir())
	var packed := PackedScene.new()
	var err := packed.pack(root)
	if err != OK:
		_fail("pack failed: %d" % err)
		return
	var save_err := ResourceSaver.save(packed, path)
	if save_err != OK:
		_fail("save failed: %d" % save_err)
